"""One WebSocket connection: authorisation, outbound events, ping and re-checks, HOST audio control."""
import asyncio
import json
import time
from uuid import UUID

from easyq_common.contracts import APIError, dt, dumps, obj, uid

from . import protocol
from .audio import AudioStream
from .protocol import (ACK_EVERY, EVERYONE, FRAME_HEADER, HOST_ONLY, IDLE_SECONDS, MAX_FRAME_BYTES, PING_SECONDS,
                       RECHECK_SECONDS, CloseConnection, run_sync)


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
                await asyncio.wait_for(self.state_wake.wait(), protocol.STATE_SECONDS)
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
