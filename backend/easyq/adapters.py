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


class GeminiLiveSTT:
    """Real-time STT (Gemini 3.5 Transcribe-Live, 6.1.3 · 6.2.5) through google-genai Live API.

    open(terms, on_final) returns a stream with async send(pcm, start_ms) / close(). Only the lecture term
    dictionary is sent as recognition hints. Audio stays in memory. The provider connection is reopened before
    its 10-minute limit. Not verified against the real provider in this repository (tests use a fake).
    """
    RECONNECT_SECONDS = 570

    def __init__(self, settings):
        self.settings = settings

    def open(self, terms, on_final):
        if not self.settings.stt_model or not self.settings.stt_api_key:
            raise APIError(503, 'STT_UNAVAILABLE')
        return _GeminiLiveStream(self.settings, list(terms), on_final, self.RECONNECT_SECONDS)


class _GeminiLiveStream:
    def __init__(self, settings, terms, on_final, reconnect_seconds):
        self.settings, self.terms, self.on_final, self.reconnect_seconds = settings, terms, on_final, reconnect_seconds
        self.context = self.session = self.reader = None
        self.opened_at = 0.0
        self.failed_at = None     # last failed connect, for reconnect backoff
        self.broken = False       # reader died: reconnect on the next frame
        self.audio_ms = None      # absolute ms of the next audio byte sent
        self.segment_start = None
        self.text = []

    async def _connect(self):
        import time
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.settings.stt_api_key)
        hint = ('Transcribe the lecturer exactly (Korean with English technical terms). Technical terms: '
                + ', '.join(self.terms)) if self.terms else 'Transcribe the lecturer exactly (Korean with English technical terms).'
        config = types.LiveConnectConfig(response_modalities=[types.Modality.TEXT],
                                         input_audio_transcription=types.AudioTranscriptionConfig(),
                                         system_instruction=hint)
        self.context = client.aio.live.connect(model=self.settings.stt_model, config=config)
        self.session = await self.context.__aenter__()
        self.opened_at = time.monotonic()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        # session.receive() ends at each turn_complete, so keep re-entering it for the whole connection.
        try:
            while True:
                async for message in self.session.receive():
                    content = getattr(message, 'server_content', None)
                    transcription = getattr(content, 'input_transcription', None) if content else None
                    if transcription and transcription.text:
                        if self.segment_start is None:
                            self.segment_start = self.audio_ms or 0
                        self.text.append(transcription.text)
                    if content and (getattr(transcription, 'finished', False) or getattr(content, 'turn_complete', False)):
                        await self._emit()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.broken = True   # surfaced by the next send() as an error -> DEGRADED, then reconnect

    async def _emit(self):
        if self.text:
            start, end = self.segment_start or 0, self.audio_ms or 0
            text, self.text, self.segment_start = ''.join(self.text).strip(), [], None
            if text:
                await self.on_final(start, max(end, start + 1), text)

    async def _disconnect(self):
        if self.session:
            try:
                await self.session.send_realtime_input(audio_stream_end=True)
                await asyncio.sleep(1)
            except Exception:
                pass
            if self.reader:
                self.reader.cancel()
            await self._emit()
            await self.context.__aexit__(None, None, None)
            self.context = self.session = self.reader = None

    RETRY_SECONDS = 2.0

    async def send(self, pcm, start_ms):
        import time
        from google.genai import types
        if self.session and (self.broken or time.monotonic() - self.opened_at > self.reconnect_seconds):
            broken, self.broken = self.broken, False
            try:
                await self._disconnect()
            except Exception:
                self.context = self.session = self.reader = None
            if broken:
                raise RuntimeError('STT stream lost')
        if not self.session:
            if self.failed_at is not None and time.monotonic() - self.failed_at < self.RETRY_SECONDS:
                raise RuntimeError('STT reconnect backoff')
            try:
                await self._connect()
            except Exception:
                self.failed_at = time.monotonic()
                raise
            self.failed_at = None
        self.audio_ms = start_ms
        await self.session.send_realtime_input(audio=types.Blob(data=pcm, mime_type='audio/pcm;rate=16000'))
        self.audio_ms = start_ms + len(pcm) // 32   # 16 kHz mono PCM16 = 32 bytes per ms

    async def close(self):
        await self._disconnect()


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
            MAIL_STARTTLS=s.smtp_starttls, MAIL_SSL_TLS=s.smtp_ssl,   # Gmail 587: STARTTLS; local Mailpit: neither
            USE_CREDENTIALS=bool(s.smtp_user), VALIDATE_CERTS=True)
        message = MessageSchema(subject=subject, recipients=[address], body=body, subtype=MessageType.plain)
        asyncio.run(FastMail(config).send_message(message))
