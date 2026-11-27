"""CLEANUP job (8.2.28 · 8.2.37) and the deletion helpers PURGE shares.

payload.scope
  TRAINING  TRAINING consent withdrawal (API-11 · 20): delete the unmerged label candidates of that subject.
  DAILY     unverified accounts older than 24 h, used/expired auth records, expired rate counters and delivered
            ws_events ("사용 · 만료 후 24시간 안에 정리"). Enqueued once a day by enqueue_daily_jobs().

MERGING candidates are never deleted here: the job waits (defer_job) until the merge is confirmed through the
training server (SYNC side, 8.2.22) and the candidate is either gone (merged) or back to APPROVED (not merged).
Auth sessions are not touched: easyq_worker has no grant on auth_sessions (expired rows are harmless; auth owns them).
"""
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from easyq_common.contracts import APIError, dumps, obj, uid


class WaitForMerge(Exception):
    """A MERGING candidate blocks this deletion: roll the transaction back and retry later."""


def drop_candidates(tx, where, args):
    """Delete unmerged candidates (with their reviews and provenance) matched through candidate_provenance p.
    Raises WaitForMerge if any match is MERGING (RESTRICT FK on the question would fail anyway)."""
    rows = tx.all('SELECT c.candidate_id,c.review_status FROM feedback_candidates c '
                  'JOIN candidate_provenance p ON p.candidate_id=c.candidate_id WHERE ' + where + ' FOR UPDATE', tuple(args))
    if any(r['review_status'] == 'MERGING' for r in rows):
        raise WaitForMerge()
    for r in rows:
        for table in ('label_reviews', 'candidate_provenance', 'feedback_candidates'):
            tx.execute(f'DELETE FROM {table} WHERE candidate_id=%s', (r['candidate_id'],))
    return len(rows)


def user_candidates(user):
    """Candidates tied to a member: as actor, as question author, or from a lecture they host (host consent)."""
    return ("""(p.actor_user_id=%s OR p.session_id IN (SELECT session_id FROM lecture_sessions WHERE host_user_id=%s)
        OR p.question_id IN (SELECT q.question_id FROM questions q JOIN participants a ON a.participant_id=q.participant_id
        WHERE a.user_id=%s))""", [user] * 3)


class CleanupJob:
    JOB_HANDLERS = {'CLEANUP': 'run_cleanup'}
    MERGE_WAIT = timedelta(minutes=10)

    # ---- shared job helpers (also used by PurgeJob) -------------------------------------
    def defer_job(self, tx, job, waiting):
        """Put the job back in the queue without spending an attempt (the claim counted one)."""
        self.active(tx, job)
        tx.execute("""UPDATE jobs SET status='QUEUED',attempts=attempts-1,available_at=%s,lease_until=NULL,lease_token=NULL,result=%s
            WHERE job_id=%s AND lease_token=%s""", (self.service.clock() + self.MERGE_WAIT, dumps({'waiting_for': waiting}),
                                                    job['job_id'], job['lease_token']))

    def ensure_job(self, tx, kind, target, key, payload, sid=None, user=None):
        """Insert a job unless one with the same idempotency_key exists (safe across worker replicas)."""
        tx.execute("""INSERT INTO jobs(job_id,job_type,target_id,idempotency_key,payload,requested_by,session_id,available_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING""",
            (uid(), kind, target, key, dumps(payload), user, sid, self.service.clock()))

    def enqueue_daily_jobs(self):
        """Daily PURGE (retention fan-out + stalled deletions) and CLEANUP. Call from runner.maintenance()."""
        day = self.service.clock().date().isoformat()
        with self.db.transaction() as tx:
            self.ensure_job(tx, 'PURGE', str(uuid5(NAMESPACE_URL, 'easyq:purge:' + day)), 'purge:daily:' + day, {'scope': 'RETENTION'})
            self.ensure_job(tx, 'CLEANUP', str(uuid5(NAMESPACE_URL, 'easyq:cleanup:' + day)), 'cleanup:daily:' + day, {'scope': 'DAILY'})

    # ---- CLEANUP ------------------------------------------------------------------------
    def run_cleanup(self, job):
        try:
            with self.db.transaction() as tx:
                self.active(tx, job)
                result = self.cleanup(tx, job)
                self.finish(tx, job, result)
        except WaitForMerge:
            with self.db.transaction() as tx:
                self.defer_job(tx, job, 'MERGE_CONFIRMATION')

    def cleanup(self, tx, job):
        payload = obj(job['payload'])
        scope = payload.get('scope')
        if scope == 'DAILY':
            return self.daily_cleanup(tx)
        if scope != 'TRAINING':
            raise APIError(422, 'JOB_UNSUPPORTED')
        if payload.get('user_id'):
            where, args = user_candidates(payload['user_id'])
        else:
            where, args = 'p.actor_participant_id=%s', [payload['participant_id']]
        if payload.get('session_id'):
            where += ' AND p.session_id=%s'
            args.append(payload['session_id'])
        return {'deleted_candidates': drop_candidates(tx, where, args)}

    def daily_cleanup(self, tx):
        moment = self.service.clock()
        cutoff = moment - timedelta(hours=24)
        removed = 0
        # Same user-row lock as e-mail confirmation (8.2.2) so a code confirmed right now is not lost.
        for user in tx.all("SELECT user_id FROM users WHERE status='PENDING_VERIFICATION' AND created_at<%s", (cutoff,)):
            row = tx.one("SELECT user_id,status FROM users WHERE user_id=%s FOR UPDATE", (user['user_id'],))
            if not row or row['status'] != 'PENDING_VERIFICATION':
                continue
            tx.execute("DELETE FROM jobs WHERE requested_by=%s AND job_type IN ('EMAIL_VERIFICATION','PASSWORD_RESET')", (user['user_id'],))
            for table in ('consents', 'email_verifications', 'password_resets'):
                tx.execute(f'DELETE FROM {table} WHERE user_id=%s', (user['user_id'],))
            tx.execute("DELETE FROM users WHERE user_id=%s AND status='PENDING_VERIFICATION'", (user['user_id'],))
            removed += 1
        for table in ('email_verifications', 'password_resets'):
            tx.execute(f'DELETE FROM {table} WHERE consumed_at<%s OR revoked_at<%s OR expires_at<%s', (cutoff, cutoff, cutoff))
        tx.execute('DELETE FROM auth_rate_limits WHERE expires_at<%s', (moment,))
        tx.execute('DELETE FROM ws_events WHERE expires_at<%s', (moment,))
        return {'unverified_accounts': removed}
