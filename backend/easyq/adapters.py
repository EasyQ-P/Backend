"""Production-only boundaries; tests supply deterministic fakes explicitly."""
from pathlib import Path
import asyncio
import json
import urllib.request

from .contracts import APIError, dumps


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root) or p == self.root:
            raise ValueError('Invalid object key')
        return p

    def put(self, key, content, media):
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def get(self, key):
        return self.path(key).read_bytes()

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket):
        import boto3
        if not bucket:
            raise RuntimeError('S3_BUCKET is required')
        self.client, self.bucket = boto3.client('s3'), bucket

    def put(self, key, content, media):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=media, ServerSideEncryption='AES256')

    def get(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response['Body'] as body:
            return body.read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)


def storage_from(settings):
    if settings.storage_backend == 'local':
        return LocalStorage(settings.storage_root)
    if settings.storage_backend == 's3':
        return S3Storage(settings.s3_bucket)
    raise RuntimeError('STORAGE_BACKEND must be local or s3')


class AuthClient:
    def __init__(self, settings, worker=False):
        self.settings = settings
        self.key = settings.worker_internal_key if worker else settings.internal_key

    def call(self, path, body):
        request = urllib.request.Request(self.settings.auth_url.rstrip('/') + '/internal/v1/' + path,
            data=dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Service-Key': self.key}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)['data']

    def issue(self, pid, request_id):
        return self.call('guest-sessions', {'participant_id': pid, 'request_id': request_id})

    def revoke(self, body):
        return self.call('session-revocations', body)

    def renew(self, auth_session_id, session_id, request_id):
        return self.call('host-session-renewals', {'auth_session_id': auth_session_id, 'session_id': session_id, 'request_id': request_id})


class GeminiEmbedder:
    """Explicitly configured Gemini model; fail rather than emit placeholder vectors."""
    def __init__(self, settings):
        self.settings = settings

    def embed(self, texts):
        s = self.settings
        if not s.embedding_url or not s.embedding_key or not s.embedding_model:
            raise APIError(503, 'EMBEDDING_NOT_CONFIGURED')
        body = {'requests': [{'model': s.embedding_model, 'content': {'parts': [{'text': t}]},
            'taskType': 'RETRIEVAL_DOCUMENT', 'outputDimensionality': 768} for t in texts]}
        request = urllib.request.Request(s.embedding_url, data=dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'x-goog-api-key': s.embedding_key}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.load(response)
            return [v['values'] for v in data['embeddings']]
        except Exception:
            raise APIError(503, 'EMBEDDING_PROVIDER_UNAVAILABLE') from None


class Mailer:
    def __init__(self, settings):
        self.settings = settings

    def send(self, address, subject, body):
        from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
        s = self.settings
        if not s.smtp_host or not s.smtp_from:
            raise APIError(503, 'SMTP_NOT_CONFIGURED')
        config = ConnectionConfig(MAIL_USERNAME=s.smtp_user, MAIL_PASSWORD=s.smtp_password,
            MAIL_FROM=s.smtp_from, MAIL_PORT=s.smtp_port, MAIL_SERVER=s.smtp_host,
            MAIL_STARTTLS=s.smtp_starttls, MAIL_SSL_TLS=not s.smtp_starttls,
            USE_CREDENTIALS=bool(s.smtp_user), VALIDATE_CERTS=True)
        message = MessageSchema(subject=subject, recipients=[address], body=body, subtype=MessageType.plain)
        asyncio.run(FastMail(config).send_message(message))
