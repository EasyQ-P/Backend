"""PURGE job: retention purge and deletion requests (6.2.11 · 8.2.37).

payload.scope
  DOCUMENT   API-24: S3 original → citations' chunk_id NULL → chunks/vectors/index → document row.
  QUESTION   API-40: private question and its answer are deleted; a public one keeps its lecture-record part
             (author link already cut by backend) and loses only the author's personal answer data.
  ARCHIVE    API-40: the member's questions/answers/notes/votes/ratings in that lecture, then the record check.
  SESSION    API-40 host delete, or retention (reason RETENTION, 90 days after the end): raw data purge.
  ACCOUNT    API-40 withdrawal: 6.2.11-5 order, hosted lectures go through SESSION.
  RETENTION  the daily run: queues SESSION purges for expired lectures and re-queues stalled deletions.

Order (8.2.37): access block (backend, or here for retention) → stop job results (cancel waiting jobs; running
ones check DELETING/deleted_at) → unmerged candidates · temporary events → S3 → DB rows · vectors · index → result.
An S3 failure raises, so the runner retries with access still blocked. MERGING candidates make the job wait.

Raw data (8.2.37): documents/chunks/transcripts/vectors, guest participants with their votes and consents, guest
private question content and answers, unmerged candidates. Record (kept while a member keeps it archived and
until the host deleted or withdrew): title/date/host name, summary, public questions with vote counts, and the
members' personal records.
"""
from easyq_common.contracts import dumps, obj

from .cleanup import WaitForMerge, drop_candidates, user_candidates

PUBLIC_STATUSES = ('ESCALATED', 'COMPLETED')


def strip_chunk_ids(value, ids=None):
    """Summary source_refs keep location text and excerpts; only chunk_id goes (all of them when ids is None)."""
    if isinstance(value, list):
        return [strip_chunk_ids(v, ids) for v in value]
    if isinstance(value, dict):
        out = {k: strip_chunk_ids(v, ids) for k, v in value.items()}
        if out.get('chunk_id') is not None and (ids is None or str(out['chunk_id']) in ids):
            out['chunk_id'] = None
        return out
    return value


class PurgeJob:
    JOB_HANDLERS = {'PURGE': 'purge'}

    def purge(self, job):
        payload = obj(job['payload'])
        scope = payload.get('scope')
        try:
            if scope == 'DOCUMENT':
                return self.purge_document(job, payload)
            if scope == 'SESSION':
                return self.purge_session(job, payload)
            with self.db.transaction() as tx:
                self.active(tx, job)
                handler = {'QUESTION': self.purge_question, 'ARCHIVE': self.purge_archive,
                           'ACCOUNT': self.purge_account, 'RETENTION': self.purge_retention}[scope]
                self.finish(tx, job, handler(tx, payload))
        except WaitForMerge:
            with self.db.transaction() as tx:
                if scope == 'SESSION':
                    # Keep raw data out of reach while waiting (retention runs block access here).
                    tx.execute("""UPDATE lecture_sessions SET status='DELETING',version=version+1 WHERE session_id=%s
                        AND status IN ('DRAFT','ENDED')""", (payload['session_id'],))
                self.defer_job(tx, job, 'MERGE_CONFIRMATION')

    # ---- building blocks ----------------------------------------------------------------
    def drop_answers(self, tx, sid, qid):
        answers = tx.all('SELECT answer_id FROM answers WHERE session_id=%s AND question_id=%s', (sid, qid))
        for a in answers:
            tx.execute('DELETE FROM answer_feedback WHERE session_id=%s AND answer_id=%s', (sid, a['answer_id']))
            tx.execute('DELETE FROM answer_citations WHERE session_id=%s AND answer_id=%s', (sid, a['answer_id']))
        tx.execute('DELETE FROM answers WHERE session_id=%s AND question_id=%s', (sid, qid))
        tx.execute('DELETE FROM ws_events WHERE session_id=%s AND question_id=%s', (sid, qid))   # answer.delta etc.

    def remove_question(self, tx, sid, qid):
        self.drop_answers(tx, sid, qid)
        tx.execute('DELETE FROM question_notes WHERE session_id=%s AND question_id=%s', (sid, qid))
        tx.execute('DELETE FROM votes WHERE session_id=%s AND question_id=%s', (sid, qid))
        tx.execute('UPDATE api_usage SET question_id=NULL WHERE question_id=%s', (qid,))
        tx.execute('DELETE FROM questions WHERE session_id=%s AND question_id=%s', (sid, qid))

    def remove_member_records(self, tx, sid, pid, user):
        """Member's personal records in one lecture (6.2.10-4 · 8.2.37); candidates must be dropped first."""
        for q in tx.all('SELECT question_id,status FROM questions WHERE session_id=%s AND participant_id=%s', (sid, pid)):
            if q['status'] in PUBLIC_STATUSES:
                tx.execute('UPDATE questions SET participant_id=NULL WHERE session_id=%s AND question_id=%s', (sid, q['question_id']))
                self.drop_answers(tx, sid, q['question_id'])
            else:
                self.remove_question(tx, sid, q['question_id'])
        tx.execute('DELETE FROM question_notes WHERE session_id=%s AND user_id=%s', (sid, user))
        tx.execute('DELETE FROM votes WHERE session_id=%s AND participant_id=%s', (sid, pid))
        tx.execute('DELETE FROM answer_feedback WHERE session_id=%s AND participant_id=%s', (sid, pid))

    def member_candidates(self, sid, pid):
        return ('p.session_id=%s AND (p.actor_participant_id=%s OR p.question_id IN '
                '(SELECT question_id FROM questions WHERE session_id=%s AND participant_id=%s))', [sid, pid, sid, pid])

    def clear_chunk_refs(self, tx, sid, ids=None):
        """Citations and summary sources keep their location text; chunk_id becomes NULL (FK SET NULL, explicit
        here so the order does not depend on FK actions)."""
        if ids is None:
            tx.execute('UPDATE answer_citations SET chunk_id=NULL WHERE session_id=%s AND chunk_id IS NOT NULL', (sid,))
        else:
            for chunk in ids:
                tx.execute('UPDATE answer_citations SET chunk_id=NULL WHERE session_id=%s AND chunk_id=%s', (sid, chunk))
        for row in tx.all('SELECT summary_id,source_refs FROM summary_notes WHERE session_id=%s FOR UPDATE', (sid,)):
            refs = obj(row['source_refs'])
            stripped = strip_chunk_ids(refs, ids)
            if stripped != refs:
                tx.execute('UPDATE summary_notes SET source_refs=%s WHERE summary_id=%s', (dumps(stripped), row['summary_id']))

    def record_orphaned(self, tx, s):
        """6.2.11-4: no member keeps it archived and the host deleted it or withdrew."""
        if not (s['host_deleted_at'] or s['host_user_id'] is None):
            return False
        kept = tx.one("SELECT count(*) AS n FROM participants WHERE session_id=%s AND kind='MEMBER' AND archived=true",
                      (s['session_id'],))['n']
        return kept == 0

    def settle_record(self, tx, sid):
        """Re-check the record rule after an archive/account change: delete it now if raw data is gone,
        otherwise make sure a SESSION purge (which ends with the same check) is queued."""
        s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
        if not s or not self.record_orphaned(tx, s):
            return False
        documents = tx.one('SELECT count(*) AS n FROM documents WHERE session_id=%s', (sid,))['n']
        if s['status'] == 'PURGED' and not documents:
            self.delete_record(tx, sid)
            return True
        if s['status'] in ('DRAFT', 'ENDED'):
            tx.execute("UPDATE lecture_sessions SET status='DELETING',version=version+1 WHERE session_id=%s", (sid,))
        if s['status'] != 'LIVE':
            self.ensure_job(tx, 'PURGE', sid, 'purge:session:' + sid, {'scope': 'SESSION', 'session_id': sid}, sid=sid)
        return False

    def purge_raw_rows(self, tx, sid):
        """DB part of the raw-data purge (S3 objects are already deleted)."""
        self.clear_chunk_refs(tx, sid)
        tx.execute('UPDATE questions SET document_id=NULL WHERE session_id=%s AND document_id IS NOT NULL', (sid,))
        chunks = tx.one('SELECT count(*) AS n FROM knowledge_chunks WHERE session_id=%s', (sid,))['n']
        tx.execute('DELETE FROM knowledge_chunks WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM documents WHERE session_id=%s', (sid,))
        guests = [r['participant_id'] for r in tx.all("SELECT participant_id FROM participants WHERE session_id=%s AND kind='GUEST'", (sid,))]
        for pid in guests:
            # Guest private questions: content and answers go, the row stays for operating metrics (6.2.11-1).
            for q in tx.all('SELECT question_id,status FROM questions WHERE session_id=%s AND participant_id=%s', (sid, pid)):
                if q['status'] not in PUBLIC_STATUSES:
                    self.drop_answers(tx, sid, q['question_id'])
                    tx.execute("""UPDATE questions SET content=NULL,content_purged_at=%s WHERE session_id=%s AND question_id=%s
                        AND content_purged_at IS NULL""", (self.service.clock(), sid, q['question_id']))
            tx.execute('DELETE FROM votes WHERE session_id=%s AND participant_id=%s', (sid, pid))
            tx.execute('DELETE FROM answer_feedback WHERE session_id=%s AND participant_id=%s', (sid, pid))
            tx.execute('DELETE FROM consents WHERE session_id=%s AND participant_id=%s', (sid, pid))
            # Public questions written by guests lose their author (8.2.8).
            tx.execute('UPDATE questions SET participant_id=NULL WHERE session_id=%s AND participant_id=%s', (sid, pid))
            tx.execute('UPDATE jobs SET participant_id=NULL WHERE participant_id=%s', (pid,))
            tx.execute('DELETE FROM participants WHERE session_id=%s AND participant_id=%s', (sid, pid))
        return {'chunks': chunks, 'guest_participants': len(guests)}

    def delete_record(self, tx, sid):
        """Delete the whole lecture (record included). Raw data and S3 objects must already be gone."""
        drop_candidates(tx, 'p.session_id=%s', [sid])
        tx.execute('DELETE FROM ws_events WHERE session_id=%s', (sid,))
        for table in ('question_notes', 'votes', 'answer_feedback', 'answer_citations', 'answers', 'summary_notes'):
            tx.execute(f'DELETE FROM {table} WHERE session_id=%s', (sid,))
        tx.execute('UPDATE api_usage SET question_id=NULL WHERE question_id IN (SELECT question_id FROM questions WHERE session_id=%s)', (sid,))
        tx.execute('UPDATE api_usage SET session_id=NULL WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM questions WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM knowledge_chunks WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM documents WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM consents WHERE session_id=%s', (sid,))
        tx.execute('UPDATE jobs SET participant_id=NULL WHERE participant_id IN (SELECT participant_id FROM participants WHERE session_id=%s)', (sid,))
        tx.execute('UPDATE jobs SET session_id=NULL WHERE session_id=%s', (sid,))
        tx.execute('DELETE FROM participants WHERE session_id=%s', (sid,))   # guest auth_sessions cascade (FK)
        tx.execute('DELETE FROM lecture_sessions WHERE session_id=%s', (sid,))

    # ---- DOCUMENT -----------------------------------------------------------------------
    def purge_document(self, job, payload):
        did = payload['document_id']
        with self.db.transaction() as tx:
            self.active(tx, job)
            document = tx.one('SELECT * FROM documents WHERE document_id=%s FOR UPDATE', (did,))
            if not document or document['status'] != 'DELETING':
                self.finish(tx, job, {'deleted_chunks': 0})
                return
            key, sid = document['object_key'], str(document['session_id'])
        self.storage_delete(key)
        with self.db.transaction() as tx:
            self.active(tx, job)
            ids = {str(r['chunk_id']) for r in tx.all('SELECT chunk_id FROM knowledge_chunks WHERE session_id=%s AND document_id=%s', (sid, did))}
            self.clear_chunk_refs(tx, sid, ids)
            tx.execute('UPDATE questions SET document_id=NULL WHERE session_id=%s AND document_id=%s', (sid, did))
            tx.execute('DELETE FROM knowledge_chunks WHERE session_id=%s AND document_id=%s', (sid, did))
            tx.execute('DELETE FROM documents WHERE session_id=%s AND document_id=%s', (sid, did))
            self.finish(tx, job, {'deleted_chunks': len(ids)})

    def storage_delete(self, key):
        # Errors propagate: the runner retries while access stays blocked (8.2.37). Missing objects are fine.
        self.service.storage.delete(key)

    # ---- QUESTION · ARCHIVE --------------------------------------------------------------
    def purge_question(self, tx, payload):
        sid, qid = payload['session_id'], payload['question_id']
        q = tx.one('SELECT question_id,status,deleted_at FROM questions WHERE session_id=%s AND question_id=%s FOR UPDATE', (sid, qid))
        if not q:
            return {'deleted': False}
        drop_candidates(tx, 'p.session_id=%s AND p.question_id=%s', [sid, qid])
        if payload.get('public'):
            tx.execute('UPDATE questions SET participant_id=NULL WHERE session_id=%s AND question_id=%s', (sid, qid))
            self.drop_answers(tx, sid, qid)
            return {'unlinked': True}
        self.remove_question(tx, sid, qid)
        return {'deleted': True}

    def purge_archive(self, tx, payload):
        sid, pid, user = payload['session_id'], payload['participant_id'], payload['user_id']
        a = tx.one('SELECT participant_id,archived FROM participants WHERE session_id=%s AND participant_id=%s FOR UPDATE', (sid, pid))
        if not a:
            return {'record_deleted': False}
        where, args = self.member_candidates(sid, pid)
        drop_candidates(tx, where, args)
        self.remove_member_records(tx, sid, pid, user)
        return {'record_deleted': self.settle_record(tx, sid)}

    # ---- SESSION (host delete · retention) ------------------------------------------------
    def purge_session(self, job, payload):
        sid = payload['session_id']
        with self.db.transaction() as tx:
            self.active(tx, job)
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
            if not s or s['status'] == 'LIVE' or (payload.get('reason') == 'RETENTION' and s['status'] not in ('ENDED', 'DELETING')):
                self.finish(tx, job, {'purged': False})
                return
            # FINALIZE/SUMMARY finish first (they build the record from raw data); a budget-HELD summary is dropped.
            busy = tx.one("""SELECT count(*) AS n FROM jobs WHERE session_id=%s AND job_type IN ('FINALIZE','SUMMARY')
                AND status IN ('QUEUED','RUNNING')""", (sid,))['n']
            if s['status'] in ('DRAFT', 'ENDED'):
                tx.execute("UPDATE lecture_sessions SET status='DELETING',version=version+1 WHERE session_id=%s", (sid,))
            if busy:
                self.defer_job(tx, job, 'FINALIZE')
                return
            moment = self.service.clock()
            tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE session_id=%s AND status IN ('QUEUED','HELD')
                AND job_type IN ('INGEST','TRANSCRIPT_INDEX','QA','SUMMARY')""", (moment, sid))
            tx.execute("UPDATE summary_notes SET status='FAILED' WHERE session_id=%s AND status IN ('QUEUED','RUNNING')", (sid,))
            drop_candidates(tx, 'p.session_id=%s', [sid])
            tx.execute('DELETE FROM ws_events WHERE session_id=%s', (sid,))
            keys = [r['object_key'] for r in tx.all('SELECT object_key FROM documents WHERE session_id=%s', (sid,))]
        for key in keys:
            self.storage_delete(key)
        with self.db.transaction() as tx:
            self.active(tx, job)
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
            if not s:
                self.finish(tx, job, {'purged': True, 'record_deleted': True})
                return
            drop_candidates(tx, 'p.session_id=%s', [sid])   # created meanwhile? (none expected: lecture is DELETING)
            counts = self.purge_raw_rows(tx, sid)
            deleted = self.record_orphaned(tx, s)
            if deleted:
                self.delete_record(tx, sid)
            else:
                tx.execute("UPDATE lecture_sessions SET status='PURGED',version=version+1 WHERE session_id=%s AND status<>'PURGED'", (sid,))
            self.finish(tx, job, {'purged': True, 'record_deleted': deleted, **counts})

    # ---- ACCOUNT ------------------------------------------------------------------------
    def purge_account(self, tx, payload):
        user = payload['user_id']
        row = tx.one('SELECT user_id,status FROM users WHERE user_id=%s FOR UPDATE', (user,))
        if not row or row['status'] != 'DELETING':
            return {'deleted': False}
        moment = self.service.clock()
        # 1) unmerged candidates
        where, args = user_candidates(user)
        drop_candidates(tx, where, args)
        # Hosted lectures: like a host delete (6.2.11-3); raw data goes through a SESSION purge job.
        settle = []
        for s in tx.all('SELECT * FROM lecture_sessions WHERE host_user_id=%s FOR UPDATE', (user,)):
            sid = str(s['session_id'])
            if s['status'] == 'LIVE':   # backend refuses withdrawal while LIVE; a race still ends the lecture first
                self.service.end(tx, s, 'HOST')
                s['status'] = 'ENDED'
            if s['status'] == 'DRAFT':
                guests = tx.all("SELECT participant_id FROM participants WHERE session_id=%s AND kind='GUEST' AND status='ACTIVE'", (sid,))
                tx.execute("UPDATE participants SET status='REVOKED',archived=false WHERE session_id=%s", (sid,))
                for g in guests:
                    gid = str(g['participant_id'])
                    self.ensure_job(tx, 'AUTH_REVOKE', gid, 'session-delete-revoke:' + gid, {'participant_id': gid, 'reason': 'SESSION_DELETED'}, sid=sid)
            tx.execute('UPDATE lecture_sessions SET host_deleted_at=COALESCE(host_deleted_at,%s) WHERE session_id=%s', (moment, sid))
            if s['status'] in ('DRAFT', 'ENDED', 'DELETING'):
                tx.execute("UPDATE lecture_sessions SET status='DELETING',version=version+1 WHERE session_id=%s AND status<>'DELETING'", (sid,))
                self.ensure_job(tx, 'PURGE', sid, 'purge:session:' + sid, {'scope': 'SESSION', 'session_id': sid}, sid=sid)
            settle.append(sid)
        # 2) personal questions · answers · notes · votes · ratings, 3) public questions lose the author link
        participations = tx.all("SELECT participant_id,session_id FROM participants WHERE user_id=%s", (user,))
        for a in participations:
            self.remove_member_records(tx, str(a['session_id']), str(a['participant_id']), user)
        tx.execute('DELETE FROM inquiries WHERE user_id=%s', (user,))
        # 4) participant rows, 5) the member row (consents · auth records · sessions go with it)
        for a in participations:
            tx.execute('UPDATE jobs SET participant_id=NULL WHERE participant_id=%s', (a['participant_id'],))
        tx.execute('DELETE FROM participants WHERE user_id=%s', (user,))
        tx.execute('UPDATE lecture_sessions SET host_user_id=NULL WHERE host_user_id=%s', (user,))
        for sid in dict.fromkeys(settle + [str(a['session_id']) for a in participations]):
            self.settle_record(tx, sid)
        for table in ('consents', 'email_verifications', 'password_resets'):
            tx.execute(f'DELETE FROM {table} WHERE user_id=%s', (user,))
        tx.execute('UPDATE jobs SET requested_by=NULL WHERE requested_by=%s', (user,))
        tx.execute("DELETE FROM users WHERE user_id=%s AND status='DELETING'", (user,))
        return {'deleted': True, 'hosted_sessions': len(settle), 'participations': len(participations)}

    # ---- RETENTION (daily) ------------------------------------------------------------------
    def purge_retention(self, tx, payload):
        moment = self.service.clock()
        day = moment.date().isoformat()
        queued = 0
        for s in tx.all("SELECT session_id FROM lecture_sessions WHERE status='ENDED' AND raw_data_expires_at<=%s", (moment,)):
            sid = str(s['session_id'])
            self.ensure_job(tx, 'PURGE', sid, 'purge:retention:' + sid, {'scope': 'SESSION', 'session_id': sid, 'reason': 'RETENTION'}, sid=sid)
            queued += 1
        # Deletions whose job failed for good stay blocked (DELETING / deleted_at): queue them again once a day.
        waiting = "NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_type='PURGE' AND j.target_id={} AND j.status IN ('QUEUED','RUNNING'))"
        stalled = [(str(r['session_id']), {'scope': 'SESSION', 'session_id': str(r['session_id'])}, str(r['session_id']))
                   for r in tx.all(f"SELECT session_id FROM lecture_sessions s WHERE status='DELETING' AND {waiting.format('s.session_id')}")]
        stalled += [(str(r['document_id']), {'scope': 'DOCUMENT', 'document_id': str(r['document_id'])}, str(r['session_id']))
                    for r in tx.all(f"SELECT document_id,session_id FROM documents d WHERE status='DELETING' AND {waiting.format('d.document_id')}")]
        stalled += [(str(r['question_id']), {'scope': 'QUESTION', 'question_id': str(r['question_id']), 'session_id': str(r['session_id']), 'public': False},
                     str(r['session_id']))
                    for r in tx.all(f"SELECT question_id,session_id FROM questions q WHERE deleted_at IS NOT NULL AND {waiting.format('q.question_id')}")]
        stalled += [(str(r['user_id']), {'scope': 'ACCOUNT', 'user_id': str(r['user_id'])}, None)
                    for r in tx.all(f"SELECT user_id FROM users u WHERE status='DELETING' AND {waiting.format('u.user_id')}")]
        for target, body, sid in stalled:
            self.ensure_job(tx, 'PURGE', target, 'purge:retry:' + target + ':' + day, body, sid=sid)
        return {'queued': queued, 'requeued': len(stalled)}
