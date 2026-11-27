"""Bounded ASGI transport, mounted unchanged in FastAPI in production.

Keeping HTTP decoding separate lets the complete public contract be tested offline.
No test-only authentication or database path exists in this transport.
"""
from email.parser import BytesParser
from email.policy import default
from http.cookies import SimpleCookie
import json
from urllib.parse import parse_qs
from uuid import uuid4

from .contracts import APIError, Request, dumps


class HTTPApplication:
    def __init__(self, service, mode='backend'):
        self.service, self.mode = service, mode

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        rid = str(uuid4())
        headers = {k.decode('latin1').lower(): v.decode('latin1') for k, v in scope['headers']}
        base_headers = {'Cache-Control': 'no-store', 'X-Request-ID': rid, 'X-Content-Type-Options': 'nosniff'}
        try:
            data = bytearray()
            maximum = self.service.settings.upload_limit + 65536 if 'multipart/form-data' in headers.get('content-type', '') else 65536
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                data.extend(message.get('body', b''))
                if len(data) > maximum:
                    raise APIError(413, 'FILE_TOO_LARGE')
                if not message.get('more_body', False):
                    break
            body, upload = {}, None
            ctype = headers.get('content-type', '')
            if data:
                if scope['method'] in ('GET', 'HEAD'):
                    raise APIError(422, 'VALIDATION_ERROR')
                if ctype.split(';')[0].strip() == 'application/json':
                    try:
                        body = json.loads(data)
                        if not isinstance(body, dict):
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        raise APIError(422, 'VALIDATION_ERROR') from None
                elif ctype.startswith('multipart/form-data'):
                    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + ctype.encode('latin1') + b'\r\nMIME-Version: 1.0\r\n\r\n' + data)
                    parts = list(message.iter_parts())
                    if not message.is_multipart() or len(parts) != 1 or parts[0].get_param('name', header='content-disposition') != 'file':
                        raise APIError(422, 'VALIDATION_ERROR')
                    part = parts[0]
                    upload = (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
                else:
                    raise APIError(415, 'FILE_TYPE_UNSUPPORTED')
            cookie = SimpleCookie()
            cookie.load(headers.get('cookie', ''))
            request = Request(scope['method'], scope['path'], headers, body,
                {k: v.value for k, v in cookie.items()},
                {k: v[-1] for k, v in parse_qs(scope.get('query_string', b'').decode()).items()},
                scope.get('client', ('unknown', 0))[0], upload)
            # All I/O, password hashing and parsing run off the ASGI event loop.
            import asyncio
            result = await asyncio.to_thread(self.service.execute, request, self.mode)
            status = result.status
            base_headers.update(result.headers)
            payload = b'' if status == 204 else dumps({'request_id': rid, 'data': result.data}).encode()
        except APIError as exc:
            status = exc.status
            base_headers.update(exc.headers)
            payload = dumps({'request_id': rid, 'error': {'code': exc.code, 'message': exc.code, 'details': exc.details, 'retryable': status in (429, 503)}}).encode()
        except Exception:
            # Do not serialize DB/SMTP/provider exceptions: they may include secrets.
            status = 503
            payload = dumps({'request_id': rid, 'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Service unavailable', 'details': {}, 'retryable': True}}).encode()
        if status != 204:
            base_headers['Content-Type'] = 'application/json; charset=utf-8'
        base_headers['Content-Length'] = str(len(payload))
        await send({'type': 'http.response.start', 'status': status, 'headers': [(k.lower().encode(), item.encode('latin1')) for k, v in base_headers.items()
                                                                                 for item in (v if isinstance(v, list) else [v])]})
        await send({'type': 'http.response.body', 'body': payload})


def build_app(mode, service, routes, lifespan=None, configure=None):
    """FastAPI app for one container: OpenAPI from `routes` (+ easyq_common.schemas), then the bounded ASGI
    transport mounted at '/'. `configure(app)` registers extra routes (backend WebSocket) before the mount."""
    import re
    from fastapi import FastAPI
    from . import schemas

    app = FastAPI(title='EasyQ ' + mode, version='0.3.0', lifespan=lifespan)

    def openapi():
        paths = {}
        for method, path, aid, summary in routes:
            parameters = [{'name': name, 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
                          for name in re.findall(r'\{([^}]+)\}', path)]
            if method != 'GET':
                parameters.append({'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string'}})
            operation = {'operationId': aid, 'summary': summary, 'parameters': parameters,
                'responses': {str(c): {'description': d} for c, d in [(200, 'OK'), (201, 'Created'), (202, 'Accepted'), (204, 'No Content'), (401, 'AUTH_REQUIRED'), (403, 'FORBIDDEN'), (409, 'State/version conflict'), (422, 'Validation error'), (429, 'Rate limited'), (503, 'SERVICE_UNAVAILABLE')]}}
            if method in ('POST', 'PATCH', 'PUT') and not path.endswith('/retries'):
                media = 'multipart/form-data' if path.endswith('/documents') else 'application/json'
                operation['requestBody'] = {'content': {media: {'schema': {'type': 'object'}}}}
            documented = schemas.operation(aid, method)
            if documented:
                parameters[:] = [p for p in parameters if p['in'] == 'path'] + documented.pop('parameters')
                operation.update(documented)
            paths.setdefault('/api/v1' + path, {})[method.lower()] = operation
        return {'openapi': '3.1.0', 'info': {'title': app.title, 'version': '0.3.0',
                'description': '요청 · 응답 필드는 인증(API-01~09) · 입장(API-18, 19)부터 문서화. 나머지는 docs/service/api-v0.3.md 참고.'},
                'paths': paths, 'components': {'schemas': schemas.components(), 'securitySchemes': {
                    'cookieAuth': {'type': 'apiKey', 'in': 'cookie', 'name': 'easyq_session'}}}}

    app.openapi = openapi
    if configure:
        configure(app)
    app.mount('/', HTTPApplication(service, mode))
    return app
