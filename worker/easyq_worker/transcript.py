"""TRANSCRIPT_INDEX (6.2.5): embed and index one ~30 s utterance chunk."""
import time

from easyq_common.contracts import APIError, dt, dumps

from .budget import reserve, settle
from .retrieval import check_vectors, keywords


class TranscriptJob:
    def transcript_index(self, job):
        """TRANSCRIPT_INDEX (6.2.5): embed and keyword-index one ~30 s utterance chunk, then READY."""
        with self.db.transaction() as tx:
            self.active(tx, job)
            chunk = tx.one("SELECT * FROM knowledge_chunks WHERE session_id=%s AND chunk_id=%s AND source_type='TRANSCRIPT'",
                           (job['session_id'], job['target_id']))
            if not chunk or chunk['index_status'] == 'READY':
                self.finish(tx, job, {'skipped': True})
                return
        usage_id = reserve(self.service, job)
        began = time.monotonic()
        try:
            vector = check_vectors(self.embedder.embed([chunk['content']]), 1)[0]
        except Exception:
            settle(self.service, usage_id, False, int((time.monotonic() - began) * 1000))
            raise
        settle(self.service, usage_id, True, int((time.monotonic() - began) * 1000))
        with self.db.transaction() as tx:
            self.active(tx, job)
            lecture = tx.one('SELECT status,stt_status FROM lecture_sessions WHERE session_id=%s', (job['session_id'],))
            if not lecture or lecture['status'] not in ('DRAFT', 'LIVE', 'ENDED'):
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            moment = self.service.clock()
            tx.execute("""UPDATE knowledge_chunks SET embedding=%s::vector,search_vector=to_tsvector('simple',%s),
                retrieval_config_version=%s,index_status='READY',indexed_at=%s WHERE session_id=%s AND chunk_id=%s""",
                (dumps(vector), keywords(chunk['content']), self.settings.retrieval_version, moment,
                 job['session_id'], job['target_id']))
            lag = max(0, int((moment - dt(chunk['captured_at'])).total_seconds() * 1000))
            self.service.event(tx, 'stt.status', {'status': lecture['stt_status'], 'indexed_until_ms': chunk['end_ms'],
                'ingest_lag_ms': lag}, str(job['session_id']))
            self.finish(tx, job, {'indexed_until_ms': chunk['end_ms']})
