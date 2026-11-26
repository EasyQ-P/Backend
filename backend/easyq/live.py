"""WebSocket /api/v1/ws/sessions/{sid} (7.3.10): event delivery, HOST audio relay, STT, 30 s utterance chunks.

* Events: worker/backend store ws_events and NOTIFY; EventHub reads new rows (one poll for the whole process,
  so a missed NOTIFY is covered by reading after the last event_id) and hands each row to the connections
  allowed to see it. Recipients are re-checked against the DB (7.3.10 권한 재확인).
* Audio (6.2.5): only the lecture HOST, LIVE, with this lecture's STT consent, one active stream per lecture.
  Frames are relayed in memory to the STT adapter and never written to DB, disk, S3, logs or events.
* Finalised utterances are sent to the HOST (transcript.final) and grouped into ~30 s chunks
  (knowledge_chunks TRANSCRIPT, PENDING) with a TRANSCRIPT_INDEX job; the worker embeds and indexes them.

Process-local state (active streams) assumes one backend process, as in the compose file.
"""
import asyncio
import json
import struct
import time
from uuid import UUID

from .contracts import APIError, dt, dumps, obj, uid

FRAME_HEADER = struct.Struct('!16sIQ')   # stream UUID, sequence uint32, start_ms uint64 (network byte order)
MAX_FRAME_BYTES = 32 * 1024              # 7.3.10 음성 한도
BUFFER_FRAMES = 50                       # 5 s of 100 ms frames held in memory
ACK_EVERY = 10
CHUNK_MS = 30_000                        # F3-3 30 s utterance chunks
PING_SECONDS, IDLE_SECONDS, RECHECK_SECONDS = 20, 60, 30
STATE_SECONDS = 5                        # stream-state check (API-16 pause, STT consent withdrawal, lecture end)
CLOSE_TIMEOUT = 10                       # never let a stalled STT provider block stream cleanup
NOTICE_SECONDS = 1.0                     # at most one "frames dropped" notice per second
EVENT_LOOKBACK = 500                     # event ids re-read behind the newest one (late commits)
HOST_ONLY = {'document.updated', 'stt.status', 'transcript.final'}
EVERYONE = {'session.updated'}

run_sync = asyncio.to_thread             # tests may swap in an inline runner


class CloseConnection(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- events ----
class EventHub:
    """Identity ids are handed out at INSERT, not at COMMIT: a transaction holding id 7 may commit after id 8
    was already read. So each poll re-reads the last EVENT_LOOKBACK ids and delivers only ids not seen yet."""

    def __init__(self, service, interval=1.0, lookback=EVENT_LOOKBACK):
        self.service, self.interval, self.lookback = service, interval, lookback
        self.connections = set()
        self.last_event_id = None
        self.seen = set()

    def subscribe(self, connection):
        self.connections.add(connection)

    def unsubscribe(self, connection):
        self.connections.discard(connection)

    def _read(self):
        with self.service.db.transaction() as tx:
            if self.last_event_id is None:
                # Start at the end of the log; events already there count as delivered.
                row = tx.one('SELECT max(event_id) AS n FROM ws_events')
                self.last_event_id = row['n'] or 0
                self.seen = {r['event_id'] for r in tx.all('SELECT event_id FROM ws_events WHERE event_id>%s',
                                                          (max(0, self.last_event_id - self.lookback),))}
                return []
            floor = max(0, self.last_event_id - self.lookback)
            rows = tx.all('SELECT * FROM ws_events WHERE event_id>%s ORDER BY event_id LIMIT 2000', (floor,))
        fresh = [r for r in rows if r['event_id'] not in self.seen]
        for r in fresh:
            self.seen.add(r['event_id'])
        if rows:
            self.last_event_id = max(self.last_event_id, rows[-1]['event_id'])
        floor = max(0, self.last_event_id - self.lookback)
        self.seen = {i for i in self.seen if i > floor}
        return fresh

    async def poll_once(self):
        rows = await run_sync(self._read)
        for row in rows:
            for connection in list(self.connections):
                connection.offer(row)
        return len(rows)

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                pass  # DB hiccup: the next poll re-reads from the lookback floor.
            await asyncio.sleep(self.interval)


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


# ----------------------------------------------------------- connection ----
class LiveConnection:
    """Transport-independent WebSocket session. `socket` needs accept(), receive() -> {'text'|'bytes'|None},
    send_text(str) and close(code)."""

    streams = {}   # sid -> {'stream_id', 'connection', 'last_sequence', 'base_ms', 'origin_ms'} (process-local)

    def __init__(self, service, hub, stt_factory, socket, sid, headers, cookies):
        self.service, self.hub, self.stt_factory, self.socket = service, hub, stt_factory, socket
        self.sid, self.headers, self.cookies = sid, headers, cookies
        self.outbox = asyncio.Queue()
        self.principal = None
        self.role = None
        self.participant_id = None
        self.host_user_id = None
        self.audio = None
        self.last_seen = time.monotonic()
        self.state_wake = asyncio.Event()

    # -- authorisation ---------------------------------------------------
    def _authorize(self):
        if self.headers.get('origin') != self.service.settings.origin:
            raise APIError(403, 'FORBIDDEN')
        request = type('R', (), {'cookies': self.cookies})()
        with self.service.db.transaction() as tx:
            p = self.service.principal(tx, request)
            lecture = self.service.lecture(tx, self.sid, p)
            owner = bool(p['user_id']) and str(p['user_id']) == str(lecture['host_user_id'])
            participant = None if owner else self.service.participant(tx, self.sid, p)
            if lecture['status'] not in ('DRAFT', 'LIVE'):
                raise APIError(409, 'SESSION_ENDED')
            return p, 'HOST' if owner else 'AUDIENCE', participant, lecture

    async def authorize(self):
        try:
            p, role, participant, lecture = await run_sync(self._authorize)
        except APIError:
            return None
        self.principal, self.role = p, role
        self.participant_id = str(participant['participant_id']) if participant else None
        self.host_user_id = str(lecture['host_user_id'])
        return lecture

    async def still_allowed(self):
        try:
            await run_sync(self._authorize)
            return True
        except APIError:
            return False

    # -- outbound ----------------------------------------------------------
    def envelope(self, kind, data, event_id=None, version=None):
        return {'type': kind, 'event_id': event_id, 'session_id': self.sid, 'request_id': uid(),
                'resource_version': version, 'data': data}

    async def send_event(self, kind, data):
        await self.outbox.put(self.envelope(kind, data))

    def offer(self, row):
        """Called by EventHub for every new ws_events row; keep only what this connection may see."""
        kind, payload = row['event_type'], obj(row['payload'])
        mine = str(self.principal['auth_session_id']) if self.principal else None
        if row['session_id'] is None:
            if kind == 'auth.csrf_rotated' and payload.get('auth_session_id') == mine:
                self.outbox.put_nowait(self.envelope(kind, {}, row['event_id']))
            elif kind == 'session.revoked' and payload.get('auth_session_id') == mine:
                self.outbox.put_nowait(CloseConnection(1008))
            return
        if str(row['session_id']) != self.sid:
            return
        if kind == 'session.updated' and self.audio:
            self.state_wake.set()   # pause / STT consent withdrawal / end: re-check the stream right away
        if kind in HOST_ONLY and self.role != 'HOST':
            return
        if kind not in HOST_ONLY and kind not in EVERYONE:
            return  # question/answer events arrive with the v0.4+ question features
        self.outbox.put_nowait(self.envelope(kind, payload, row['event_id'], payload.get('version')))

    async def sender(self):
        while True:
            item = await self.outbox.get()
            if isinstance(item, CloseConnection):
                raise item
            await self.socket.send_text(dumps(item))

    async def heartbeat(self):
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(PING_SECONDS)
            now = time.monotonic()
            if now - self.last_seen > IDLE_SECONDS:
                raise CloseConnection(1000)
            await self.send_event('ping', {'timestamp': self.service.clock()})
            if now - last_check >= RECHECK_SECONDS:
                last_check = now
                if not await self.still_allowed():
                    raise CloseConnection(1008)

    async def stream_watch(self):
        """Own loop (not the 20 s ping loop): stop the stream within STATE_SECONDS, or at once on session.updated."""
        while True:
            try:
                await asyncio.wait_for(self.state_wake.wait(), STATE_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.state_wake.clear()
            if self.audio:
                await self.check_stream_state()

    # -- inbound -----------------------------------------------------------
    async def receiver(self):
        while True:
            message = await self.socket.receive()
            if message is None:
                return
            self.last_seen = time.monotonic()
            if 'bytes' in message and message['bytes'] is not None:
                await self.on_frame(message['bytes'])
                continue
            try:
                body = json.loads(message.get('text') or '')
                kind, data = body['type'], body.get('data') or {}
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                await self.send_error('VALIDATION_ERROR')
                continue
            if kind == 'ping':
                await self.send_event('pong', {'timestamp': self.service.clock()})
            elif kind == 'pong':
                continue
            elif kind == 'audio.start':
                await self.on_audio_start(data)
            elif kind == 'audio.stop':
                await self.on_audio_stop(data)
            else:
                await self.send_error('VALIDATION_ERROR')

    async def send_error(self, code, retryable=False):
        await self.send_event('error', {'code': code, 'message': code, 'retryable': retryable})

    # -- audio -------------------------------------------------------------
    def _start_stream(self, stream_id):
        """DB side of audio.start: HOST · LIVE · STT consent · stt_status ready, then RUNNING."""
        with self.service.db.transaction() as tx:
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (self.sid,))
            if not s or s['status'] != 'LIVE':
                raise APIError(409, 'SESSION_NOT_LIVE')
            consent = self.service.effective(tx, 'STT', user=self.principal['user_id'], sid=self.sid)
            if not consent or not consent['granted']:
                raise APIError(403, 'CONSENT_REQUIRED')
            if s['stt_status'] not in ('CONNECTING', 'RUNNING', 'DEGRADED'):
                raise APIError(409, 'SESSION_STATE_CONFLICT')  # PAUSED/STOPPED: resume with API-16 first
            if s['stt_status'] != 'RUNNING':
                tx.execute("UPDATE lecture_sessions SET stt_status='RUNNING',version=version+1 WHERE session_id=%s", (self.sid,))
                self.service.event(tx, 'session.updated', {'status': 'LIVE', 'stt_status': 'RUNNING', 'version': s['version'] + 1}, self.sid)
            # Chunk times are milliseconds since the lecture started, so "발화 21:05" labels stay comparable across streams.
            elapsed = int((self.service.clock() - dt(s['started_at'])).total_seconds() * 1000)
            return list(obj(s['terms']) or []), max(0, elapsed)

    def _set_stt_status(self, status, only_from):
        with self.service.db.transaction() as tx:
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (self.sid,))
            if s and s['stt_status'] in only_from:
                tx.execute('UPDATE lecture_sessions SET stt_status=%s,version=version+1 WHERE session_id=%s', (status, self.sid))
                self.service.event(tx, 'session.updated', {'status': s['status'], 'stt_status': status, 'version': s['version'] + 1}, self.sid)

    async def on_audio_start(self, data):
        if self.role != 'HOST':
            return await self.send_error('FORBIDDEN')
        try:
            stream_id = str(UUID(str(data.get('stream_id'))))
        except ValueError:
            return await self.send_error('VALIDATION_ERROR')
        if data.get('sample_rate') != 16000 or data.get('channels') != 1:
            return await self.send_error('VALIDATION_ERROR')
        if self.audio:   # restarting on this connection: finish the previous stream first
            old, self.audio = self.audio, None
            try:
                await asyncio.shield(old.close())
            except Exception:
                pass
        active = self.streams.get(self.sid)
        if active and active['connection'] is not None and active['connection'] is not self:
            return await self.send_error('SESSION_STATE_CONFLICT')   # 음성은 HOST의 활성 스트림 연결 1개만
        resumed = dict(active) if active and active['stream_id'] == stream_id else None
        # Reserve synchronously, before the first await, so a second HOST tab cannot pass the check too.
        reservation = {'stream_id': stream_id, 'connection': self,
                       'last_sequence': resumed['last_sequence'] if resumed else -1,
                       'base_ms': resumed['base_ms'] if resumed else None,
                       'origin_ms': resumed['origin_ms'] if resumed else None}
        self.streams[self.sid] = reservation

        def release():
            if self.streams.get(self.sid) is reservation:
                if active:
                    self.streams[self.sid] = {**active, 'connection': None}
                else:
                    self.streams.pop(self.sid, None)

        try:
            terms, elapsed = await run_sync(self._start_stream, stream_id)
        except APIError as exc:
            release()
            return await self.send_error(exc.code)
        except Exception:
            release()
            raise
        stream = AudioStream(self, stream_id, reservation['base_ms'] if resumed else elapsed)
        stream.last_sequence, stream.origin_ms = reservation['last_sequence'], reservation['origin_ms']
        reservation['base_ms'] = stream.base_ms
        try:
            await stream.open(terms)   # STT connection gets the lecture term dictionary as hints
        except Exception:
            release()
            await run_sync(self._set_stt_status, 'DEGRADED', ('RUNNING',))
            return await self.send_error('STT_UNAVAILABLE', retryable=True)
        self.audio = stream
        await self.send_event('audio.accepted', {'stream_id': stream_id, 'next_sequence': stream.last_sequence + 1})

    async def on_frame(self, raw):
        if len(raw) > MAX_FRAME_BYTES:
            raise CloseConnection(1009)
        if self.role != 'HOST' or not self.audio:
            return await self.send_error('FORBIDDEN')
        if len(raw) < FRAME_HEADER.size or (len(raw) - FRAME_HEADER.size) % 2:
            return await self.send_error('VALIDATION_ERROR')
        stream_bytes, sequence, start_ms = FRAME_HEADER.unpack_from(raw)
        if str(UUID(bytes=stream_bytes)) != self.audio.stream_id:
            return await self.send_error('VALIDATION_ERROR')
        await self.audio.frame(sequence, start_ms, raw[FRAME_HEADER.size:])
        entry = self.streams.get(self.sid)
        if entry and entry['connection'] is self:
            entry.update(last_sequence=self.audio.last_sequence, origin_ms=self.audio.origin_ms)
        if sequence % ACK_EVERY == ACK_EVERY - 1:
            await self.send_event('audio.ack', {'stream_id': self.audio.stream_id, 'sequence': self.audio.last_sequence})

    async def finish_stream(self, status, only_from):
        """Release the lecture's stream slot first, then flush and set stt_status. Errors never leave the
        slot locked or the status stale."""
        stream, self.audio = self.audio, None
        if not stream:
            return
        entry = self.streams.get(self.sid)
        if entry and entry['connection'] is self:
            entry['connection'] = None   # same stream_id may resume after a reconnect
        try:
            await stream.close()
        except Exception:
            pass   # the last chunk could not be stored; never block the lecture's audio on it
        try:
            await run_sync(self._set_stt_status, status, only_from)
        except Exception:
            pass

    async def stop_stream(self, status, only_from):
        """finish_stream that completes even if the calling task is cancelled (client gone, shutdown)."""
        if not self.audio:
            return
        await asyncio.shield(asyncio.ensure_future(self.finish_stream(status, only_from)))

    async def on_audio_stop(self, data):
        try:
            stream_id = str(UUID(str(data.get('stream_id'))))
        except ValueError:
            stream_id = None
        if self.role != 'HOST' or not self.audio or stream_id != self.audio.stream_id:
            return await self.send_error('VALIDATION_ERROR')
        await self.send_event('audio.ack', {'stream_id': self.audio.stream_id, 'sequence': self.audio.last_sequence})
        await self.stop_stream('PAUSED', ('RUNNING', 'DEGRADED'))

    async def check_stream_state(self):
        """API-16 pause, STT consent withdrawal or lecture end stop the stream (REST changes the DB).
        Anything but RUNNING/DEGRADED stops it, including a pause-then-resume that left CONNECTING."""
        def read():
            with self.service.db.transaction() as tx:
                return tx.one('SELECT status,stt_status FROM lecture_sessions WHERE session_id=%s', (self.sid,))
        s = await run_sync(read)
        if not s or s['status'] != 'LIVE' or s['stt_status'] not in ('RUNNING', 'DEGRADED'):
            await self.stop_stream('PAUSED', ())
            await self.send_event('stt.status', {'status': s['stt_status'] if s else 'STOPPED', 'indexed_until_ms': None,
                                                  'ingest_lag_ms': None})

    # -- lifecycle ---------------------------------------------------------
    async def run(self):
        lecture = await self.authorize()
        if not lecture:
            await self.socket.close(1008)
            return
        await self.socket.accept()
        ready = {'role': self.role, 'version': lecture['version']}
        if self.participant_id:
            ready['participant_id'] = self.participant_id
        await self.socket.send_text(dumps(self.envelope('connection.ready', ready, version=lecture['version'])))
        self.hub.subscribe(self)
        tasks = [asyncio.create_task(t) for t in (self.receiver(), self.sender(), self.heartbeat(), self.stream_watch())]
        code = 1000
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if isinstance(exc, CloseConnection):
                    code = exc.code
                elif exc is not None:
                    code = 1011
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.hub.unsubscribe(self)
            # A dropped connection keeps the stream resumable; stt_status shows it as unstable until audio.start.
            await self.stop_stream('DEGRADED', ('RUNNING',))
            # Deliver what is already queued (e.g. last transcript.final) before closing.
            while not self.outbox.empty():
                item = self.outbox.get_nowait()
                if not isinstance(item, CloseConnection):
                    try:
                        await self.socket.send_text(dumps(item))
                    except Exception:
                        break
            try:
                await self.socket.close(code)
            except Exception:
                pass


class StarletteSocket:
    """Adapter from a Starlette/FastAPI WebSocket to the LiveConnection socket interface."""

    def __init__(self, websocket):
        self.websocket = websocket

    async def accept(self):
        await self.websocket.accept()

    async def receive(self):
        message = await self.websocket.receive()
        if message['type'] == 'websocket.disconnect':
            return None
        return {'text': message.get('text'), 'bytes': message.get('bytes')}

    async def send_text(self, value):
        await self.websocket.send_text(value)

    async def close(self, code):
        await self.websocket.close(code)
