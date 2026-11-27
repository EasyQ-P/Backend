"""Questions, votes, answers, ratings, notes, citation sources, summary and statistics (API-12 · 26~39)."""
from datetime import timedelta
import unittest

from easyq_common.contracts import dumps, obj, uid
from easyq_common.http import HTTPApplication
from easyq_common.security import decrypt

from tests.test_api import APIBase, FakeSTT, FakeSocket
from tests.support import Client


class QuestionTests(APIBase):
    # ---- fixtures ---------------------------------------------------------------------------
    def live(self):
        """Called first by each test (not setUp: PostgresMixin swaps the database after APIBase.setUp)."""
        self.s, etag = self.lecture()
        self.sid = self.s['session_id']
        self.base = '/api/v1/sessions/' + self.sid
        self.api.call('POST', self.base + '/consents', {'purpose': 'TRAINING', 'policy_version': 'training-v1', 'granted': True})
        status, payload, _ = self.api.call('PATCH', self.base, {'status': 'LIVE'}, headers={'if-match': self.etag()})
        self.assertEqual(status, 200, payload)

    def etag(self):
        return self.api.call('GET', self.base)[2]['etag']

    def join_guest(self, name='청중', training=True):
        client = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = client.call('POST', '/api/v1/participations', {'join_code': self.s['join_code'], 'nickname': name,
                                                                            'service_consent': True, 'training_consent': training})
        self.assertEqual(status, 201, payload)
        return client

    def join_member(self, address='audience@example.com'):
        self.moment += timedelta(seconds=61)
        auth, api = Client(HTTPApplication(self.service, 'auth')), Client(HTTPApplication(self.service, 'backend'))
        auth.call('POST', '/api/v1/users', {'email': address, 'password': 'easyq2026', 'display_name': '청중회원',
                                            'consents': {'SERVICE': {'policy_version': 'service-v1', 'granted': True}}})
        row = self.row('SELECT * FROM email_verifications v JOIN users u ON u.user_id=v.user_id WHERE u.email=%s', (address,))
        auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': address, 'code': decrypt(self.settings.encryption_key, row['delivery_ciphertext'])})
        auth.call('POST', '/api/v1/auth/sessions', {'email': address, 'password': 'easyq2026'})
        api.cookies, api.csrf = auth.cookies.copy(), auth.csrf
        status, payload, _ = api.call('POST', '/api/v1/participations', {'join_code': self.s['join_code']})
        self.assertEqual(status, 201, payload)
        return api

    def ask(self, client, content='해시 충돌은 어떻게 처리해요?', key=None):
        status, payload, headers = client.call('POST', self.base + '/questions', {'content': content}, headers={'idempotency-key': key or uid()})
        self.assertEqual(status, 202, payload)
        return payload['data']['resource_id']

    def sql(self, statement, args=()):
        with self.db.transaction() as tx:
            tx.execute(statement, args)

    def set_status(self, qid, status, reason=None):
        self.sql('UPDATE questions SET status=%s,escalation_reason=%s WHERE question_id=%s', (status, reason, qid))

    def answer(self, qid, snapshot=None):
        """What the worker stores on AI_ANSWERED (6.1.4): a transcript chunk, the answer and its citation."""
        chunk, aid = uid(), uid()
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO knowledge_chunks(chunk_id,session_id,source_type,source_key,start_ms,end_ms,content,search_vector,
                retrieval_config_version,captured_at) VALUES(%s,%s,'TRANSCRIPT',%s,0,30000,%s,to_tsvector('simple',%s),'rc-v1',%s)""",
                (chunk, self.sid, 'tr:' + chunk, '해시 충돌은 체이닝으로 처리합니다. 연락처 010-1234-5678', '해시 충돌', self.moment))
            tx.execute("""INSERT INTO answers(answer_id,question_id,session_id,content,provider_model,created_at) VALUES(%s,%s,%s,%s,'sonnet',%s)""",
                       (aid, qid, self.sid, '체이닝으로 처리합니다 [1]', self.moment))
            tx.execute("""INSERT INTO answer_citations(answer_id,citation_no,chunk_id,source_type,location_label,start_ms,end_ms,excerpt,session_id)
                VALUES(%s,1,%s,'TRANSCRIPT','발화 00:00',0,30000,'체이닝으로 처리',%s)""", (aid, chunk, self.sid))
            snapshot = {'mode': 'CUSTOM', 'route': 'CUSTOM', **(snapshot or {}), 'candidate_scores': [{'evidence_id': chunk, 'score': 0.8, 'rank': 1}]}
            tx.execute("UPDATE questions SET status='AI_ANSWERED',decision_snapshot=%s WHERE question_id=%s", (dumps(snapshot), qid))
        return aid, chunk

    def events(self, kind):
        with self.db.transaction() as tx:
            return tx.all('SELECT * FROM ws_events WHERE event_type=%s ORDER BY event_id', (kind,))

    def end(self):
        status, payload, _ = self.api.call('PATCH', self.base, {'status': 'ENDED'}, headers={'if-match': self.etag()})
        self.assertEqual(status, 200, payload)

    # ---- API-27 -----------------------------------------------------------------------------
    def test_submit_stores_question_job_and_event_in_one_step(self):
        self.live()
        guest = self.join_guest()
        key = uid()
        status, payload, headers = guest.call('POST', self.base + '/questions', {'content': ' 해시 충돌? '}, headers={'idempotency-key': key})
        self.assertEqual(status, 202, payload)
        data = payload['data']
        self.assertEqual(headers['location'], '/api/v1/jobs/' + data['job_id'])
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (data['resource_id'],))
        self.assertEqual((q['status'], q['content'], q['version']), ('QUEUED', '해시 충돌?', 1))
        job = self.row('SELECT * FROM jobs WHERE job_id=%s', (data['job_id'],))
        self.assertEqual((job['job_type'], job['idempotency_key'], str(job['participant_id'])), ('QA', 'qa:' + data['resource_id'], str(q['participant_id'])))
        event = self.events('question.updated')[-1]
        self.assertEqual(str(event['question_id']), data['resource_id'])
        self.assertEqual(obj(event['payload']), {'question_id': data['resource_id'], 'status': 'QUEUED'})
        # Same key and body: same 202. Different body: 409. Missing key: 422.
        again = guest.call('POST', self.base + '/questions', {'content': '해시 충돌?'}, headers={'idempotency-key': key})
        self.assertEqual((again[0], again[1]['data']['resource_id'], again[1]['data']['job_id']), (202, data['resource_id'], data['job_id']))
        self.assertEqual(guest.call('POST', self.base + '/questions', {'content': '다른 질문'}, headers={'idempotency-key': key})[1]['error']['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(guest.call('POST', self.base + '/questions', {'content': '키 없음'})[0], 422)
        self.assertEqual(guest.call('POST', self.base + '/questions', {'content': 'x' * 201}, headers={'idempotency-key': uid()})[0], 422)
        self.assertEqual(guest.call('POST', self.base + '/questions', {'content': '쪽', 'page_no': 3}, headers={'idempotency-key': uid()})[0], 422)
        self.assertEqual(self.row('SELECT count(*) n FROM questions')['n'], 1)

    def test_submit_requires_participant_consent_and_live(self):
        self.live()
        self.assertEqual(self.api.call('POST', self.base + '/questions', {'content': '강연자'}, headers={'idempotency-key': uid()})[0], 403)
        guest = self.join_guest()
        pid = self.row("SELECT participant_id FROM participants WHERE kind='GUEST'")['participant_id']
        self.moment += timedelta(seconds=1)
        self.sql("""INSERT INTO consents(consent_id,session_id,participant_id,purpose,policy_version,granted,recorded_at)
            VALUES(%s,%s,%s,'SERVICE','v1',false,%s)""", (uid(), self.sid, pid, self.moment))
        self.assertEqual(guest.call('POST', self.base + '/questions', {'content': '동의 철회'}, headers={'idempotency-key': uid()})[1]['error']['code'], 'CONSENT_REQUIRED')
        member = self.join_member()
        self.sql("UPDATE lecture_sessions SET status='DRAFT' WHERE session_id=%s", (self.sid,))
        self.assertEqual(member.call('POST', self.base + '/questions', {'content': '시작 전'}, headers={'idempotency-key': uid()})[1]['error']['code'], 'SESSION_NOT_LIVE')
        outsider = Client(HTTPApplication(self.service, 'backend'))
        self.assertEqual(outsider.call('POST', self.base + '/questions', {'content': '?'}, headers={'idempotency-key': uid()})[0], 401)

    # ---- API-28 · 29 visibility --------------------------------------------------------------
    def test_private_questions_and_answers_are_hidden_from_others(self):
        self.live()
        asker, other = self.join_guest('질문자'), self.join_guest('다른')
        qid = self.ask(asker)
        aid, chunk = self.answer(qid)
        self.assertEqual(other.call('GET', self.base + '/questions/' + qid)[0], 404)
        status, payload, headers = asker.call('GET', self.base + '/questions/' + qid)
        self.assertEqual((status, payload['data']['status'], headers['etag']), (200, 'AI_ANSWERED', '"1"'))
        answer = payload['data']['answer']
        self.assertEqual((answer['answer_id'], answer['status'], answer['citations'][0]['chunk_id']), (aid, 'ACTIVE', chunk))
        self.assertNotIn('participant_id', payload['data'])
        self.assertEqual(other.call('GET', self.base + '/questions?scope=public')[1]['data']['total_count'], 0)
        mine = asker.call('GET', self.base + '/questions?scope=mine')[1]['data']
        self.assertEqual([i['question_id'] for i in mine['items']], [qid])
        self.assertEqual(other.call('GET', self.base + '/questions?scope=mine')[1]['data']['items'], [])
        self.assertEqual(other.call('GET', self.base + '/questions?scope=review')[0], 403)
        review = self.api.call('GET', self.base + '/questions?scope=review')[1]['data']['items']
        self.assertEqual((review[0]['answer']['answer_id'], review[0]['answer']['my_rating']), (aid, None))
        self.assertEqual(self.api.call('GET', self.base + '/questions?scope=mine')[0], 403)
        # Escalated questions become public, without author or personal answer.
        self.set_status(qid, 'ESCALATED', 'LOW_CONFIDENCE')
        public = other.call('GET', self.base + '/questions?scope=public')[1]['data']['items'][0]
        self.assertEqual(set(public), {'question_id', 'content', 'status', 'document_id', 'page_no', 'vote_count', 'voted_by_me', 'my_note', 'version', 'created_at'})
        self.assertNotIn('answer', other.call('GET', self.base + '/questions/' + qid)[1]['data'])
        self.assertEqual(self.api.call('GET', self.base + '/questions?scope=bogus')[0], 422)

    def test_public_list_sorts_by_votes_and_pages_with_cursor(self):
        self.live()
        asker = self.join_guest()
        voters = [self.join_guest(f'v{i}') for i in range(2)]
        ids = []
        for i in range(3):
            self.moment += timedelta(seconds=1)
            ids.append(self.ask(asker, f'질문 {i}'))
            self.set_status(ids[-1], 'ESCALATED', 'LOW_CONFIDENCE')
        for v in voters:
            v.call('PUT', self.base + f'/questions/{ids[2]}/votes/me')
        voters[0].call('PUT', self.base + f'/questions/{ids[1]}/votes/me')
        first = asker.call('GET', self.base + '/questions?scope=public&sort=votes&limit=2')[1]['data']
        self.assertEqual(([i['question_id'] for i in first['items']], first['total_count']), (ids[2:0:-1], 3))
        second = asker.call('GET', self.base + '/questions?scope=public&sort=votes&limit=2&cursor=' + first['next_cursor'])[1]['data']
        self.assertEqual(([i['question_id'] for i in second['items']], second['next_cursor']), ([ids[0]], None))
        latest = asker.call('GET', self.base + '/questions?scope=public')[1]['data']['items']
        self.assertEqual([i['question_id'] for i in latest], ids[::-1])
        self.assertTrue(voters[0].call('GET', self.base + '/questions?scope=public&sort=votes')[1]['data']['items'][0]['voted_by_me'])

    # ---- API-30 · 31 ---------------------------------------------------------------------------
    def test_votes_count_once_and_only_on_live_escalated_questions(self):
        self.live()
        asker, voter = self.join_guest(), self.join_guest('공감')
        qid = self.ask(asker)
        self.assertEqual(voter.call('PUT', self.base + f'/questions/{qid}/votes/me')[0], 404)        # someone else's private question
        self.assertEqual(asker.call('PUT', self.base + f'/questions/{qid}/votes/me')[1]['error']['code'], 'QUESTION_NOT_PUBLIC')
        self.set_status(qid, 'ESCALATED', 'INSUFFICIENT_EVIDENCE')
        for _ in range(2):
            status, payload, _ = voter.call('PUT', self.base + f'/questions/{qid}/votes/me')
            self.assertEqual((status, payload['data']), (200, {'question_id': qid, 'voted': True, 'vote_count': 1}))
        self.assertEqual(len(self.events('public_question.updated')), 1)
        self.assertNotIn('voted_by_me', obj(self.events('public_question.updated')[0]['payload']))
        self.assertEqual(self.api.call('PUT', self.base + f'/questions/{qid}/votes/me')[0], 403)
        for _ in range(2):
            self.assertEqual(voter.call('DELETE', self.base + f'/questions/{qid}/votes/me')[0], 204)
        self.assertEqual(self.row('SELECT vote_count FROM questions')['vote_count'], 0)
        self.set_status(qid, 'COMPLETED')
        self.assertEqual(voter.call('PUT', self.base + f'/questions/{qid}/votes/me')[1]['error']['code'], 'QUESTION_NOT_PUBLIC')
        member = self.join_member()
        self.set_status(qid, 'ESCALATED')
        member.call('PUT', self.base + f'/questions/{qid}/votes/me')
        self.end()
        self.assertEqual(member.call('PUT', self.base + f'/questions/{qid}/votes/me')[1]['error']['code'], 'SESSION_NOT_LIVE')
        self.assertEqual(member.call('DELETE', self.base + f'/questions/{qid}/votes/me')[1]['error']['code'], 'SESSION_NOT_LIVE')
        self.assertTrue(member.call('GET', self.base + '/questions?scope=public')[1]['data']['items'][0]['voted_by_me'])

    # ---- API-32 -------------------------------------------------------------------------------
    def test_host_completes_and_uncompletes_escalated_questions(self):
        self.live()
        asker = self.join_guest()
        qid = self.ask(asker)
        path = self.base + '/questions/' + qid
        self.assertEqual(self.api.call('PATCH', path, {'status': 'COMPLETED'}, headers={'if-match': '"1"'})[1]['error']['code'], 'SESSION_STATE_CONFLICT')
        self.set_status(qid, 'ESCALATED', 'LOW_CONFIDENCE')
        self.assertEqual(self.api.call('PATCH', path, {'status': 'COMPLETED'}, headers={'if-match': '"9"'})[1]['error']['code'], 'VERSION_CONFLICT')
        self.assertEqual(asker.call('PATCH', path, {'status': 'COMPLETED'}, headers={'if-match': '"1"'})[0], 404)
        status, payload, headers = self.api.call('PATCH', path, {'status': 'COMPLETED'}, headers={'if-match': '"1"'})
        self.assertEqual((status, payload['data']['status'], payload['data']['version'], headers['etag']), (200, 'COMPLETED', 2, '"2"'))
        self.assertIsNotNone(self.row('SELECT completed_at FROM questions')['completed_at'])
        status, payload, _ = self.api.call('PATCH', path, {'status': 'ESCALATED'}, headers={'if-match': '"2"'})
        self.assertEqual((payload['data']['status'], self.row('SELECT completed_at FROM questions')['completed_at']), ('ESCALATED', None))
        self.assertEqual(self.api.call('PATCH', path, {'status': 'AI_ANSWERED'}, headers={'if-match': '"3"'})[0], 422)
        self.end()
        self.assertEqual(self.api.call('PATCH', path, {'status': 'COMPLETED'}, headers={'if-match': '"3"'})[1]['error']['code'], 'SESSION_NOT_LIVE')

    # ---- API-33 · 34 and label candidates ------------------------------------------------------
    def test_retraction_hides_answer_audits_and_makes_retracted_candidate(self):
        self.live()
        asker = self.join_guest(training=True)
        qid = self.ask(asker)
        aid, chunk = self.answer(qid, {'model_version': 'judge-v1', 'threshold': 0.5, 'max_score': 0.8})
        path = self.base + f'/questions/{qid}/answer-retractions'
        self.assertEqual(self.api.call('POST', path, {'reason_code': 'NOPE'}, headers={'if-match': '"1"'})[0], 422)
        self.assertEqual(self.api.call('POST', path, {'reason_code': 'WRONG_OR_INSUFFICIENT'}, headers={'if-match': '"7"'})[1]['error']['code'], 'VERSION_CONFLICT')
        self.assertEqual(asker.call('POST', path, {'reason_code': 'WRONG_OR_INSUFFICIENT'}, headers={'if-match': '"1"'})[0], 404)
        status, payload, _ = self.api.call('POST', path, {'reason_code': 'WRONG_OR_INSUFFICIENT', 'reason_detail': '근거 부족'}, headers={'if-match': '"1"'})
        self.assertEqual((status, payload['data']), (201, {'question_id': qid, 'answer_status': 'RETRACTED', 'question_status': 'ESCALATED',
                                                           'reason_code': 'WRONG_OR_INSUFFICIENT', 'question_version': 2}))
        a = self.row('SELECT * FROM answers')
        self.assertEqual((a['status'], a['retraction_reason']), ('RETRACTED', 'WRONG_OR_INSUFFICIENT'))
        q = self.row('SELECT * FROM questions')
        self.assertEqual((q['status'], q['escalation_reason'], q['version']), ('ESCALATED', 'RETRACTED', 2))
        audit = self.row('SELECT * FROM audit_events')
        self.assertEqual((audit['action'], audit['target_type']), ('ANSWER_RETRACTED', 'ANSWER'))
        self.assertNotIn(aid, audit['target_ref'])
        self.assertEqual(str(self.events('answer.retracted')[0]['question_id']), qid)
        candidate = self.row('SELECT * FROM feedback_candidates')
        self.assertEqual((candidate['event_type'], candidate['default_label'], candidate['reason_code']), ('RETRACTED', 'INSUFFICIENT_EVIDENCE', 'WRONG_OR_INSUFFICIENT'))
        pair = obj(candidate['sanitized_pair'])
        self.assertEqual((pair['evidence_id'], pair['course_id']), (chunk, 'course-' + self.sid))
        self.assertIn('[MASK]', pair['evidence_text'])
        self.assertNotIn('010-1234-5678', pair['evidence_text'])
        self.assertAlmostEqual(obj(candidate['judgment_snapshot'])['margin'], 0.3)
        self.assertEqual(self.row('SELECT count(*) n FROM candidate_provenance')['n'], 1)
        view = asker.call('GET', self.base + '/questions/' + qid)[1]['data']['answer']
        self.assertEqual((view['status'], view['content'], view['citations']), ('RETRACTED', None, []))
        self.assertEqual(self.api.call('POST', path, {'reason_code': 'EXPLAIN_AGAIN'}, headers={'if-match': '"2"'})[1]['error']['code'], 'ANSWER_NOT_ACTIVE')
        self.assertEqual(asker.call('PUT', self.base + f'/questions/{qid}/answer-ratings/me', {'answer_id': aid, 'rating': 'HELPFUL'})[1]['error']['code'], 'ANSWER_NOT_ACTIVE')

    def test_rating_by_asker_only_and_one_candidate_whose_type_follows_the_rating(self):
        self.live()
        asker, other = self.join_guest(training=True), self.join_guest('다른')
        qid = self.ask(asker)
        aid, _ = self.answer(qid)
        path = self.base + f'/questions/{qid}/answer-ratings/me'
        self.assertEqual(other.call('PUT', path, {'answer_id': aid, 'rating': 'HELPFUL'})[0], 404)
        self.assertEqual(asker.call('PUT', path, {'answer_id': uid(), 'rating': 'HELPFUL'})[0], 404)
        self.assertEqual(asker.call('PUT', path, {'answer_id': aid, 'rating': 'GOOD'})[0], 422)
        self.assertEqual(asker.call('PUT', path, {'answer_id': aid, 'rating': 'HELPFUL'})[1]['data'], {'answer_id': aid, 'rating': 'HELPFUL', 'version': 1})
        self.assertEqual(asker.call('PUT', path, {'answer_id': aid, 'rating': 'HELPFUL'})[1]['data']['version'], 1)
        self.assertEqual(self.row('SELECT event_type,default_label FROM feedback_candidates'), {'event_type': 'AUDIENCE_HELPFUL', 'default_label': 'ANSWERABLE'})
        self.assertEqual(asker.call('PUT', path, {'answer_id': aid, 'rating': 'UNHELPFUL'})[1]['data']['version'], 2)
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 1)
        self.assertEqual(self.row('SELECT event_type,default_label FROM feedback_candidates'), {'event_type': 'AUDIENCE_UNHELPFUL', 'default_label': None})
        self.assertEqual(asker.call('GET', self.base + '/questions/' + qid)[1]['data']['answer']['my_rating'], 'UNHELPFUL')

    def test_no_candidate_without_both_training_consents_or_for_eval_lectures(self):
        self.live()
        asker = self.join_guest(training=False)
        qid = self.ask(asker)
        aid, _ = self.answer(qid)
        asker.call('PUT', self.base + f'/questions/{qid}/answer-ratings/me', {'answer_id': aid, 'rating': 'HELPFUL'})
        self.assertEqual(self.row('SELECT count(*) n FROM answer_feedback')['n'], 1)    # stored regardless of consent
        consenting = self.join_guest('동의', training=True)
        qid2 = self.ask(consenting, '두 번째')
        aid2, _ = self.answer(qid2)
        self.sql('UPDATE lecture_sessions SET is_eval_lecture=true WHERE session_id=%s', (self.sid,))
        consenting.call('PUT', self.base + f'/questions/{qid2}/answer-ratings/me', {'answer_id': aid2, 'rating': 'HELPFUL'})
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 0)

    # ---- API-35 · 36 ----------------------------------------------------------------------------
    def test_notes_are_members_only_public_only_and_private_to_writer(self):
        self.live()
        asker = self.join_guest()
        member, other = self.join_member(), self.join_member('second@example.com')
        qid = self.ask(asker, '원래 질문')
        path = self.base + f'/questions/{qid}/notes/me'
        self.assertEqual(member.call('PUT', path, {'content': '메모'})[0], 404)       # private question of another participant
        self.set_status(qid, 'ESCALATED', 'LOW_CONFIDENCE')
        self.assertEqual(asker.call('PUT', path, {'content': '게스트'})[0], 403)
        self.assertEqual(self.api.call('PUT', path, {'content': '강연자'})[0], 403)
        self.assertEqual(member.call('PUT', path, {'content': 'x' * 1001})[0], 422)
        status, payload, _ = member.call('PUT', path, {'content': '메모'})
        self.assertEqual((status, payload['data']['content'], payload['data']['question_text']), (200, '메모', '원래 질문'))
        self.sql("UPDATE questions SET content='바뀐 질문'")
        self.assertEqual(member.call('PUT', path, {'content': '고친 메모'})[1]['data']['question_text'], '원래 질문')
        items = member.call('GET', self.base + '/questions?scope=public')[1]['data']['items']
        self.assertEqual(items[0]['my_note']['content'], '고친 메모')
        self.assertIsNone(other.call('GET', self.base + '/questions?scope=public')[1]['data']['items'][0]['my_note'])
        self.end()
        self.assertEqual(member.call('PUT', path, {'content': '보관함에서'})[0], 200)   # archive members keep writing
        for _ in range(2):
            self.assertEqual(member.call('DELETE', path)[0], 204)
        self.assertEqual(self.row('SELECT count(*) n FROM question_notes')['n'], 0)

    # ---- API-26 ---------------------------------------------------------------------------------
    def test_similar_search_returns_only_escalated_questions(self):
        self.live()
        asker = self.join_guest()
        public, private = self.ask(asker, '해시 충돌은 어떻게 처리해요?'), self.ask(asker, '해시 충돌은 어떻게 처리하나요?')
        self.ask(asker, '전혀 다른 주제')
        self.set_status(public, 'ESCALATED', 'LOW_CONFIDENCE')
        status, payload, _ = asker.call('POST', self.base + '/similar-question-searches', {'content': '해시 충돌 어떻게 처리'})
        self.assertEqual(status, 200, payload)
        self.assertEqual([i['question']['question_id'] for i in payload['data']['items']], [public])
        self.assertGreater(payload['data']['items'][0]['similarity'], 0.3)
        self.assertNotIn(private, str(payload))

    # ---- API-12 ---------------------------------------------------------------------------------
    def test_my_questions_lists_member_records_across_lectures(self):
        self.live()
        member = self.join_member()
        qid = self.ask(member)
        self.answer(qid)
        self.end()
        status, payload, _ = member.call('GET', '/api/v1/me/questions')
        self.assertEqual(status, 200, payload)
        item = payload['data']['items'][0]
        self.assertEqual((item['question_id'], item['session_id'], item['session_title'], item['answer']['status']), (qid, self.sid, 'EasyQ 자료구조', 'ACTIVE'))
        self.assertEqual(member.call('GET', '/api/v1/me/questions?session_id=' + uid())[1]['data']['total_count'], 0)
        self.sql('UPDATE participants SET archived=false WHERE user_id IS NOT NULL')
        self.assertEqual(member.call('GET', '/api/v1/me/questions')[1]['data']['total_count'], 0)

    def test_guest_cannot_list_my_questions(self):
        self.live()
        self.assertEqual(self.join_guest().call('GET', '/api/v1/me/questions')[0], 403)

    # ---- API-38 ---------------------------------------------------------------------------------
    def test_citation_source_only_for_host_or_citing_asker(self):
        self.live()
        asker, other = self.join_guest(), self.join_guest('다른')
        qid = self.ask(asker)
        _, chunk = self.answer(qid)
        status, payload, _ = asker.call('GET', self.base + '/sources/' + chunk)
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['citation_no'], payload['data']['location_label']), (1, '발화 00:00'))
        self.assertIn('체이닝', payload['data']['content'])
        self.assertEqual(other.call('GET', self.base + '/sources/' + chunk)[0], 403)
        self.assertEqual(self.api.call('GET', self.base + '/sources/' + chunk)[0], 200)
        self.assertEqual(self.api.call('GET', self.base + '/sources/' + uid())[0], 404)

    # ---- API-37 · 39 ----------------------------------------------------------------------------
    def test_summary_and_statistics_after_end(self):
        self.live()
        guest, member = self.join_guest(), self.join_member()
        self.assertEqual(guest.call('GET', self.base + '/summary')[0], 403)
        self.assertEqual(member.call('GET', self.base + '/summary')[1]['error']['code'], 'SESSION_STATE_CONFLICT')
        self.assertEqual(self.api.call('GET', self.base + '/statistics')[1]['error']['code'], 'SESSION_STATE_CONFLICT')
        q1, q2 = self.ask(guest), self.ask(member, '두 번째')
        self.answer(q1)
        self.set_status(q2, 'ESCALATED', 'LOW_CONFIDENCE')
        guest.call('PUT', self.base + f'/questions/{q2}/votes/me')
        self.end()
        self.assertEqual(member.call('GET', self.base + '/summary')[1]['data']['status'], 'QUEUED')
        self.sql("""INSERT INTO summary_notes(summary_id,session_id,status,content,source_refs,generated_at) VALUES(%s,%s,'READY',%s,%s,%s)""",
                 (uid(), self.sid, '{"one_line":"해시","key_concepts":[],"emphases":[],"keywords":["해시"]}',
                  '[{"location_label":"교안 p.1","excerpt":"해시","chunk_id":null}]', self.moment))
        data = member.call('GET', self.base + '/summary')[1]['data']
        self.assertEqual((data['status'], data['content']['one_line'], data['sources'][0]['citation_no']), ('READY', '해시', 1))
        self.assertEqual(member.call('GET', self.base + '/statistics')[0], 403)
        stats = self.api.call('GET', self.base + '/statistics')[1]['data']
        self.assertEqual((stats['question_count'], stats['ai_answer_count'], stats['escalated_count'], stats['vote_total'],
                          stats['participant_count'], stats['ai_answer_share'], stats['general_question_count']),
                         (2, 1, 1, 1, {'member': 1, 'guest': 1}, 0.5, 2))
        self.sql('UPDATE lecture_sessions SET statistics=%s WHERE session_id=%s', ('{"question_count":9}', self.sid))
        self.assertEqual(self.api.call('GET', self.base + '/statistics')[1]['data'], {'question_count': 9})

    # ---- 7.3.10 recipients ------------------------------------------------------------------------
    def test_event_hub_routes_question_events_to_asker_and_host(self):
        self.live()
        import asyncio
        from easyq_backend.live import EventHub, LiveConnection
        asker, other = self.join_guest('질문자'), self.join_guest('다른')
        hub = EventHub(self.service)
        conns = [LiveConnection(self.service, hub, FakeSTT(), FakeSocket([]), self.sid, {'origin': self.settings.origin}, dict(c.cookies))
                 for c in (self.api, asker, other)]

        async def scenario():
            for c in conns:
                self.assertTrue(await c.authorize())
            await hub.poll_once()
            for c in conns:
                hub.subscribe(c)
            qid = self.ask(asker)
            with self.db.transaction() as tx:
                self.service.question_event(tx, 'answer.delta', {'question_id': qid, 'text': '체'}, self.sid, qid)
                self.service.event(tx, 'answer.delta', {'question_id': qid, 'text': '?'}, self.sid)   # no question_id: nobody
            self.set_status(qid, 'ESCALATED', 'LOW_CONFIDENCE')
            other.call('PUT', self.base + f'/questions/{qid}/votes/me')
            await hub.poll_once()
            out = []
            for c in conns:
                items = []
                while not c.outbox.empty():
                    items.append(c.outbox.get_nowait()['type'])
                out.append(items)
            return out
        host, mine, theirs = asyncio.run(scenario())
        self.assertEqual(host, ['question.updated', 'public_question.updated'])
        self.assertEqual(mine, ['question.updated', 'answer.delta', 'public_question.updated'])
        self.assertEqual(theirs, ['public_question.updated'])


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGQuestionTests(PostgresMixin, QuestionTests):
    pass
