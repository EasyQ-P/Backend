"""WebSocket /api/v1/ws/sessions/{sid} (7.3.10): event delivery, HOST audio relay, STT, 30 s utterance chunks.

* Events: worker/backend store ws_events and NOTIFY; EventHub reads new rows (one poll for the whole process,
  so a missed NOTIFY is covered by reading after the last event_id) and hands each row to the connections
  allowed to see it. Recipients are re-checked against the DB (7.3.10 권한 재확인).
* Audio (6.2.5): only the lecture HOST, LIVE, with this lecture's STT consent, one active stream per lecture.
  Frames are relayed in memory to the STT adapter and never written to DB, disk, S3, logs or events.
* Finalised utterances are sent to the HOST (transcript.final) and grouped into ~30 s chunks
  (knowledge_chunks TRANSCRIPT, PENDING) with a TRANSCRIPT_INDEX job; the worker embeds and indexes them.

Process-local state (active streams) assumes one backend process, as in the compose file."""
from .connection import LiveConnection, StarletteSocket
from .events import EventHub
from .protocol import FRAME_HEADER, CloseConnection

__all__ = ['EventHub', 'LiveConnection', 'StarletteSocket', 'FRAME_HEADER', 'CloseConnection']
