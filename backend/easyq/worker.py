"""Durable v0.3 jobs. Work is claimed with SKIP LOCKED and fenced by lease_token."""
from datetime import timedelta
import json
import threading
import time

from .contracts import APIError, dt, dumps, obj, uid
from .retrieval import chunks, check_vectors, extract_pages, keywords, overlap_ratio
from .security import decrypt
from .budget import month_bounds, reserve, settle


class Worker:
    TYPES = ('INGEST', 'EMAIL_VERIFICATION', 'PASSWORD_RESET', 'AUTO_END', 'FINALIZE', 'AUTH_REVOKE', 'CLEANUP')

    def __init__(self, service, embedder, mailer, tokenizer=None):
        self.service, self.db, self.settings = service, service.db, service.settings
        self.embedder, self.mailer, self.tokenizer = embedder, mailer, tokenizer

    def claim(self):
        moment = self.service.clock()
        with self.db.transaction() as tx:
            marks = ','.join('%s' for _ in self.TYPES)
            row = tx.one(f"""SELECT * FROM jobs WHERE job_type IN ({marks}) AND
                ((status='QUEUED' AND available_at<=%s) OR (status='RUNNING' AND lease_until<%s))
                ORDER BY available_at,created_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED""", (*self.TYPES, moment, moment))
            if not row:
                return None
            lease = uid()
            tx.execute("""UPDATE jobs SET status='RUNNING',lease_token=%s,lease_until=%s,attempts=attempts+1
                WHERE job_id=%s""", (lease, moment + timedelta(minutes=5), row['job_id']))
            row.update(lease_token=lease, attempts=row['attempts'] + 1)
            return row

    def active(self, tx, job):
        current = tx.one('SELECT * FROM jobs WHERE job_id=%s FOR UPDATE', (job['job_id'],))
        if not current or current['status'] != 'RUNNING' or str(current['lease_token']) != str(job['lease_token']) or dt(current['lease_until']) <= self.service.clock():
            raise APIError(409, 'LEASE_LOST')
        return current

    def heartbeat(self, job):
        with self.db.transaction() as tx:
            self.active(tx, job)
            tx.execute('UPDATE jobs SET lease_until=%s WHERE job_id=%s AND lease_token=%s',
                (self.service.clock() + timedelta(minutes=5), job['job_id'], job['lease_token']))

    def finish(self, tx, job, result=None):
        self.active(tx, job)
        tx.execute("""UPDATE jobs SET status='SUCCEEDED',result=%s,finished_at=%s,lease_until=NULL
            WHERE job_id=%s AND lease_token=%s""", (dumps(result or {}), self.service.clock(), job['job_id'], job['lease_token']))

    def stage(self, job, stage):
        with self.db.transaction() as tx:
            self.active(tx, job)
            tx.execute("UPDATE documents SET status='PROCESSING',stage=%s,attempt_count=%s,error_code=NULL WHERE document_id=%s AND status<>'DELETING'",
                (stage, job['attempts'], job['target_id']))
            self.service.event(tx, 'document.updated', {'document_id': str(job['target_id']), 'status': 'PROCESSING', 'stage': stage}, str(job['session_id']))

    def ingest(self, job):
        self.stage(job, 'EXTRACTING')
        with self.db.transaction() as tx:
            self.active(tx, job)
            document = tx.one('SELECT * FROM documents WHERE document_id=%s', (job['target_id'],))
        if not document or document['status'] == 'DELETING':
            raise APIError(409, 'RESOURCE_NOT_FOUND')
        pages = extract_pages(document['file_name'], self.service.storage.get(document['object_key']))
        pieces = chunks(pages)
        if not pieces:
            raise APIError(422, 'DOCUMENT_NO_TEXT')
        self.stage(job, 'INDEXING')
        if self.tokenizer is None:
            from kiwipiepy import Kiwi
            self.tokenizer = Kiwi()
        vectors = []
        for start in range(0, len(pieces), 32):
            self.heartbeat(job)
            batch = pieces[start:start + 32]
            usage_id = reserve(self.service, job)
            began = time.monotonic()
            try:
                vector_batch = check_vectors(self.embedder.embed([p['content'] for p in batch]), len(batch))
            except Exception:
                settle(self.service, usage_id, False, int((time.monotonic() - began) * 1000))
                raise
            settle(self.service, usage_id, True, int((time.monotonic() - began) * 1000))
            vectors.extend(vector_batch)
        # Terms are a later milestone; v0.3 stores only the shared lexical/vector index.
        with self.db.transaction() as tx:
            self.active(tx, job)
            lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (job['session_id'],))
            current = tx.one('SELECT * FROM documents WHERE document_id=%s FOR UPDATE', (job['target_id'],))
            if not lecture or lecture['status'] not in ('DRAFT', 'LIVE') or not current or current['status'] == 'DELETING':
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            # Upsert stable source keys: lease reclaim or retry cannot duplicate chunks.
            for piece, vector in zip(pieces, vectors):
                tx.execute("""INSERT INTO knowledge_chunks(chunk_id,session_id,document_id,source_type,source_key,page_no,
                    content,embedding,search_vector,retrieval_config_version,index_status,captured_at,indexed_at)
                    VALUES(%s,%s,%s,'DOCUMENT',%s,%s,%s,%s::vector,to_tsvector('simple',%s),%s,'READY',%s,%s)
                    ON CONFLICT(session_id,source_key) DO UPDATE SET content=excluded.content,embedding=excluded.embedding,
                    search_vector=excluded.search_vector,retrieval_config_version=excluded.retrieval_config_version,
                    index_status='READY',indexed_at=excluded.indexed_at""",
                    (uid(), job['session_id'], job['target_id'], f'{job["target_id"]}:{piece["page_no"]}:{piece["ordinal"]}',
                     piece['page_no'], piece['content'], dumps(vector), keywords(piece['content'], self.tokenizer),
                     self.settings.retrieval_version, self.service.clock(), self.service.clock()))
            tx.execute("UPDATE documents SET status='READY',page_count=%s,chunk_count=%s,error_code=NULL WHERE document_id=%s",
                (len(pages), len(pieces), job['target_id']))
            # Until the AI team's evaluation reference sync exists, never claim the evaluation check succeeded.
            references = tx.all('SELECT normalized_text,eval_slide_version FROM eval_reference_chunks WHERE eval_slide_version=%s', (self.settings.reference_version,)) if self.settings.reference_version and self.settings.reference_retrieval_version == self.settings.retrieval_version else []
            if references:
                all_chunks = tx.all("SELECT content FROM knowledge_chunks WHERE session_id=%s AND source_type='DOCUMENT' AND index_status='READY'", (job['session_id'],))
                matched = sum(any(overlap_ratio(p['content'], r['normalized_text']) >= .8 for r in references) for p in all_chunks)
                detected = bool(all_chunks) and matched / len(all_chunks) >= .3
                detection = {'check_status': 'CHECKED', 'matched_chunks': matched, 'total_chunks': len(all_chunks),
                    'match_ratio': matched / len(all_chunks) if all_chunks else 0, 'reference_version': references[0]['eval_slide_version'],
                    'retrieval_config_version': self.settings.retrieval_version, 'checked_at': self.service.clock(), 'auto_detected': detected}
                # Preserve a manual evaluation mark; automated checks cannot clear it.
                before = obj(lecture['eval_detection'])
                detection['manual_marked'] = bool(before.get('manual_marked'))
                tx.execute('UPDATE lecture_sessions SET eval_detection=%s,is_eval_lecture=%s WHERE session_id=%s',
                    (dumps(detection), detected or detection['manual_marked'], job['session_id']))
            else:
                detection = obj(lecture['eval_detection'])
                detection['check_status'] = 'PENDING'
                tx.execute('UPDATE lecture_sessions SET eval_detection=%s WHERE session_id=%s', (dumps(detection), job['session_id']))
            self.service.event(tx, 'document.updated', {'document_id': str(job['target_id']), 'status': 'READY', 'page_count': len(pages), 'chunk_count': len(pieces)}, str(job['session_id']))
            self.finish(tx, job, {'page_count': len(pages), 'chunk_count': len(pieces)})

    def mail(self, job):
        reset = job['job_type'] == 'PASSWORD_RESET'
        table, idcol = ('password_resets', 'reset_id') if reset else ('email_verifications', 'verification_id')
        with self.db.transaction() as tx:
            self.active(tx, job)
            r = tx.one(f'SELECT * FROM {table} WHERE {idcol}=%s FOR UPDATE', (job['target_id'],))
            if not r or r['consumed_at'] or r['revoked_at'] or dt(r['expires_at']) <= self.service.clock() or not r['delivery_ciphertext']:
                self.finish(tx, job, {'skipped': True})
                return
            user = tx.one('SELECT * FROM users WHERE user_id=%s', (r['user_id'],))
            raw = decrypt(self.settings.encryption_key, r['delivery_ciphertext'])
            address = user['email']
        body = self.settings.origin + '/reset-password#token=' + raw if reset else 'EasyQ 가입 인증 코드: ' + raw
        self.mailer.send(address, 'EasyQ 비밀번호 재설정' if reset else 'EasyQ 이메일 인증', body)
        with self.db.transaction() as tx:
            self.active(tx, job)
            if reset:
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL WHERE {idcol}=%s', (job['target_id'],))
            else:
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL,sent_at=%s WHERE {idcol}=%s', (self.service.clock(), job['target_id']))
            self.finish(tx, job)

    def cleanup(self, tx, job):
        payload = obj(job['payload'])
        if payload.get('scope') != 'TRAINING':
            raise APIError(422, 'JOB_UNSUPPORTED')
        # Delete only unmigrated candidates. MERGING requires AI-side reconciliation in a later milestone.
        sql = "SELECT c.candidate_id,c.review_status FROM feedback_candidates c JOIN candidate_provenance p ON p.candidate_id=c.candidate_id WHERE "
        if payload.get('user_id'):
            sql += """(p.actor_user_id=%s OR p.session_id IN (SELECT session_id FROM lecture_sessions WHERE host_user_id=%s)
                OR p.question_id IN (SELECT q.question_id FROM questions q JOIN participants a ON a.participant_id=q.participant_id WHERE a.user_id=%s))"""
            args = [payload['user_id']] * 3
        else:
            sql += 'p.actor_participant_id=%s'
            args = [payload['participant_id']]
        if payload.get('session_id'):
            sql += ' AND p.session_id=%s'
            args.append(payload['session_id'])
        candidates = tx.all(sql, tuple(args))
        if any(c['review_status'] == 'MERGING' for c in candidates):
            raise APIError(409, 'MERGE_RECONCILIATION_REQUIRED')
        for c in candidates:
            tx.execute('DELETE FROM feedback_candidates WHERE candidate_id=%s', (c['candidate_id'],))

    def run_one(self):
        job = self.claim()
        if not job:
            return False
        stop = threading.Event()

        def renew():
            while not stop.wait(30):
                try:
                    self.heartbeat(job)
                except Exception:
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            kind = job['job_type']
            if kind == 'INGEST':
                self.ingest(job)
            elif kind in ('EMAIL_VERIFICATION', 'PASSWORD_RESET'):
                self.mail(job)
            elif kind == 'AUTH_REVOKE':
                self.service.guest_client.revoke({**obj(job['payload']), 'request_id': str(job['job_id'])})
                with self.db.transaction() as tx:
                    self.finish(tx, job)
            else:
                with self.db.transaction() as tx:
                    self.active(tx, job)
                    if kind == 'AUTO_END':
                        s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (job['session_id'],))
                        if s and s['status'] == 'LIVE':
                            self.service.end(tx, s, 'AUTO_TIMEOUT')
                            self.service.event(tx, 'session.updated', {'status': 'ENDED', 'end_reason': 'AUTO_TIMEOUT'}, str(job['session_id']))
                    elif kind == 'FINALIZE':
                        # QA, STT and summary generation belong to later milestones, so handoff is explicit.
                        self.finish(tx, job, {'handoff': 'QA_STT_SUMMARY_NEXT_MILESTONE'})
                        return True
                    elif kind == 'CLEANUP':
                        self.cleanup(tx, job)
                    self.finish(tx, job)
        except Exception as exc:
            code = exc.code if isinstance(exc, APIError) else 'WORKER_DEPENDENCY_UNAVAILABLE'
            if code != 'LEASE_LOST':
                with self.db.transaction() as tx:
                    try:
                        self.active(tx, job)
                    except APIError:
                        return True
                    permanent = code in ('DOCUMENT_INVALID', 'DOCUMENT_NO_TEXT', 'SESSION_STATE_CONFLICT', 'RESOURCE_NOT_FOUND')
                    held = code in ('BUDGET_EXCEEDED', 'MERGE_RECONCILIATION_REQUIRED')
                    failed = permanent or job['attempts'] >= 3
                    tx.execute("""UPDATE jobs SET status=%s,error_code=%s,available_at=%s,lease_until=NULL,finished_at=%s
                        WHERE job_id=%s AND lease_token=%s""", ('HELD' if held else ('FAILED' if failed else 'QUEUED'), code,
                        month_bounds(self.service.clock())[1] if held else self.service.clock() + timedelta(seconds=30 * 2 ** (job['attempts'] - 1)), self.service.clock() if failed or held else None,
                        job['job_id'], job['lease_token']))
                    if job['job_type'] == 'INGEST':
                        # 6.3.5: a budget-held document stays PENDING with error_code BUDGET_EXCEEDED until the cap is lifted.
                        doc_status = 'FAILED' if failed and not held else 'PENDING'
                        tx.execute('UPDATE documents SET status=%s,error_code=%s WHERE document_id=%s', (doc_status, code, job['target_id']))
                        self.service.event(tx, 'document.updated', {'document_id': str(job['target_id']), 'status': doc_status, 'error_code': code}, str(job['session_id']))
        finally:
            stop.set()
            heartbeat.join(timeout=1)
        return True

    def maintenance(self):
        """Only unauthenticated signup credentials expire in v0.3; preserve member records."""
        with self.db.transaction() as tx:
            self.service.rate(tx, 'maintenance', 'worker', 1000)
            pending = tx.all("SELECT user_id FROM users WHERE status='PENDING_VERIFICATION' AND created_at<%s", (self.service.clock() - timedelta(hours=24),))
            for user in pending:
                tx.execute("DELETE FROM jobs WHERE requested_by=%s AND job_type IN ('EMAIL_VERIFICATION','PASSWORD_RESET')", (user['user_id'],))
                tx.execute("DELETE FROM users WHERE user_id=%s AND status='PENDING_VERIFICATION' AND created_at<%s", (user['user_id'], self.service.clock() - timedelta(hours=24)))
            for table in ('email_verifications', 'password_resets'):
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL WHERE expires_at<%s', (self.service.clock(),))
            tx.execute('DELETE FROM auth_rate_limits WHERE expires_at<%s', (self.service.clock() - timedelta(days=1),))
            setting = tx.one("SELECT * FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET'")
            held = tx.all("SELECT * FROM jobs WHERE status='HELD' AND error_code='BUDGET_EXCEEDED' FOR UPDATE SKIP LOCKED")
            for job in held:
                changed = setting and (not setting['hard_cap_enabled'] or (job['finished_at'] and dt(setting['updated_at']) > dt(job['finished_at'])))
                if dt(job['available_at']) <= self.service.clock() or changed:
                    tx.execute("UPDATE jobs SET status='QUEUED',attempts=0,error_code=NULL,available_at=%s,finished_at=NULL WHERE job_id=%s", (self.service.clock(), job['job_id']))


def main():
    from .adapters import AuthClient, GeminiEmbedder, Mailer, storage_from
    from .config import Settings
    from .db import Database
    from .service import Service
    s = Settings.from_env()
    service = Service(Database(s.database_url), s, storage_from(s), AuthClient(s, worker=True))
    worker = Worker(service, GeminiEmbedder(s), Mailer(s))
    last = 0
    while True:
        if time.monotonic() - last >= 60:
            worker.maintenance()
            last = time.monotonic()
        if not worker.run_one():
            time.sleep(1)


if __name__ == '__main__':
    main()
