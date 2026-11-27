"""API-24 document delete, API-40 deletion requests and the PURGE / CLEANUP worker jobs (6.2.11 · 8.2.37)."""
from datetime import timedelta
from pathlib import Path
import unittest

from easyq_common.contracts import PPTX, dumps, obj, uid
from easyq_common.http import HTTPApplication

from tests.support import Client, pptx
from tests import test_api
from tests.test_api import APIBase


class DeletionTests(APIBase):
    audience = test_api.APITests.audience   # second member on a fresh client, joined to lecture s
    # ---------------------------------------------------------------- fixtures ----
    def ready_lecture(self):
        """DRAFT lecture with one READY pptx document (chunks indexed)."""
        s, _ = self.lecture()
        sid = s['session_id']
        status, payload, _ = self.api.call('POST', '/api/v1/sessions/' + sid + '/documents', upload=('d.pptx', PPTX, pptx()),
                                           headers={'idempotency-key': uid()})
        self.assertEqual(status, 202, payload)
        self.drain()
        did = payload['data']['resource_id']
        self.assertEqual(self.row('SELECT status FROM documents WHERE document_id=%s', (did,))['status'], 'READY')
        return sid, did

    def drain(self):
        for _ in range(50):
            if not self.worker.run_one():
                return

    def patch_status(self, sid, status):
        etag = self.api.call('GET', '/api/v1/sessions/' + sid)[2]['etag']
        result = self.api.call('PATCH', '/api/v1/sessions/' + sid, {'status': status}, headers={'if-match': etag})
        self.assertEqual(result[0], 200, result[1])

    def end(self, sid):
        self.patch_status(sid, 'ENDED')
        # FINALIZE/SUMMARY are another job's work: mark them done so PURGE does not wait for them.
        with self.db.transaction() as tx:
            tx.execute("UPDATE jobs SET status='SUCCEEDED' WHERE session_id=%s AND job_type IN ('FINALIZE','SUMMARY')", (sid,))

    def join_guest(self, s_code):
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s_code, 'nickname': '게스트', 'service_consent': True})
        self.assertIn(status, (200, 201), payload)
        return visitor, payload['data']['participant_id']

    def question(self, sid, pid, status='AI_ANSWERED', chunk=None, content='해시 충돌이 뭐예요?'):
        qid, aid = uid(), uid()
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO questions(question_id,session_id,participant_id,content,status,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s)""", (qid, sid, pid, content, status, uid()))
            tx.execute("""INSERT INTO answers(answer_id,question_id,content,provider_model,status,session_id)
                VALUES(%s,%s,'답변','sonnet','ACTIVE',%s)""", (aid, qid, sid))
            tx.execute("""INSERT INTO answer_citations(answer_id,citation_no,chunk_id,source_type,location_label,page_no,excerpt,session_id)
                VALUES(%s,1,%s,'DOCUMENT','교안 p.1',1,'인용 구절',%s)""", (aid, chunk, sid))
        return qid, aid

    def candidate(self, sid, qid, aid=None, actor_participant=None, actor_user=None, status='PENDING'):
        cid = uid()
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO feedback_candidates(candidate_id,event_type,default_label,sanitized_pair,retrieved_evidence,judgment_snapshot,review_status)
                VALUES(%s,'AUDIENCE_HELPFUL','ANSWERABLE','{}','[]','{}',%s)""", (cid, status))
            tx.execute("""INSERT INTO candidate_provenance(candidate_id,question_id,answer_id,actor_user_id,actor_participant_id,source_revision,
                consent_snapshot,event_key,session_id) VALUES(%s,%s,%s,%s,%s,1,'{}',%s,%s)""",
                (cid, qid, aid, actor_user, actor_participant, uid().replace('-', '') + 'x' * 32, sid))
        return cid

    def chunk_of(self, did):
        return str(self.row("SELECT chunk_id FROM knowledge_chunks WHERE document_id=%s AND index_status='READY' LIMIT 1", (did,))['chunk_id'])

    def count(self, sql, args=()):
        return self.row('SELECT count(*) AS n FROM ' + sql, args)['n']

    def member_pid(self, sid, address='audience@example.com'):
        return str(self.row('SELECT a.participant_id FROM participants a JOIN users u ON u.user_id=a.user_id WHERE a.session_id=%s AND u.email=%s',
                            (sid, address))['participant_id'])

    def delete(self, client, target_type, target_id, key=None):
        return client.call('POST', '/api/v1/deletion-requests', {'target_type': target_type, 'target_id': target_id},
                           headers={'idempotency-key': key or uid()})

    # ------------------------------------------------------------- API-24 ----
    def test_document_delete_purges_s3_chunks_and_keeps_citation_text(self):
        sid, did = self.ready_lecture()
        code = self.api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        member = self.audience({'join_code': code})
        self.patch_status(sid, 'LIVE')
        chunk = self.chunk_of(did)
        qid, aid = self.question(sid, self.member_pid(sid), chunk=chunk)
        with self.db.transaction() as tx:
            tx.execute('UPDATE questions SET document_id=%s,page_no=1 WHERE question_id=%s', (did, qid))
            tx.execute("""INSERT INTO summary_notes(summary_id,session_id,status,content,source_refs) VALUES(%s,%s,'READY','{}',%s)""",
                       (uid(), sid, dumps([{'chunk_id': chunk, 'location_label': '교안 p.1', 'excerpt': '구절'}])))
        path = '/api/v1/sessions/' + sid + '/documents/' + did
        self.assertEqual(self.api.call('DELETE', path, headers={'idempotency-key': uid()})[0], 409)   # LIVE + READY
        self.end(sid)
        object_key = self.row('SELECT object_key FROM documents WHERE document_id=%s', (did,))['object_key']
        self.assertTrue((Path(self.tmp.name) / object_key).exists())
        key = uid()
        self.assertEqual(member.call('DELETE', path, headers={'idempotency-key': key})[0], 404)       # not the host
        status, payload, headers = self.api.call('DELETE', path, headers={'idempotency-key': key})
        self.assertEqual(status, 202, payload)
        self.assertEqual(headers['location'], '/api/v1/jobs/' + payload['data']['job_id'])
        self.assertEqual(self.api.call('DELETE', path, headers={'idempotency-key': key})[1]['data'], payload['data'])
        self.assertEqual(self.api.call('DELETE', path, headers={'idempotency-key': uid()})[0], 404)   # already DELETING
        self.assertEqual(self.row('SELECT status FROM documents WHERE document_id=%s', (did,))['status'], 'DELETING')
        self.drain()
        self.assertEqual(self.count('documents WHERE document_id=%s', (did,)), 0)
        self.assertEqual(self.count('knowledge_chunks WHERE document_id=%s', (did,)), 0)
        self.assertFalse((Path(self.tmp.name) / object_key).exists())
        citation = self.row('SELECT chunk_id,excerpt,location_label FROM answer_citations WHERE answer_id=%s', (aid,))
        self.assertEqual((citation['chunk_id'], citation['excerpt'], citation['location_label']), (None, '인용 구절', '교안 p.1'))
        q = self.row('SELECT document_id,page_no FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual((q['document_id'], q['page_no']), (None, 1))
        refs = obj(self.row('SELECT source_refs FROM summary_notes WHERE session_id=%s', (sid,))['source_refs'])
        self.assertEqual(refs, [{'chunk_id': None, 'location_label': '교안 p.1', 'excerpt': '구절'}])
        job = self.api.call('GET', '/api/v1/jobs/' + payload['data']['job_id'])[1]['data']
        self.assertEqual((job['job_type'], job['status']), ('PURGE', 'SUCCEEDED'))

    def test_live_lecture_accepts_only_failed_document_without_evidence(self):
        s, _ = self.lecture()
        sid = s['session_id']
        self.patch_status(sid, 'LIVE')
        status, payload, _ = self.api.call('POST', '/api/v1/sessions/' + sid + '/documents', upload=('d.pptx', PPTX, pptx()),
                                           headers={'idempotency-key': uid()})
        did = payload['data']['resource_id']
        path = '/api/v1/sessions/' + sid + '/documents/' + did
        self.assertEqual(self.api.call('DELETE', path, headers={'idempotency-key': uid()})[0], 409)   # PENDING
        with self.db.transaction() as tx:
            tx.execute("UPDATE documents SET status='FAILED',error_code='DOCUMENT_PARSE_FAILED' WHERE document_id=%s", (did,))
        self.assertEqual(self.api.call('DELETE', path, headers={'idempotency-key': uid()})[0], 202)
        listed = self.api.call('GET', '/api/v1/sessions/' + sid + '/documents')[1]['data']['items']
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='INGEST' AND status='QUEUED'")['n'], 0)
        self.drain()
        self.assertEqual(self.count('documents'), 0)
        self.assertIsNotNone(listed)

    # ------------------------------------------------------------- SESSION ----
    def test_session_delete_rules_and_draft_withdraws_waiting_participants(self):
        sid, did = self.ready_lecture()
        code = self.api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        visitor, gid = self.join_guest(code)
        self.patch_status(sid, 'LIVE')
        self.assertEqual(self.delete(self.api, 'SESSION', sid)[1]['error']['code'], 'SESSION_STATE_CONFLICT')
        # A second DRAFT lecture with a waiting guest is deleted outright.
        status, payload, _ = self.api.call('POST', '/api/v1/sessions', {'title': '준비 중', 'scheduled_at': self.moment.isoformat()})
        draft = payload['data']
        waiting, wid = self.join_guest(draft['join_code'])
        status, payload, _ = self.delete(self.api, 'SESSION', draft['session_id'])
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.row('SELECT status FROM participants WHERE participant_id=%s', (wid,))['status'], 'REVOKED')
        self.assertEqual(waiting.call('GET', '/api/v1/sessions/' + draft['session_id'])[0], 401)
        self.assertEqual(self.api.call('GET', '/api/v1/sessions/' + draft['session_id'])[0], 404)
        self.drain()
        self.assertEqual(self.count('lecture_sessions WHERE session_id=%s', (draft['session_id'],)), 0)
        self.assertEqual(self.count('participants WHERE session_id=%s', (draft['session_id'],)), 0)

    def test_host_delete_purges_raw_data_and_keeps_record_for_archive_member(self):
        sid, did = self.ready_lecture()
        code = self.api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        member = self.audience({'join_code': code})
        visitor, gid = self.join_guest(code)
        self.patch_status(sid, 'LIVE')
        chunk = self.chunk_of(did)
        mpid = self.member_pid(sid)
        private_q, private_a = self.question(sid, mpid, chunk=chunk)
        guest_private, _ = self.question(sid, gid, chunk=chunk)
        guest_public, _ = self.question(sid, gid, status='ESCALATED')
        cid = self.candidate(sid, private_q, private_a, actor_participant=mpid)
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO votes(question_id,participant_id,session_id) VALUES(%s,%s,%s)", (guest_public, gid, sid))
            tx.execute("INSERT INTO votes(question_id,participant_id,session_id) VALUES(%s,%s,%s)", (guest_public, mpid, sid))
            tx.execute("INSERT INTO summary_notes(summary_id,session_id,status,content,source_refs) VALUES(%s,%s,'READY','{}','[]')", (uid(), sid))
        self.end(sid)
        status, payload, _ = self.delete(self.api, 'SESSION', sid)
        self.assertEqual(status, 202, payload)
        self.assertEqual(member.call('GET', '/api/v1/sessions/' + sid)[0], 404)   # DELETING hides it meanwhile
        self.drain()
        s = self.row('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,))
        self.assertEqual(s['status'], 'PURGED')
        self.assertIsNotNone(s['host_deleted_at'])
        self.assertEqual(self.count('knowledge_chunks WHERE session_id=%s', (sid,)), 0)
        self.assertEqual(self.count('documents WHERE session_id=%s', (sid,)), 0)
        self.assertEqual(self.count("participants WHERE session_id=%s AND kind='GUEST'", (sid,)), 0)
        self.assertEqual(self.count('feedback_candidates WHERE candidate_id=%s', (cid,)), 0)
        self.assertEqual(self.count('votes WHERE participant_id=%s', (gid,)), 0)
        self.assertEqual(self.count('votes WHERE participant_id=%s', (mpid,)), 1)      # member personal record
        self.assertEqual(self.count('summary_notes WHERE session_id=%s', (sid,)), 1)    # lecture record
        kept = self.row('SELECT content,participant_id FROM questions WHERE question_id=%s', (private_q,))
        self.assertEqual((kept['content'], str(kept['participant_id'])), ('해시 충돌이 뭐예요?', mpid))
        self.assertIsNone(self.row('SELECT chunk_id FROM answer_citations WHERE answer_id=%s', (private_a,))['chunk_id'])
        gq = self.row('SELECT content,content_purged_at,participant_id FROM questions WHERE question_id=%s', (guest_private,))
        self.assertIsNone(gq['content'])
        self.assertIsNotNone(gq['content_purged_at'])
        self.assertEqual(self.count('answers WHERE question_id=%s', (guest_private,)), 0)
        pub = self.row('SELECT content,participant_id FROM questions WHERE question_id=%s', (guest_public,))
        self.assertEqual((pub['content'], pub['participant_id']), ('해시 충돌이 뭐예요?', None))
        self.assertEqual(self.api.call('GET', '/api/v1/sessions?scope=hosted')[1]['data']['total_count'], 0)
        # The last archive member removes it: the record goes too (6.2.11-4).
        status, payload, _ = self.delete(member, 'ARCHIVE', sid)
        self.assertEqual(status, 202, payload)
        self.assertEqual(member.call('GET', '/api/v1/sessions?scope=joined')[1]['data']['total_count'], 0)
        self.drain()
        self.assertEqual(self.count('lecture_sessions WHERE session_id=%s', (sid,)), 0)
        self.assertEqual(self.count('questions WHERE session_id=%s', (sid,)), 0)
        self.assertEqual(self.count('summary_notes WHERE session_id=%s', (sid,)), 0)

    def test_merging_candidate_makes_purge_wait_without_failing(self):
        sid, did = self.ready_lecture()
        code = self.api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        self.audience({'join_code': code})
        self.patch_status(sid, 'LIVE')
        qid, aid = self.question(sid, self.member_pid(sid))
        cid = self.candidate(sid, qid, aid, status='MERGING')
        self.end(sid)
        status, payload, _ = self.delete(self.api, 'SESSION', sid)
        self.drain()
        job = self.row('SELECT * FROM jobs WHERE job_id=%s', (payload['data']['job_id'],))
        self.assertEqual((job['status'], job['attempts'], obj(job['result'])), ('QUEUED', 0, {'waiting_for': 'MERGE_CONFIRMATION'}))
        self.assertEqual(self.row('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))['status'], 'DELETING')
        self.assertEqual(self.count('documents WHERE session_id=%s', (sid,)), 1)
        with self.db.transaction() as tx:
            tx.execute("UPDATE feedback_candidates SET review_status='APPROVED' WHERE candidate_id=%s", (cid,))  # merge not applied
        self.moment += timedelta(minutes=11)
        self.drain()
        self.assertEqual(self.row('SELECT status FROM jobs WHERE job_id=%s', (payload['data']['job_id'],))['status'], 'SUCCEEDED')
        self.assertEqual(self.count('feedback_candidates'), 0)
        self.assertEqual(self.row('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))['status'], 'PURGED')

    # ------------------------------------------------------------ QUESTION ----
    def test_question_delete_private_removes_and_public_unlinks(self):
        s, _ = self.lecture()
        sid = s['session_id']
        member = self.audience(s)
        self.patch_status(sid, 'LIVE')
        mpid = self.member_pid(sid)
        private_q, private_a = self.question(sid, mpid)
        public_q, public_a = self.question(sid, mpid, status='ESCALATED')
        self.candidate(sid, private_q, private_a, actor_participant=mpid)
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO answer_feedback(answer_id,participant_id,rating,session_id) VALUES(%s,%s,'HELPFUL',%s)", (private_a, mpid, sid))
        self.assertEqual(self.delete(self.api, 'QUESTION', private_q)[0], 404)          # not the author (host)
        status, payload, _ = self.delete(member, 'QUESTION', private_q)
        self.assertEqual(status, 202, payload)
        self.assertIsNotNone(self.row('SELECT deleted_at FROM questions WHERE question_id=%s', (private_q,))['deleted_at'])
        self.assertEqual(self.delete(member, 'QUESTION', private_q)[0], 404)
        status, payload, _ = self.delete(member, 'QUESTION', public_q)
        self.assertEqual(status, 202, payload)
        self.assertIsNone(self.row('SELECT participant_id FROM questions WHERE question_id=%s', (public_q,))['participant_id'])
        self.drain()
        self.assertEqual(self.count('questions WHERE question_id=%s', (private_q,)), 0)
        self.assertEqual(self.count('answers WHERE question_id=%s', (private_q,)), 0)
        self.assertEqual(self.count('answer_feedback'), 0)
        self.assertEqual(self.count('feedback_candidates'), 0)
        pub = self.row('SELECT content,status,participant_id FROM questions WHERE question_id=%s', (public_q,))
        self.assertEqual((pub['content'], pub['status'], pub['participant_id']), ('해시 충돌이 뭐예요?', 'ESCALATED', None))
        self.assertEqual(self.count('answers WHERE question_id=%s', (public_q,)), 0)

    def test_archive_delete_requires_finished_lecture_and_keeps_record_for_host(self):
        s, _ = self.lecture()
        sid = s['session_id']
        member = self.audience(s)
        self.patch_status(sid, 'LIVE')
        mpid = self.member_pid(sid)
        public_q, _ = self.question(sid, mpid, status='COMPLETED')
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO question_notes(question_id,session_id,user_id,question_text,content)
                SELECT %s,%s,user_id,'질문','필기' FROM participants WHERE participant_id=%s""", (public_q, sid, mpid))
        self.assertEqual(self.delete(member, 'ARCHIVE', sid)[1]['error']['code'], 'SESSION_STATE_CONFLICT')
        self.end(sid)
        self.assertEqual(self.delete(member, 'ARCHIVE', uid())[0], 404)
        self.assertEqual(self.delete(member, 'ARCHIVE', sid)[0], 202)
        self.drain()
        self.assertEqual(self.count('lecture_sessions WHERE session_id=%s', (sid,)), 1)   # host still lists it
        self.assertEqual(self.count('question_notes'), 0)
        self.assertIsNone(self.row('SELECT participant_id FROM questions WHERE question_id=%s', (public_q,))['participant_id'])
        self.assertFalse(self.row('SELECT archived FROM participants WHERE participant_id=%s', (mpid,))['archived'])

    # ----------------------------------------------------------- RETENTION ----
    def test_daily_purge_destroys_raw_data_ninety_days_after_end(self):
        sid, did = self.ready_lecture()
        code = self.api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        self.audience({'join_code': code})
        self.patch_status(sid, 'LIVE')
        self.end(sid)
        self.worker.enqueue_daily_jobs()
        self.drain()
        self.assertEqual(self.row('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))['status'], 'ENDED')
        self.moment += timedelta(days=91)
        self.worker.enqueue_daily_jobs()
        self.worker.enqueue_daily_jobs()   # once a day
        self.assertEqual(self.count("jobs WHERE job_type='PURGE' AND status='QUEUED'"), 1)
        self.drain()
        self.assertEqual(self.row('SELECT status FROM lecture_sessions WHERE session_id=%s', (sid,))['status'], 'PURGED')
        self.assertEqual(self.count('knowledge_chunks'), 0)
        self.assertEqual(self.count('documents'), 0)

    def test_failed_s3_delete_keeps_access_blocked_and_daily_run_retries(self):
        sid, did = self.ready_lecture()
        broken = self.service.storage.delete
        def fail(key):
            raise OSError('s3 down')
        self.service.storage.delete = fail
        import io
        from contextlib import redirect_stderr
        self.api.call('DELETE', '/api/v1/sessions/' + sid + '/documents/' + did, headers={'idempotency-key': uid()})
        with redirect_stderr(io.StringIO()):
            for _ in range(3):
                self.drain()
                self.moment += timedelta(minutes=5)
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='PURGE'")['status'], 'FAILED')
        self.assertEqual(self.row('SELECT status FROM documents WHERE document_id=%s', (did,))['status'], 'DELETING')
        self.service.storage.delete = broken
        self.moment += timedelta(days=1)
        self.worker.enqueue_daily_jobs()
        self.drain()
        self.assertEqual(self.count('documents'), 0)

    # ------------------------------------------------------------- ACCOUNT ----
    def test_account_withdrawal_rules(self):
        from easyq_auth.admin import create_admin
        s, _ = self.lecture()
        user = self.row("SELECT user_id FROM users WHERE email='member@example.com'")['user_id']
        self.assertEqual(self.delete(self.api, 'ACCOUNT', uid())[0], 404)
        self.patch_status(s['session_id'], 'LIVE')
        self.assertEqual(self.delete(self.api, 'ACCOUNT', user)[1]['error']['code'], 'HOSTING_LIVE_SESSION')
        self.moment += timedelta(minutes=11)
        self.assertEqual(self.delete(self.api, 'ACCOUNT', user)[1]['error']['code'], 'RECENT_LOGIN_REQUIRED')
        create_admin(self.db, 'admin@example.com', '관리자', 'admin2026', clock=lambda: self.moment)
        admin = Client(HTTPApplication(self.service, 'auth'))
        admin.call('POST', '/api/v1/auth/sessions', {'email': 'admin@example.com', 'password': 'admin2026'})
        api = Client(HTTPApplication(self.service, 'backend'))
        api.cookies, api.csrf = admin.cookies.copy(), admin.csrf
        admin_id = self.row("SELECT user_id FROM users WHERE role='ADMIN'")['user_id']
        self.assertEqual(self.delete(api, 'ACCOUNT', admin_id)[1]['error']['code'], 'FORBIDDEN')
        visitor, _ = self.join_guest(s['join_code'])
        self.assertEqual(self.delete(visitor, 'ACCOUNT', user)[0], 403)

    def test_account_withdrawal_deletes_personal_records_and_hosted_lectures(self):
        # Host lecture A (ENDED, one archive member) and lecture B hosted by someone else where the member asked.
        sid, did = self.ready_lecture()
        host_id = str(self.row("SELECT user_id FROM users WHERE email='member@example.com'")['user_id'])
        host_api = self.api
        code = host_api.call('GET', '/api/v1/sessions/' + sid)[1]['data']['join_code']
        member = self.audience({'join_code': code})
        self.patch_status(sid, 'LIVE')
        self.end(sid)
        # The member hosts B and the original host asks there.
        member_id = str(self.row("SELECT user_id FROM users WHERE email='audience@example.com'")['user_id'])
        status, payload, _ = member.call('POST', '/api/v1/sessions', {'title': 'B', 'scheduled_at': self.moment.isoformat()})
        b = payload['data']
        self.assertEqual(host_api.call('POST', '/api/v1/participations', {'join_code': b['join_code']})[0], 201)
        hpid = str(self.row('SELECT participant_id FROM participants WHERE session_id=%s AND user_id=%s', (b['session_id'], host_id))['participant_id'])
        etag = member.call('GET', '/api/v1/sessions/' + b['session_id'])[2]['etag']
        member.call('PATCH', '/api/v1/sessions/' + b['session_id'], {'status': 'LIVE'}, headers={'if-match': etag})
        private_q, private_a = self.question(b['session_id'], hpid)
        public_q, _ = self.question(b['session_id'], hpid, status='ESCALATED')
        self.candidate(b['session_id'], private_q, private_a, actor_participant=hpid)
        etag = member.call('GET', '/api/v1/sessions/' + b['session_id'])[2]['etag']
        member.call('PATCH', '/api/v1/sessions/' + b['session_id'], {'status': 'ENDED'}, headers={'if-match': etag})
        with self.db.transaction() as tx:
            tx.execute("UPDATE jobs SET status='SUCCEEDED' WHERE job_type IN ('FINALIZE','SUMMARY')")
            tx.execute("INSERT INTO inquiries(inquiry_id,user_id,title,content) VALUES(%s,%s,'문의','내용')", (uid(), host_id))
        # Withdraw the original host (fresh login for the 10-minute rule).
        self.moment += timedelta(minutes=1)
        self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})
        host_api.cookies, host_api.csrf = self.auth.cookies.copy(), self.auth.csrf
        status, payload, _ = self.delete(host_api, 'ACCOUNT', host_id)
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.row('SELECT status FROM users WHERE user_id=%s', (host_id,))['status'], 'DELETING')
        self.assertEqual(host_api.call('GET', '/api/v1/sessions?scope=hosted')[0], 401)   # N2: blocked at once
        self.assertEqual(host_api.call('GET', '/api/v1/jobs/' + payload['data']['job_id'])[0], 401)
        self.drain()
        self.assertEqual(self.count('users WHERE user_id=%s', (host_id,)), 0)
        self.assertEqual(self.count('inquiries'), 0)
        self.assertEqual(self.count('auth_sessions WHERE user_id=%s AND revoked_at IS NULL', (host_id,)), 0)
        self.assertEqual(self.count('questions WHERE question_id=%s', (private_q,)), 0)
        self.assertEqual(self.count('feedback_candidates'), 0)
        self.assertIsNone(self.row('SELECT participant_id FROM questions WHERE question_id=%s', (public_q,))['participant_id'])
        self.assertEqual(self.count('participants WHERE participant_id=%s', (hpid,)), 0)
        # Hosted lecture A: raw data purged, record kept for the archive member, host link gone.
        a = self.row('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,))
        self.assertEqual((a['status'], a['host_user_id']), ('PURGED', None))
        self.assertEqual(self.count('documents WHERE session_id=%s', (sid,)), 0)
        self.assertEqual(member.call('GET', '/api/v1/sessions?scope=joined')[1]['data']['total_count'], 1)
        self.assertEqual(self.row('SELECT status FROM users WHERE user_id=%s', (member_id,))['status'], 'ACTIVE')

    # ------------------------------------------------------------- CLEANUP ----
    def test_daily_cleanup_removes_unverified_accounts_and_expired_records(self):
        self.register('late@example.com')
        self.login()
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO ws_events(event_type,payload,expires_at) VALUES('session.revoked','{}',%s)", (self.moment + timedelta(hours=1),))
        self.moment += timedelta(hours=25)
        self.worker.enqueue_daily_jobs()
        self.drain()
        self.assertEqual(self.count("users WHERE email='late@example.com'"), 0)
        self.assertEqual(self.count("users WHERE email='member@example.com'"), 1)
        self.assertEqual(self.count('ws_events'), 0)
        self.assertEqual(self.count('email_verifications'), 0)

    def test_training_withdrawal_cleanup_waits_for_merging_candidates(self):
        s, _ = self.lecture()
        sid = s['session_id']
        member = self.audience(s)
        self.patch_status(sid, 'LIVE')
        mpid = self.member_pid(sid)
        qid, aid = self.question(sid, mpid)
        pending = self.candidate(sid, qid, aid, actor_participant=mpid)
        merging = self.candidate(sid, qid, aid, actor_participant=mpid, status='MERGING')
        self.assertEqual(member.call('POST', '/api/v1/me/consents', {'purpose': 'TRAINING', 'policy_version': 'v1', 'granted': False})[0], 201)
        self.drain()
        job = self.row("SELECT * FROM jobs WHERE job_type='CLEANUP'")
        self.assertEqual(job['status'], 'QUEUED')
        self.assertEqual(self.count('feedback_candidates'), 2)
        with self.db.transaction() as tx:   # SYNC confirmed the merge: merged candidates are cleaned up there
            for table in ('candidate_provenance', 'feedback_candidates'):
                tx.execute(f'DELETE FROM {table} WHERE candidate_id=%s', (merging,))
        self.moment += timedelta(minutes=11)
        self.drain()
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='CLEANUP'")['status'], 'SUCCEEDED')
        self.assertEqual(self.count('feedback_candidates WHERE candidate_id=%s', (pending,)), 0)

    def test_resend_leaves_old_mail_job_to_be_skipped_by_worker(self):
        # easyq_auth cannot UPDATE jobs (8.2.1): the stale job stays queued and the worker sends only the new code.
        self.register()
        self.moment += timedelta(seconds=61)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/email-verifications', {'email': 'member@example.com'})[0], 202)
        self.drain()
        self.assertEqual(len(self.mailer.messages), 1)
        results = sorted(dumps(obj(r['result'])) for r in self.db_rows("SELECT result FROM jobs WHERE job_type='EMAIL_VERIFICATION'"))
        self.assertEqual(results, ['{"skipped":true}', '{}'])

    def db_rows(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.all(sql, args)


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGDeletionTests(PostgresMixin, DeletionTests):
    pass
