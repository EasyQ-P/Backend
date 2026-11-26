"""Real-time STT adapter (Gemini 3.5 Transcribe-Live via google-genai Live API, 6.2.5)."""
import asyncio

from easyq_common.contracts import APIError


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
