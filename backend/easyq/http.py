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


ROUTES = [
    ('auth', 'POST', '/users', 'API-01', '회원가입'),
    ('auth', 'POST', '/auth/email-verifications', 'API-02', '가입 인증 메일 재발송'),
    ('auth', 'POST', '/auth/email-verifications/confirm', 'API-03', '이메일 인증'),
    ('auth', 'POST', '/auth/sessions', 'API-04', '로그인'),
    ('auth', 'DELETE', '/auth/sessions/current', 'API-05', '로그아웃'),
    ('auth', 'POST', '/auth/csrf-tokens', 'API-06', 'CSRF 복구'),
    ('auth', 'POST', '/auth/password-reset-requests', 'API-07', '비밀번호 재설정 메일 요청'),
    ('auth', 'POST', '/auth/password-resets', 'API-08', '비밀번호 재설정'),
    ('auth', 'GET', '/me', 'API-09', '현재 회원 조회: 로그인 복구 지원'),
    ('backend', 'POST', '/me/consents', 'API-11', '계정 동의'),
    ('backend', 'POST', '/sessions', 'API-13', '강연 생성'),
    ('backend', 'GET', '/sessions/{sid}', 'API-15', '상태와 현재 권한'),
    ('backend', 'PATCH', '/sessions/{sid}', 'API-16', '강연 수정/시작/종료'),
    ('backend', 'POST', '/sessions/{sid}/join-links', 'API-17', '입장 QR 재발급'),
    ('backend', 'GET', '/join-codes/{code}', 'API-18', '입장 전 강연 확인'),
    ('backend', 'POST', '/participations', 'API-19', '회원/게스트 입장'),
    ('backend', 'POST', '/sessions/{sid}/consents', 'API-20', '강연 동의'),
    ('backend', 'POST', '/sessions/{sid}/documents', 'API-21', '교안 접수'),
    ('backend', 'GET', '/sessions/{sid}/documents', 'API-22', '교안 처리 상태: 처리 검증 지원'),
    ('backend', 'GET', '/jobs/{job_id}', 'API-41', '작업 상태: 비동기 작업 검증 지원'),
]


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
            payload = dumps({'request_id': rid, 'error': {'code': 'DEPENDENCY_UNAVAILABLE', 'message': 'Dependency unavailable', 'details': {}, 'retryable': True}}).encode()
        if status != 204:
            base_headers['Content-Type'] = 'application/json; charset=utf-8'
        base_headers['Content-Length'] = str(len(payload))
        # A header value may be a list (several Set-Cookie lines in one response).
        raw_headers = [(k.lower().encode(), item.encode('latin1')) for k, v in base_headers.items() for item in (v if isinstance(v, list) else [v])]
        await send({'type': 'http.response.start', 'status': status, 'headers': raw_headers})
        await send({'type': 'http.response.body', 'body': payload})


def create_app(mode='backend', service=None):
    from fastapi import FastAPI
    if service is None:
        from .config import Settings
        from .db import Database
        from .adapters import AuthClient, storage_from
        from .service import Service
        settings = Settings.from_env()
        service = Service(Database(settings.database_url), settings, storage_from(settings), AuthClient(settings))
    app = FastAPI(title='EasyQ ' + mode, version='0.3.0')

    def openapi():
        paths = {}
        for target, method, path, aid, summary in ROUTES:
            if target != mode:
                continue
            parameters = []
            import re
            for name in re.findall(r'\{([^}]+)\}', path):
                parameters.append({'name': name, 'in': 'path', 'required': True, 'schema': {'type': 'string'}})
            if method != 'GET':
                parameters.append({'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string'}})
            operation = {'operationId': aid, 'summary': summary, 'parameters': parameters,
                'responses': {str(c): {'description': d} for c, d in [(200, 'OK'), (201, 'Created'), (202, 'Accepted'), (204, 'No Content'), (401, 'AUTH_REQUIRED'), (403, 'FORBIDDEN'), (409, 'State/version conflict'), (422, 'Validation error'), (429, 'Rate limited'), (503, 'Dependency unavailable')]}}
            if method in ('POST', 'PATCH'):
                media = 'multipart/form-data' if path.endswith('/documents') else 'application/json'
                operation['requestBody'] = {'content': {media: {'schema': {'type': 'object'}}}}
            paths.setdefault('/api/v1' + path, {})[method.lower()] = operation
        return {'openapi': '3.1.0', 'info': {'title': app.title, 'version': '0.3.0'}, 'paths': paths}

    app.openapi = openapi
    app.mount('/', HTTPApplication(service, mode))
    return app
