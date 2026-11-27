"""WebSocket protocol constants (7.3.10), the DB runner used from async code, and the close signal."""
import asyncio
import struct

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
EVERYONE = {'session.updated', 'public_question.updated'}
ASKER_ONLY = {'question.progress', 'answer.delta', 'answer.discarded'}          # routed by ws_events.question_id
ASKER_AND_HOST = {'question.updated', 'answer.completed', 'answer.retracted'}
MEMBERS_AND_HOST = {'summary.updated'}
run_sync = asyncio.to_thread             # tests may swap in an inline runner


class CloseConnection(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
