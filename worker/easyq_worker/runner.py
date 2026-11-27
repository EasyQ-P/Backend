"""Job runner: claim with SKIP LOCKED, lease/heartbeat, finish, retry and failure codes, maintenance."""
from datetime import datetime, timedelta, timezone
import sys
import threading

from easyq_common.contracts import APIError, dt, dumps, obj, uid

from .budget import month_bounds


class WorkerBase:
    TYPES = ('TRANSCRIPT_INDEX', 'INGEST', 'EMAIL_VERIFICATION', 'PASSWORD_RESET', 'AUTO_END', 'FINALIZE', 'AUTH_REVOKE', 'CLEANUP')

    MAX_ATTEMPTS = 3

    # Job mixins add {job_type: method_name} here (JOB_HANDLERS = {...}); the method takes the claimed job row and
    # runs its own transactions (self.active / self.finish with the lease token). Collected over the MRO.
    JOB_HANDLERS = {}

    @classmethod
    def handlers(cls):
        table = {}
        for klass in reversed(cls.__mro__):
            table.update(klass.__dict__.get('JOB_HANDLERS', {}))
        return table

    @classmethod
    def job_types(cls):
        return tuple(dict.fromkeys(cls.TYPES + tuple(cls.handlers())))

    # Failures caused by the original file or a finished lecture are not retried.
    PERMANENT = ('DOCUMENT_INVALID', 'DOCUMENT_NO_TEXT', 'SESSION_STATE_CONFLICT', 'RESOURCE_NOT_FOUND')

    def __init__(self, service, embedder, mailer, tokenizer=None):
        self.service, self.db, self.settings = service, service.db, service.settings
        self.embedder, self.mailer, self.tokenizer = embedder, mailer, tokenizer
        self.token_spans = None  # slide chunk token counter; loaded on first INGEST (tests inject a stub)

    @staticmethod
    def error_code(job_type, internal):
        """Persisted error_code must be an 8.4.2 code (jobs.error_code is shown by API-22 · API-41)."""
        if internal in ('BUDGET_EXCEEDED', 'SESSION_STATE_CONFLICT', 'RESOURCE_NOT_FOUND'):
            return internal
        if job_type == 'INGEST':
            return 'DOCUMENT_PARSE_FAILED' if internal in ('DOCUMENT_INVALID', 'DOCUMENT_NO_TEXT') else 'INDEX_BUILD_FAILED'
        if job_type == 'TRANSCRIPT_INDEX':
            return 'INDEX_BUILD_FAILED'
        if job_type in ('EMAIL_VERIFICATION', 'PASSWORD_RESET'):
            return 'EMAIL_DELIVERY_FAILED'
        if job_type == 'AUTH_REVOKE':
            return 'AUTH_SERVICE_UNAVAILABLE'
        if job_type in ('PURGE', 'CLEANUP'):
            return 'DELETION_FAILED'   # 8.4.2: access stays blocked, retried
        return 'SERVICE_UNAVAILABLE'

    def log_job_failure(self, job, internal, code, final, held, exc):
        """One JSON line per failed attempt on stderr (8.3 common fields). Exception text is NOT written:
        provider/SMTP messages can contain e-mail addresses or credentials; only the class name is kept."""
        event = 'email.verification_failed' if job['job_type'] == 'EMAIL_VERIFICATION' else 'job.failed'
        line = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': 'ERROR' if final else 'WARNING',
                'service': 'worker', 'event': event, 'request_id': None, 'trace_id': None, 'code': code,
                'message': 'job held' if held else ('job failed' if final else 'job will be retried'),
                'meta': {'job_id': str(job['job_id']), 'job_type': job['job_type'], 'attempts': job['attempts'],
                         'final': final, 'held': held, 'cause': internal, 'exception': type(exc).__name__}}
        print(dumps(line), file=sys.stderr, flush=True)

    def record_failure(self, tx, job, code, final):
        if job['job_type'] == 'INGEST':
            status = 'FAILED' if final else 'PENDING'
            # A document being deleted (DELETING) must never come back as FAILED/PENDING.
            tx.execute("UPDATE documents SET status=%s,error_code=%s WHERE document_id=%s AND status<>'DELETING'", (status, code, job['target_id']))
            self.service.event(tx, 'document.updated', {'document_id': str(job['target_id']), 'status': status, 'error_code': code}, str(job['session_id']))
        elif job['job_type'] == 'TRANSCRIPT_INDEX' and final and code != 'BUDGET_EXCEEDED':
            # 6.2.5: a failed utterance stays PENDING while retried; only the final failure is marked.
            tx.execute("UPDATE knowledge_chunks SET index_status='FAILED' WHERE session_id=%s AND chunk_id=%s AND index_status='PENDING'",
                       (job['session_id'], job['target_id']))

    def claim(self):
        moment = self.service.clock()
        types = self.job_types()
        marks = ','.join('%s' for _ in types)
        while True:
            with self.db.transaction() as tx:
                # Utterance indexing goes before document processing (6.1.2: 발화 색인이 교안 처리보다 우선).
                row = tx.one(f"""SELECT * FROM jobs WHERE job_type IN ({marks}) AND
                    ((status='QUEUED' AND available_at<=%s) OR (status='RUNNING' AND lease_until<%s))
                    ORDER BY CASE WHEN job_type IN ('QA','TRANSCRIPT_INDEX') THEN 0 ELSE 1 END,available_at,created_at,job_id
                    LIMIT 1 FOR UPDATE SKIP LOCKED""", (*types, moment, moment))
                if not row:
                    return None
                if row['status'] == 'RUNNING' and row['attempts'] >= self.MAX_ATTEMPTS:
                    # The worker died MAX_ATTEMPTS times on this job (e.g. out of memory): stop reclaiming it forever.
                    code = self.error_code(row['job_type'], 'WORKER_EXCEPTION')
                    tx.execute("""UPDATE jobs SET status='FAILED',error_code=%s,lease_until=NULL,finished_at=%s WHERE job_id=%s""",
                               (code, moment, row['job_id']))
                    self.record_failure(tx, row, code, True)
                    continue
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
            handler = self.handlers().get(kind)
            if handler:
                getattr(self, handler)(job)
            elif kind == 'INGEST':
                self.ingest(job)
            elif kind == 'TRANSCRIPT_INDEX':
                self.transcript_index(job)
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
            internal = exc.code if isinstance(exc, APIError) else 'WORKER_EXCEPTION'
            if internal != 'LEASE_LOST':
                with self.db.transaction() as tx:
                    try:
                        self.active(tx, job)
                    except APIError:
                        return True
                    permanent = internal in self.PERMANENT
                    held = internal == 'BUDGET_EXCEEDED'
                    failed = permanent or job['attempts'] >= self.MAX_ATTEMPTS
                    code = self.error_code(job['job_type'], internal)
                    tx.execute("""UPDATE jobs SET status=%s,error_code=%s,available_at=%s,lease_until=NULL,finished_at=%s
                        WHERE job_id=%s AND lease_token=%s""", ('HELD' if held else ('FAILED' if failed else 'QUEUED'), code,
                        month_bounds(self.service.clock())[1] if held else self.service.clock() + timedelta(seconds=30 * 2 ** (job['attempts'] - 1)), self.service.clock() if failed or held else None,
                        job['job_id'], job['lease_token']))
                    self.record_failure(tx, job, code, failed or held)
                self.log_job_failure(job, internal, code, failed, held, exc)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
        return True

    def maintenance(self):
        """About once a minute: enqueue the daily PURGE/CLEANUP (cleanup.enqueue_daily_jobs, idempotent per day; CLEANUP
        removes unverified accounts, expired auth records, rate counters and delivered ws_events), keep the hourly
        training-server SYNC scheduled, and release budget-HELD jobs."""
        if hasattr(self, 'enqueue_daily_jobs'):
            self.enqueue_daily_jobs()
        if hasattr(self, 'ensure_periodic_sync'):
            self.ensure_periodic_sync()   # hourly SYNC with the training server (8.2.25); SYNC jobs chain themselves
        with self.db.transaction() as tx:
            for table in ('email_verifications', 'password_resets'):
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL WHERE expires_at<%s', (self.service.clock(),))
            setting = tx.one("SELECT * FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET'")
            held = tx.all("SELECT * FROM jobs WHERE status='HELD' AND error_code='BUDGET_EXCEEDED' FOR UPDATE SKIP LOCKED")
            for job in held:
                changed = setting and (not setting['hard_cap_enabled'] or (job['finished_at'] and dt(setting['updated_at']) > dt(job['finished_at'])))
                if dt(job['available_at']) <= self.service.clock() or changed:
                    tx.execute("UPDATE jobs SET status='QUEUED',attempts=0,error_code=NULL,available_at=%s,finished_at=NULL WHERE job_id=%s", (self.service.clock(), job['job_id']))
