"""HOST audio stream (6.2.5): frame dedupe, 5 s buffer, STT relay, ~30 s utterance chunks and their storage."""
import asyncio
import time

from easyq_common.contracts import uid

from .protocol import BUFFER_FRAMES, CHUNK_MS, CLOSE_TIMEOUT, NOTICE_SECONDS, run_sync


# ------------------------------------------------------------ utterances ----
class TranscriptWindow:
    """Groups finalised utterances into ~30 s chunks keyed by (stream, first start_ms)."""

    def __init__(self, store, chunk_ms=CHUNK_MS):
        self.store, self.chunk_ms = store, chunk_ms
        self.parts = []

    async def add(self, start_ms, end_ms, text):
        if text.strip():
            self.parts.append((start_ms, end_ms, ' '.join(text.split())))
        if self.parts and self.parts[-1][1] - self.parts[0][0] >= self.chunk_ms:
            await self.flush()

    async def flush(self):
        if not self.parts:
            return
        parts, self.parts = self.parts, []
        await self.store(parts[0][0], max(p[1] for p in parts), ' '.join(p[2] for p in parts))


class AudioStream:
    """One HOST stream: dedupes frames, buffers <= 5 s, relays PCM to STT, chunks finals."""

    def __init__(self, connection, stream_id, base_ms):
        self.connection, self.stream_id, self.base_ms = connection, stream_id, base_ms
        self.origin_ms = None
        self.last_sequence = -1
        self.queue = asyncio.Queue(maxsize=BUFFER_FRAMES)
        self.window = TranscriptWindow(self.store_chunk)
        self.stt = None
        self.pump = None
        self.dropped = 0
        self.degraded = False
        self.last_notice = 0.0

    async def open(self, terms):
        self.stt = self.connection.stt_factory.open(terms, self.on_final)
        self.pump = asyncio.create_task(self.relay())

    def absolute(self, start_ms):
        if self.origin_ms is None:
            self.origin_ms = start_ms
        return self.base_ms + max(0, start_ms - self.origin_ms)

    async def frame(self, sequence, start_ms, pcm):
        if sequence <= self.last_sequence:
            return False  # 7.3.10 중복 처리: stream_id · sequence
        missing = sequence - self.last_sequence - 1 if self.last_sequence >= 0 else 0
        self.last_sequence = sequence
        try:
            self.queue.put_nowait((self.absolute(start_ms), pcm))
        except asyncio.QueueFull:
            missing += 1
        if missing:
            self.dropped += missing
            now = time.monotonic()
            if now - self.last_notice >= NOTICE_SECONDS:   # 누락 구간 알림, debounced
                self.last_notice = now
                await self.connection.send_event('stt.status', {'status': 'DEGRADED', 'indexed_until_ms': None,
                    'ingest_lag_ms': None, 'error_code': 'STT_UNAVAILABLE', 'missing_frames': self.dropped})
        return True

    async def set_degraded(self, degraded):
        """Mirror STT health into lecture_sessions.stt_status once per change (REST and audience see it)."""
        if degraded == self.degraded:
            return
        self.degraded = degraded
        status = 'DEGRADED' if degraded else 'RUNNING'
        await self.connection.send_event('stt.status', {'status': status, 'indexed_until_ms': None, 'ingest_lag_ms': None,
                                                         'error_code': 'STT_UNAVAILABLE' if degraded else None})
        try:
            await run_sync(self.connection._set_stt_status, status, ('RUNNING',) if degraded else ('DEGRADED',))
        except Exception:
            pass

    async def relay(self):
        while True:
            item = await self.queue.get()
            if item is None:
                break
            try:
                await self.stt.send(item[1], item[0])
            except Exception:
                await self.set_degraded(True)
            else:
                await self.set_degraded(False)

    async def on_final(self, start_ms, end_ms, text):
        await self.connection.send_event('transcript.final', {'start_ms': start_ms, 'end_ms': end_ms, 'text': text})
        await self.window.add(start_ms, end_ms, text)

    async def store_chunk(self, start_ms, end_ms, text):
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        await run_sync(save_transcript_chunk, self.connection.service, self.connection.sid,
                       self.connection.host_user_id, self.stream_id, start_ms, end_ms, text)

    async def close(self):
        """audio.stop or disconnect: finish the remaining confirmed speech, then stop input.
        Bounded by CLOSE_TIMEOUT so a stalled provider cannot hang the connection cleanup."""
        if self.pump:
            try:
                await asyncio.wait_for(self.queue.put(None), CLOSE_TIMEOUT)
                await asyncio.wait_for(self.pump, CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                self.pump.cancel()
            self.pump = None
        if self.stt:
            try:
                await asyncio.wait_for(self.stt.close(), CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            self.stt = None
        await self.window.flush()


def save_transcript_chunk(service, sid, host_user_id, stream_id, start_ms, end_ms, text):
    """Store one PENDING TRANSCRIPT chunk and its TRANSCRIPT_INDEX job in one transaction.
    source_key = stream:start_ms, so a reconnect re-sending the same speech cannot duplicate it.
    Nothing is stored once the HOST has withdrawn this lecture's STT consent (7.1.3)."""
    with service.db.transaction() as tx:
        lecture = tx.one('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))
        if not lecture or lecture['status'] not in ('LIVE', 'ENDED'):
            return None
        consent = service.effective(tx, 'STT', user=host_user_id, sid=sid)
        if not consent or not consent['granted']:
            return None
        chunk_id = uid()
        row = tx.one("""INSERT INTO knowledge_chunks(chunk_id,session_id,source_type,source_key,start_ms,end_ms,content,
            search_vector,retrieval_config_version,index_status,captured_at)
            VALUES(%s,%s,'TRANSCRIPT',%s,%s,%s,%s,to_tsvector('simple',''),%s,'PENDING',%s)
            ON CONFLICT(session_id,source_key) DO NOTHING RETURNING chunk_id""",
            (chunk_id, sid, f'{stream_id}:{start_ms}', start_ms, end_ms, text, service.settings.retrieval_version, service.clock()))
        if not row:
            return None
        service.job(tx, 'TRANSCRIPT_INDEX', chunk_id, 'transcript:' + chunk_id, {}, user=host_user_id, sid=sid)
        return chunk_id
