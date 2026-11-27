"""Document delete and deletion requests (API-24 · 40).

backend only blocks access and queues the work: it has no DELETE grant on documents, knowledge_chunks or
questions (8.2.1 runtime roles), so the worker PURGE job (easyq_worker.purge) removes S3 objects and rows in the
8.2.37 order. Access is blocked in the same transaction as the job row:
  document  → documents.status = DELETING (hidden from API-22, worker refuses INGEST results)
  QUESTION  → private: questions.deleted_at; public: author link cut right away (participant_id = NULL)
  ARCHIVE   → participants.archived = false (ServiceCore.participant() no longer matches)
  SESSION   → host_deleted_at (+ status DELETING while raw data exists)
  ACCOUNT   → users.status = DELETING (auth_session_view check fails at once) + AUTH_REVOKE (INT-02)
"""
from datetime import timedelta

from easyq_common.contracts import APIError, dt, fields, text, uuid_value

PUBLIC_STATUSES = ('ESCALATED', 'COMPLETED')
RECENT_LOGIN = timedelta(minutes=10)


class DeletionMixin:
    ROUTES = [
        ('DELETE', r'/sessions/(?P<sid>[^/]+)/documents/(?P<did>[^/]+)', 'delete_document'),
        ('POST', r'/deletion-requests', 'request_deletion'),
    ]

    # ---- API-24 교안 삭제 ----------------------------------------------------------------
    def delete_document(self, tx, req, p, sid, did):
        s = self.lecture(tx, sid, p, True)
        user = self.member(p)
        client, prior = self.idempotent(tx, req, user, 'PURGE', {'scope': 'DOCUMENT', 'document_id': did})
        if prior:
            return prior
        document = tx.one('SELECT * FROM documents WHERE session_id=%s AND document_id=%s FOR UPDATE', (sid, did))
        if not document or document['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if s['status'] == 'LIVE':
            # S1: while LIVE only a FAILED document that never produced searchable (READY) chunks can go.
            evidence = tx.one("""SELECT count(*) AS n FROM knowledge_chunks WHERE session_id=%s AND document_id=%s
                AND index_status='READY'""", (sid, did))['n']
            if document['status'] != 'FAILED' or evidence:
                raise APIError(409, 'SESSION_STATE_CONFLICT')
        elif s['status'] not in ('DRAFT', 'ENDED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        # Block access first; waiting INGEST work is cancelled and a running one stops at its DELETING check.
        tx.execute("UPDATE documents SET status='DELETING' WHERE document_id=%s", (did,))
        tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE job_type='INGEST' AND target_id=%s
            AND status IN ('QUEUED','HELD')""", (self.clock(), did))
        jid = self.job(tx, 'PURGE', did, 'purge:document:' + did, {'scope': 'DOCUMENT', 'document_id': did},
                       user=user, sid=sid, client=client)
        return self.accepted(did, jid)

    # ---- API-40 삭제 접수 ----------------------------------------------------------------
    TARGET_TYPES = ('QUESTION', 'ARCHIVE', 'SESSION', 'ACCOUNT')

    def request_deletion(self, tx, req, p):
        b = req.body
        fields(b, ['target_type', 'target_id', 'reason'], ['target_type', 'target_id'])
        kind = b['target_type']
        if kind not in self.TARGET_TYPES:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'target_type'})
        target = uuid_value(b['target_id'])
        if 'reason' in b and b['reason'] is not None:
            text(b['reason'], 500, 0)   # validated only: free text is never copied into jobs or logs
        user = self.member(p)
        if kind == 'ACCOUNT' and p['user_role'] == 'ADMIN':
            raise APIError(403, 'FORBIDDEN')   # Q3: ADMIN accounts are not withdrawn on the web
        client, prior = self.idempotent(tx, req, user, 'PURGE', b)
        if prior:
            return prior
        handler = {'QUESTION': self.delete_question_request, 'ARCHIVE': self.delete_archive_request,
                   'SESSION': self.delete_session_request, 'ACCOUNT': self.delete_account_request}[kind]
        return handler(tx, p, user, target, client)

    def delete_question_request(self, tx, p, user, qid, client):
        q = tx.one("""SELECT q.*,a.user_id AS author_user_id FROM questions q
            JOIN participants a ON a.participant_id=q.participant_id
            WHERE q.question_id=%s AND a.user_id=%s FOR UPDATE""", (qid, user))
        if not q or q['deleted_at'] or q['content_purged_at']:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        sid = str(q['session_id'])
        s = tx.one('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))
        if not s or s['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        public = q['status'] in PUBLIC_STATUSES
        if public:
            # Public question stays in the lecture record; only the author link goes (now: blocks "mine").
            tx.execute('UPDATE questions SET participant_id=NULL WHERE session_id=%s AND question_id=%s', (sid, qid))
        else:
            tx.execute('UPDATE questions SET deleted_at=%s WHERE session_id=%s AND question_id=%s', (self.clock(), sid, qid))
            tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE job_type='QA' AND target_id=%s
                AND status IN ('QUEUED','HELD')""", (self.clock(), qid))
        jid = self.job(tx, 'PURGE', qid, 'purge:question:' + qid,
                       {'scope': 'QUESTION', 'question_id': qid, 'session_id': sid, 'public': public,
                        'participant_id': str(q['participant_id'])}, user=user, sid=sid, client=client)
        return self.accepted(qid, jid)

    def delete_archive_request(self, tx, p, user, sid, client):
        s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
        a = tx.one("""SELECT * FROM participants WHERE session_id=%s AND user_id=%s AND kind='MEMBER' AND archived=true
            FOR UPDATE""", (sid, user)) if s else None
        if not s or not a or s['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if s['status'] not in ('ENDED', 'PURGED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')   # the archive holds finished lectures (6.2.10)
        tx.execute('UPDATE participants SET archived=false WHERE participant_id=%s', (a['participant_id'],))
        pid = str(a['participant_id'])
        jid = self.job(tx, 'PURGE', sid, 'purge:archive:' + pid, {'scope': 'ARCHIVE', 'session_id': sid, 'participant_id': pid,
                       'user_id': user}, user=user, sid=sid, client=client)
        return self.accepted(sid, jid)

    def delete_session_request(self, tx, p, user, sid, client):
        s = self.lecture(tx, sid, p, True)
        if s['status'] not in ('DRAFT', 'ENDED', 'PURGED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')   # S3: LIVE is refused
        moment = self.clock()
        if s['status'] == 'DRAFT':
            # 6.2.11-3: waiting participations are withdrawn; the lecture never ran, so nobody keeps it archived.
            guests = tx.all("SELECT participant_id FROM participants WHERE session_id=%s AND kind='GUEST' AND status='ACTIVE'", (sid,))
            tx.execute("UPDATE participants SET status='REVOKED',archived=false WHERE session_id=%s", (sid,))
            for g in guests:
                gid = str(g['participant_id'])
                self.job(tx, 'AUTH_REVOKE', gid, 'session-delete-revoke:' + gid, {'participant_id': gid, 'reason': 'SESSION_DELETED'}, sid=sid)
        # PURGED keeps its status (no raw data left); DRAFT/ENDED are DELETING (404 everywhere) until the purge ends.
        status = 'PURGED' if s['status'] == 'PURGED' else 'DELETING'
        tx.execute('UPDATE lecture_sessions SET status=%s,host_deleted_at=%s,version=version+1 WHERE session_id=%s', (status, moment, sid))
        tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE session_id=%s AND job_type IN ('INGEST','TRANSCRIPT_INDEX')
            AND status IN ('QUEUED','HELD')""", (moment, sid))
        jid = self.job(tx, 'PURGE', sid, 'purge:session:' + sid, {'scope': 'SESSION', 'session_id': sid}, user=user, sid=sid, client=client)
        return self.accepted(sid, jid)

    def delete_account_request(self, tx, p, user, target, client):
        if target != user:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        created = p.get('session_created_at')
        if not created or self.clock() - dt(created) > RECENT_LOGIN:
            raise APIError(403, 'RECENT_LOGIN_REQUIRED')
        if tx.one("SELECT session_id FROM lecture_sessions WHERE host_user_id=%s AND status='LIVE' LIMIT 1", (user,)):
            raise APIError(409, 'HOSTING_LIVE_SESSION')
        # N2: access stops now (auth_session_view.user_status <> ACTIVE); sessions are revoked through INT-02.
        tx.execute("UPDATE users SET status='DELETING',updated_at=%s WHERE user_id=%s", (self.clock(), user))
        self.job(tx, 'AUTH_REVOKE', user, 'account-revoke:' + user, {'user_id': user, 'reason': 'ACCOUNT_DELETED'}, user=user)
        jid = self.job(tx, 'PURGE', user, 'purge:account:' + user, {'scope': 'ACCOUNT', 'user_id': user}, user=user, client=client)
        return self.accepted(user, jid)


OPENAPI = [
    ('DELETE', '/sessions/{sid}/documents/{did}', 'API-24', '교안 삭제 (Idempotency-Key, 202 · PURGE 작업)'),
    ('POST', '/deletion-requests', 'API-40', '삭제 접수: QUESTION · ARCHIVE · SESSION · ACCOUNT (Idempotency-Key, 202 · PURGE 작업)'),
]
