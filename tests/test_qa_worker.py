"""Worker QA / FINALIZE / AUTO_END / SUMMARY / label candidates (6.1.4 · 6.1.5 · 6.2.7 · 6.2.9 · 8.2.20 · 8.2.28).

Questions and QA jobs are inserted with SQL (the questions REST API is tested elsewhere). External providers are fakes:
FakeTrainer (7.4.26), FakeLLM (Claude). SQLite has no pgvector / tsvector, so the SQLite run injects MemorySearch (the same
rc-v1 BM25 / cosine from the retrieval package); the PostgreSQL run uses the production PostgresSearch SQL.
"""
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from easyq_common.contracts import APIError, dumps, obj, uid
from easyq_common.candidates import create_candidate, sanitize, sweep_candidates
from easyq_worker.judge_model import JudgeUnavailable
from easyq_worker.llm import LLMResult

from tests.test_api import APIBase


class FakeTrainer:
    def __init__(self, decision='ANSWER', error=None, max_score=0.9):
        self.decision, self.error, self.max_score, self.calls = decision, error, max_score, []

    def judge(self, payload, timeout=0.8):
        self.calls.append(payload)
        if self.error:
            raise self.error
        ids = [c['evidence_id'] for c in payload['candidates']]
        ranked = [{'evidence_id': i, 'score': round(self.max_score - n * 0.1, 3)} for n, i in enumerate(ids)]
        selected = [r['evidence_id'] for r in ranked if r['score'] >= 0.6][:payload['top_k']] if self.decision == 'ANSWER' else []
        return {'model_version': 'judge-v2', 'threshold': 0.6, 'decision': self.decision, 'max_score': self.max_score,
                'ranked': ranked, 'selected': selected, 'latency_ms': 12}


class FakeLLM:
    """complete() answers from `judgments`, stream() from `answers` (each a list of text chunks)."""
    def __init__(self, answers=(), judgments=(), summaries=(), fail=False):
        self.answers, self.judgments, self.summaries, self.fail = list(answers), list(judgments), list(summaries), fail
        self.calls = []

    def complete(self, model, system, prompt, max_tokens):
        self.calls.append(('complete', model, prompt))
        if self.fail:
            raise RuntimeError('provider down')
        text = (self.summaries if 'summary' in system else self.judgments).pop(0)
        return LLMResult(text=text, model=model, input_tokens=100, output_tokens=20, stop_reason='end_turn')

    def stream(self, model, system, prompt, max_tokens, on_text):
        self.calls.append(('stream', model, prompt))
        if self.fail:
            raise RuntimeError('provider down')
        parts = []
        for chunk in self.answers.pop(0):
            parts.append(chunk)
            if on_text(chunk) is False:
                break
        return LLMResult(text=''.join(parts), model=model, input_tokens=200, output_tokens=40, stop_reason='end_turn')


class MemorySearch:
    """SQLite stand-in for PostgresSearch: same same-lecture READY scope, cosine top-k and rc-v1 BM25."""
    def vector_ids(self, tx, sid, vector, limit=20):
        from retrieval.embedding import cosine_top_k
        rows = tx.all("SELECT chunk_id,embedding FROM knowledge_chunks WHERE session_id=%s AND index_status='READY' AND embedding IS NOT NULL", (sid,))
        return [i for i, _ in cosine_top_k(vector, {str(r['chunk_id']): json.loads(r['embedding']) for r in rows}, limit)]

    def keyword_ids(self, tx, sid, tokens, limit=20):
        from retrieval.keyword import BM25Index
        rows = tx.all("SELECT chunk_id,search_vector FROM knowledge_chunks WHERE session_id=%s AND index_status='READY'", (sid,))
        return [i for i, _ in BM25Index({str(r['chunk_id']): r['search_vector'].split() for r in rows}).search(tokens, limit)]


class QAWorkerTests(APIBase):
    def setUp(self):
        super().setUp()
        self.worker.searcher = MemorySearch()
        self.configure(training_api_base_url='http://trainer.test', training_api_key='k')
        self.worker.trainer = FakeTrainer()
        self.worker.llm = FakeLLM()

    def configure(self, **values):
        self.settings = replace(self.settings, **values)
        self.service.settings = self.worker.settings = self.settings

    # ------------------------------------------------------------ fixtures ----
    def live_lecture(self):
        s, etag = self.lecture()
        status, _, _ = self.api.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertEqual(status, 200)
        self.sid = s['session_id']
        self.host = str(self.row('SELECT host_user_id FROM lecture_sessions WHERE session_id=%s', (self.sid,))['host_user_id'])
        return self.sid

    def document(self, sid, pages):
        did = uid()
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO documents(document_id,session_id,file_name,object_key,media_type,size_bytes,sha256,status)
                VALUES(%s,%s,'deck.pptx',%s,'application/pdf',1,%s,'READY')""", (did, sid, 'k/' + did, '0' * 64))
            ids = []
            for n, text in enumerate(pages, 1):
                cid = uid()
                tx.execute("""INSERT INTO knowledge_chunks(chunk_id,session_id,document_id,source_type,source_key,page_no,content,embedding,
                    search_vector,retrieval_config_version,index_status,captured_at,indexed_at)
                    VALUES(%s,%s,%s,'DOCUMENT',%s,%s,%s,%s::vector,to_tsvector('simple',%s),'rc-v1','READY',%s,%s)""",
                    (cid, sid, did, f'{did}:{n}:0', n, text, dumps([1.0] + [0.0] * 767), text, self.moment, self.moment))
                ids.append(cid)
        return ids

    def transcript(self, sid, texts, start=0):
        with self.db.transaction() as tx:
            for n, text in enumerate(texts):
                tx.execute("""INSERT INTO knowledge_chunks(chunk_id,session_id,source_type,source_key,start_ms,end_ms,content,embedding,
                    search_vector,retrieval_config_version,index_status,captured_at,indexed_at)
                    VALUES(%s,%s,'TRANSCRIPT',%s,%s,%s,%s,%s::vector,to_tsvector('simple',%s),'rc-v1','READY',%s,%s)""",
                    (uid(), sid, f't:{start + n}', (start + n) * 30000, (start + n + 1) * 30000, text, dumps([1.0] + [0.0] * 767), text,
                     self.moment, self.moment))

    def add_guest(self, sid, training=False, nickname='청중'):
        pid = uid()
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname) VALUES(%s,%s,'GUEST',%s)", (pid, sid, nickname))
            for purpose, granted in (('SERVICE', True), ('TRAINING', training)):
                tx.execute("""INSERT INTO consents(consent_id,session_id,participant_id,purpose,policy_version,granted,recorded_at)
                    VALUES(%s,%s,%s,%s,'v1',%s,%s)""", (uid(), sid, pid, purpose, granted, self.moment))
        return pid

    def host_training(self, sid, granted=True):
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO consents(consent_id,session_id,user_id,purpose,policy_version,granted,recorded_at)
                VALUES(%s,%s,%s,'TRAINING','v1',%s,%s)""", (uid(), sid, self.host, granted, self.moment + timedelta(seconds=1)))

    def question(self, sid, pid, text='해시 충돌 은 어떻게 해결 해요'):
        qid = uid()
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO questions(question_id,session_id,participant_id,content,status,idempotency_key,created_at)
                VALUES(%s,%s,%s,%s,'QUEUED',%s,%s)""", (qid, sid, pid, text, uid(), self.moment))
            self.service.job(tx, 'QA', qid, 'qa:' + qid, {}, sid=sid, participant=pid)
        return qid

    def run_qa(self):
        for _ in range(3):
            job = self.row("SELECT job_type FROM jobs WHERE job_type='QA' AND status IN ('QUEUED','RUNNING')")
            if not job:
                return
            self.worker.run_one()

    def events(self, kind=None):
        with self.db.transaction() as tx:
            rows = tx.all('SELECT * FROM ws_events ORDER BY event_id')
        return [r for r in rows if kind is None or r['event_type'] == kind]

    def snapshot(self, qid):
        return obj(self.row('SELECT decision_snapshot FROM questions WHERE question_id=%s', (qid,))['decision_snapshot'])

    # ------------------------------------------------------------ QA ----
    def test_custom_answer_is_streamed_cited_and_confirmed(self):
        sid = self.live_lecture()
        c1, c2 = self.document(sid, ['해시 충돌 은 체이닝 으로 해결', '벡터 검색 과 키워드 검색'])
        pid = self.add_guest(sid)
        self.worker.llm = FakeLLM(answers=[['ANSWER\n해시 충돌은 ', '체이닝으로 해결합니다 [1]. ', '버킷에 연결 리스트를 둡니다 [1].']])
        qid = self.question(sid, pid)
        self.run_qa()
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual(q['status'], 'AI_ANSWERED')
        self.assertIsNotNone(q['finalized_at'])
        self.assertIsNotNone(q['first_token_at'])
        self.assertAlmostEqual(float(q['decision_score']), 0.9)
        answer = self.row('SELECT * FROM answers WHERE question_id=%s', (qid,))
        self.assertEqual(answer['status'], 'ACTIVE')
        self.assertIn('[1]', answer['content'])
        citation = self.row('SELECT * FROM answer_citations WHERE answer_id=%s', (answer['answer_id'],))
        self.assertEqual((citation['citation_no'], str(citation['chunk_id']), citation['location_label']), (1, c1, '교안 p.1'))
        snap = self.snapshot(qid)
        self.assertEqual((snap['mode'], snap['route'], snap['model_version'], snap['threshold']), ('CUSTOM', 'CUSTOM', 'judge-v2', 0.6))
        self.assertEqual(snap['top_evidence_id'], snap['candidate_scores'][0]['evidence_id'])
        for key in ('progress_ms', 'first_token_ms', 'final_ms', 'judge_ms', 'retrieval_config_version', 'selected_evidence_ids'):
            self.assertIn(key, snap)
        stages = [obj(e['payload'])['stage'] for e in self.events('question.progress')]
        self.assertEqual(stages, ['SEARCHING', 'WRITING'])
        self.assertEqual(self.events('answer.delta'), [])   # temporary events removed on confirm
        completed = self.events('answer.completed')
        self.assertEqual(obj(completed[0]['payload'])['answer_id'], str(answer['answer_id']))
        self.assertEqual(str(completed[0]['question_id']), qid)
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='QA'")['status'], 'SUCCEEDED')
        ops = {r['operation'] + ':' + r['outcome'] for r in self.all('SELECT operation,outcome FROM api_usage')}
        self.assertEqual(ops, {'EMBEDDING:SUCCESS', 'ANSWER:SUCCESS'})
        # The trainer only ever sees this lecture's chunks.
        self.assertEqual({c['evidence_id'] for c in self.worker.trainer.calls[0]['candidates']}, {c1, c2})

    def all(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.all(sql, args)

    def test_retrieval_never_leaves_the_lecture(self):
        sid = self.live_lecture()
        mine = self.document(sid, ['스택 구조'])
        other = self.second_lecture()
        theirs = self.document(other['session_id'], ['해시 충돌 해결'])
        self.worker.trainer = FakeTrainer(decision='ESCALATE', max_score=0.2)
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        ids = {c['evidence_id'] for c in self.worker.trainer.calls[0]['candidates']}
        self.assertEqual(ids, set(mine))
        self.assertNotIn(theirs[0], ids)
        self.assertEqual(self.row('SELECT escalation_reason FROM questions WHERE question_id=%s', (qid,))['escalation_reason'], 'LOW_CONFIDENCE')

    def second_lecture(self):
        status, payload, _ = self.api.call('POST', '/api/v1/sessions', {'title': 'EasyQ 운영체제', 'scheduled_at': self.moment.isoformat()})
        self.assertEqual(status, 201, payload)
        return payload['data']

    def test_no_candidates_escalates_insufficient_evidence(self):
        sid = self.live_lecture()
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual((q['status'], q['escalation_reason']), ('ESCALATED', 'INSUFFICIENT_EVIDENCE'))
        self.assertEqual(self.worker.trainer.calls, [])
        public = self.events('public_question.updated')
        self.assertEqual(obj(public[0]['payload'])['status'], 'ESCALATED')
        self.assertNotIn('participant_id', obj(public[0]['payload']))

    def test_auto_escalation_creates_candidate_only_with_both_training_consents(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌 은 체이닝'])
        self.worker.trainer = FakeTrainer(decision='ESCALATE', max_score=0.4)
        refused = self.question(sid, self.add_guest(sid, training=True))   # host has not granted TRAINING yet
        self.run_qa()
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 0)
        self.host_training(sid)
        qid = self.question(sid, self.add_guest(sid, training=True, nickname='연락처'), '해시 충돌 010-1234-5678 student@example.com')
        self.run_qa()
        candidate = self.row('SELECT * FROM feedback_candidates')
        self.assertEqual((candidate['event_type'], candidate['default_label']), ('AUTO_ESCALATED', None))
        pair = obj(candidate['sanitized_pair'])
        self.assertNotIn('010-1234-5678', pair['question_text'])
        self.assertNotIn('student@example.com', pair['question_text'])
        self.assertEqual(pair['evidence_id'], self.snapshot(qid)['top_evidence_id'])
        judgment = obj(candidate['judgment_snapshot'])
        self.assertEqual((judgment['mode'], judgment['threshold']), ('CUSTOM', 0.6))
        self.assertAlmostEqual(judgment['margin'], -0.2)
        self.assertEqual(len(obj(candidate['retrieved_evidence'])), 1)
        provenance = self.row('SELECT * FROM candidate_provenance')
        self.assertEqual(str(provenance['question_id']), qid)
        self.assertEqual(len(provenance['event_key']), 64)
        with self.db.transaction() as tx:
            # The sweep re-checks current consent: the first question is eligible now that the host granted TRAINING.
            self.assertEqual(sweep_candidates(self.service, tx, sid), 1)
            self.assertEqual(sweep_candidates(self.service, tx, sid), 0)   # idempotent by event_key
        self.assertEqual(self.row('SELECT count(*) n FROM candidate_provenance WHERE question_id=%s', (refused,))['n'], 1)

    def test_pending_evaluation_check_defers_candidates_until_sweep(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌 은 체이닝'])
        self.host_training(sid)
        with self.db.transaction() as tx:
            tx.execute('UPDATE lecture_sessions SET eval_detection=%s WHERE session_id=%s', (dumps({'check_status': 'PENDING'}), sid))
        self.worker.trainer = FakeTrainer(decision='ESCALATE', max_score=0.4)
        self.question(sid, self.add_guest(sid, training=True))
        self.run_qa()
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 0)
        with self.db.transaction() as tx:
            tx.execute('UPDATE lecture_sessions SET eval_detection=%s WHERE session_id=%s', (dumps({'check_status': 'DONE'}), sid))
            self.assertEqual(sweep_candidates(self.service, tx, sid), 1)

    def test_judge_timeout_falls_back_to_llm_only_for_that_question(self):
        sid = self.live_lecture()
        c1, c2 = self.document(sid, ['해시 충돌 은 체이닝', '해시 함수 는 균등 분포'])
        self.worker.trainer = FakeTrainer(error=TimeoutError('timed out'))
        self.worker.llm = FakeLLM(judgments=['{"score": 85, "evidence": [2, 9]}'], answers=[['ANSWER\n해시 함수는 균등하게 분포시킵니다 [1].']])
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual(q['status'], 'AI_ANSWERED')
        self.assertIsNone(q['decision_score'])
        snap = self.snapshot(qid)
        self.assertEqual((snap['mode'], snap['route'], snap['fallback_reason']), ('LLM_ONLY', 'FALLBACK_LLM_ONLY', 'TIMEOUT'))
        self.assertEqual((snap['llm_score'], snap['llm_only_threshold'], snap['llm_model_id']), (85.0, 70.0, 'claude-sonnet-5-5'))
        chosen = snap['selected_evidence_ids']
        self.assertEqual(len(chosen), 1)   # [9] is not a candidate number
        citation = self.row('SELECT chunk_id FROM answer_citations')
        self.assertEqual(str(citation['chunk_id']), chosen[0])
        ops = sorted(r['operation'] for r in self.all('SELECT operation FROM api_usage'))
        self.assertEqual(ops, ['ANSWER', 'EMBEDDING', 'JUDGE_LLM'])

    def test_model_not_loaded_611_also_falls_back(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.worker.trainer = FakeTrainer(error=JudgeUnavailable(611))
        self.worker.llm = FakeLLM(judgments=['{"score": 10, "evidence": []}'])
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        snap = self.snapshot(qid)
        self.assertEqual((snap['route'], snap['fallback_reason']), ('FALLBACK_LLM_ONLY', 'MODEL_UNAVAILABLE'))
        self.assertEqual(self.row('SELECT escalation_reason FROM questions')['escalation_reason'], 'LOW_CONFIDENCE')

    def test_admin_fixed_llm_only_skips_trainer_and_never_makes_auto_candidates(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.host_training(sid)
        with self.db.transaction() as tx:
            tx.execute("""INSERT INTO deployments(deployment_id,mode,operation,requested_by,status,reason,serving_snapshot)
                VALUES(%s,'LLM_ONLY','ROLLBACK',%s,'ACTIVE','test',%s)""", (uid(), self.host, dumps({'mode': 'LLM_ONLY', 'serving_version': None,
                'llm_only_threshold': 90, 'llm_model_id': 'claude-sonnet-test', 'llm_prompt_version': 'llm-only-v2'})))
        self.worker.llm = FakeLLM(judgments=['{"score": 80, "evidence": [1]}'])
        qid = self.question(sid, self.add_guest(sid, training=True))
        self.run_qa()
        self.assertEqual(self.worker.trainer.calls, [])
        snap = self.snapshot(qid)
        self.assertEqual((snap['route'], snap['llm_only_threshold'], snap['llm_prompt_version']), ('FIXED_LLM_ONLY', 90.0, 'llm-only-v2'))
        self.assertEqual(self.worker.llm.calls[0][1], 'claude-sonnet-test')
        self.assertEqual(self.row('SELECT escalation_reason FROM questions')['escalation_reason'], 'LOW_CONFIDENCE')
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 0)

    def test_second_stage_insufficient_discards_and_becomes_candidate(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.host_training(sid)
        self.worker.llm = FakeLLM(answers=[['INSUFF', 'ICIENT\n', '무시되는 내용']])
        qid = self.question(sid, self.add_guest(sid, training=True))
        self.run_qa()
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual((q['status'], q['escalation_reason']), ('ESCALATED', 'SECOND_STAGE_INSUFFICIENT'))
        self.assertTrue(self.snapshot(qid)['second_stage_escalated'])
        self.assertEqual(self.row('SELECT count(*) n FROM answers')['n'], 0)
        self.assertEqual(obj(self.events('answer.discarded')[0]['payload'])['reason'], 'INSUFFICIENT_EVIDENCE')
        candidate = self.row('SELECT * FROM feedback_candidates')
        self.assertEqual((candidate['event_type'], candidate['default_label']), ('SECOND_STAGE_INSUFFICIENT', 'INSUFFICIENT_EVIDENCE'))

    def test_answer_without_valid_citation_is_discarded_and_escalated(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.host_training(sid)
        self.worker.llm = FakeLLM(answers=[['ANSWER\n아주 긴 답변이지만 근거가 없는 인용입니다 [7]. 끝.']])
        qid = self.question(sid, self.add_guest(sid, training=True))
        self.run_qa()
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (qid,))
        self.assertEqual((q['status'], q['escalation_reason']), ('ESCALATED', 'PROVIDER_FAILURE'))
        self.assertEqual(self.row('SELECT count(*) n FROM answers')['n'], 0)
        self.assertEqual(self.events('answer.delta'), [])
        self.assertEqual(obj(self.events('answer.discarded')[0]['payload'])['reason'], 'CITATION_INVALID')
        self.assertEqual(self.row('SELECT count(*) n FROM feedback_candidates')['n'], 0)

    def test_answer_is_limited_to_four_sentences(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.worker.llm = FakeLLM(answers=[['ANSWER\n하나 [1]. 둘. 셋. 넷. 다섯. 여섯.']])
        self.question(sid, self.add_guest(sid))
        self.run_qa()
        self.assertEqual(self.row('SELECT content FROM answers')['content'], '하나 [1]. 둘. 셋. 넷.')

    def test_provider_failure_escalates_without_failing_the_job(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.worker.llm = FakeLLM(fail=True)
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        self.assertEqual(self.row('SELECT escalation_reason FROM questions WHERE question_id=%s', (qid,))['escalation_reason'], 'PROVIDER_FAILURE')
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='QA'")['status'], 'SUCCEEDED')
        self.assertEqual(self.row("SELECT outcome FROM api_usage WHERE operation='ANSWER'")['outcome'], 'ERROR')

    def test_budget_hard_cap_escalates_with_budget_exceeded(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO app_settings(setting_key,value,hard_cap_enabled) VALUES('MONTHLY_API_BUDGET',%s,true)",
                       (dumps({'monthly_budget_krw': 1000, 'usd_krw_rate': 1400}),))
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        self.assertEqual(self.row('SELECT escalation_reason FROM questions WHERE question_id=%s', (qid,))['escalation_reason'], 'BUDGET_EXCEEDED')
        self.assertEqual(self.worker.llm.calls, [])

    def test_answered_question_is_not_reprocessed(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.worker.llm = FakeLLM(answers=[['ANSWER\n답 [1].']])
        qid = self.question(sid, self.add_guest(sid))
        self.run_qa()
        with self.db.transaction() as tx:   # a duplicate job for the same question finds it already answered
            self.service.job(tx, 'QA', qid, 'qa-dup:' + qid, {}, sid=sid)
        self.worker.run_one()
        self.assertEqual(obj(self.row("SELECT result FROM jobs WHERE idempotency_key=%s", ('qa-dup:' + qid,))['result']), {'skipped': True})

    # ------------------------------------------------------------ candidates from REST events ----
    def test_retraction_and_rating_candidates_one_per_cited_chunk(self):
        sid = self.live_lecture()
        c1, c2 = self.document(sid, ['해시 충돌', '체이닝'])
        self.host_training(sid)
        pid = self.add_guest(sid, training=True)
        self.worker.llm = FakeLLM(answers=[['ANSWER\n충돌 [1]. 체이닝 [2].']])
        qid = self.question(sid, pid)
        self.run_qa()
        aid = str(self.row('SELECT answer_id FROM answers')['answer_id'])
        with self.db.transaction() as tx:
            made = create_candidate(self.service, tx, 'RETRACTED', qid, sid, aid, actor_user_id=self.host, reason_code='EXPLAIN_AGAIN')
            self.assertEqual(len(made), 2)
            self.assertEqual(create_candidate(self.service, tx, 'RETRACTED', qid, sid, aid, actor_user_id=self.host, reason_code='EXPLAIN_AGAIN'), [])
            tx.execute("INSERT INTO answer_feedback(answer_id,participant_id,rating,version,session_id) VALUES(%s,%s,'HELPFUL',1,%s)", (aid, pid, sid))
            self.assertEqual(sweep_candidates(self.service, tx, sid), 2)
            tx.execute("UPDATE answer_feedback SET rating='UNHELPFUL',version=2 WHERE answer_id=%s", (aid,))
            self.assertEqual(sweep_candidates(self.service, tx, sid), 0)   # same event_key: HELPFUL candidates re-typed in place
        rows = self.all('SELECT event_type,default_label,reason_code FROM feedback_candidates ORDER BY event_type')
        self.assertEqual([(r['event_type'], r['default_label']) for r in rows],
                         [('AUDIENCE_UNHELPFUL', None), ('AUDIENCE_UNHELPFUL', None), ('RETRACTED', 'ANSWERABLE'), ('RETRACTED', 'ANSWERABLE')])

    def test_sanitize_masks_contact_details(self):
        self.assertEqual(sanitize('메일 a.b@x.ac.kr 전화 010 1234 5678 학번 20231234'), '메일 [MASK] 전화 [MASK] 학번 [MASK]')

    # ------------------------------------------------------------ FINALIZE ----
    def end_lecture(self):
        etag = '"%s"' % self.row('SELECT version FROM lecture_sessions WHERE session_id=%s', (self.sid,))['version']
        status, payload, _ = self.api.call('PATCH', '/api/v1/sessions/' + self.sid, {'status': 'ENDED'}, headers={'if-match': etag})
        self.assertEqual(status, 200, payload)

    def run_type(self, kind, limit=10):
        for _ in range(limit):
            if not self.row("SELECT 1 AS x FROM jobs WHERE job_type=%s AND status IN ('QUEUED','RUNNING') AND available_at<=%s",
                            (kind, self.moment)):
                return
            self.worker.run_one()

    def test_finalize_escalates_inflight_cancels_qa_and_cleans_guest_content(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        plain = self.add_guest(sid, training=False, nickname='게스트1')
        self.worker.llm = FakeLLM(answers=[['ANSWER\n충돌 [1].']])
        answered = self.question(sid, plain)
        self.run_qa()
        inflight = self.question(sid, self.add_guest(sid, nickname='게스트2'))
        stale = self.worker.claim()   # a QA worker holds the job while the lecture ends
        self.assertEqual(str(stale['target_id']), inflight)
        self.end_lecture()
        self.run_type('FINALIZE', 3)
        finalize = self.row("SELECT * FROM jobs WHERE job_type='FINALIZE'")
        self.assertEqual(finalize['status'], 'QUEUED')   # waiting inside the grace period
        self.moment += timedelta(seconds=61)
        self.run_type('FINALIZE')
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='FINALIZE'")['status'], 'SUCCEEDED')
        q = self.row('SELECT * FROM questions WHERE question_id=%s', (inflight,))
        self.assertEqual((q['status'], q['escalation_reason']), ('ESCALATED', 'FINALIZE_TIMEOUT'))
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='QA' AND target_id=%s", (inflight,))['status'], 'CANCELLED')
        with self.assertRaisesRegex(APIError, 'LEASE_LOST'):   # the late worker cannot write any more
            self.worker.qa(stale)
        # Guest private question without TRAINING consent: content gone, rows stay.
        gone = self.row('SELECT * FROM questions WHERE question_id=%s', (answered,))
        self.assertIsNone(gone['content'])
        self.assertIsNotNone(gone['content_purged_at'])
        self.assertEqual(gone['status'], 'AI_ANSWERED')
        self.assertIsNone(self.row('SELECT content FROM answers')['content'])
        self.assertEqual(self.row('SELECT count(*) n FROM answer_citations')['n'], 0)
        self.assertEqual(self.row("SELECT count(*) n FROM participants WHERE kind='GUEST' AND nickname IS NOT NULL")['n'], 0)
        stats = obj(self.row('SELECT statistics FROM lecture_sessions')['statistics'])
        self.assertEqual((stats['question_count'], stats['ai_answer_count'], stats['escalated_count']), (2, 1, 1))
        self.assertEqual(stats['participant_count'], {'member': 0, 'guest': 2})
        self.assertEqual(stats['general_question_count'], 2)
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='AUTH_REVOKE'")['n'], 2)
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='SUMMARY'")['n'], 1)
        self.assertEqual(self.row('SELECT status FROM summary_notes')['status'], 'QUEUED')

    def test_finalize_keeps_consented_guest_question_for_candidates(self):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌'])
        self.host_training(sid)
        self.worker.llm = FakeLLM(answers=[['ANSWER\n충돌 [1].']])
        qid = self.question(sid, self.add_guest(sid, training=True))
        self.run_qa()
        self.end_lecture()
        self.run_type('FINALIZE')
        self.assertIsNotNone(self.row('SELECT content FROM questions WHERE question_id=%s', (qid,))['content'])

    def test_auto_end_handler_ends_and_finalizes(self):
        sid = self.live_lecture()
        self.moment += timedelta(hours=12, seconds=1)
        self.run_type('AUTO_END')
        self.assertEqual(self.row('SELECT status,end_reason FROM lecture_sessions WHERE session_id=%s', (sid,)),
                         {'status': 'ENDED', 'end_reason': 'AUTO_TIMEOUT'})
        self.run_type('FINALIZE')
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='FINALIZE'")['status'], 'SUCCEEDED')

    # ------------------------------------------------------------ SUMMARY ----
    def finalized(self, utterance_chars):
        sid = self.live_lecture()
        self.document(sid, ['해시 충돌 슬라이드'])
        if utterance_chars:
            self.transcript(sid, ['가' * (utterance_chars // 2), '나' * (utterance_chars - utterance_chars // 2)])
        self.end_lecture()
        self.run_type('FINALIZE')
        return sid

    def test_summary_is_generated_once_with_verified_sources(self):
        self.finalized(1200)
        self.worker.llm = FakeLLM(summaries=[dumps({'one_line': '해시 정리', 'key_concepts': [{'term': '충돌', 'description': '같은 버킷'}],
            'emphases': ['체이닝'], 'keywords': ['해시'], 'sources': [1, 3, 99]})])
        self.run_type('SUMMARY')
        note = self.row('SELECT * FROM summary_notes')
        self.assertEqual(note['status'], 'READY')
        self.assertEqual(obj(note['content'])['key_concepts'], [{'term': '충돌', 'description': '같은 버킷'}])
        refs = obj(note['source_refs'])
        self.assertEqual([(r['citation_no'], r['location_label']) for r in refs], [(1, '발화 0:00'), (3, '교안 p.1')])
        self.assertEqual(obj(self.events('summary.updated')[-1]['payload'])['status'], 'READY')
        self.assertEqual(self.row("SELECT operation FROM api_usage WHERE operation='SUMMARY'")['operation'], 'SUMMARY')

    def test_summary_skipped_under_thousand_characters(self):
        self.finalized(500)
        self.run_type('SUMMARY')
        self.assertEqual(self.row('SELECT status FROM summary_notes')['status'], 'SKIPPED')
        self.assertEqual(self.worker.llm.calls, [])

    def test_summary_budget_hold_shows_queued(self):
        self.finalized(1200)
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO app_settings(setting_key,value,hard_cap_enabled) VALUES('MONTHLY_API_BUDGET',%s,true)",
                       (dumps({'monthly_budget_krw': 1000, 'usd_krw_rate': 1400}),))
        self.run_type('SUMMARY')   # other FINALIZE follow-ups (AUTH_REVOKE) may be claimed first; stops once SUMMARY is HELD
        self.assertEqual(self.row("SELECT status,error_code FROM jobs WHERE job_type='SUMMARY'"), {'status': 'HELD', 'error_code': 'BUDGET_EXCEEDED'})
        self.assertEqual(self.row('SELECT status FROM summary_notes')['status'], 'QUEUED')

    def test_summary_final_failure_marks_failed(self):
        self.finalized(1200)
        self.worker.llm = FakeLLM(fail=True)
        for _ in range(3):
            self.moment += timedelta(minutes=5)
            self.run_type('SUMMARY', 3)
        self.assertEqual(self.row("SELECT status FROM jobs WHERE job_type='SUMMARY'")['status'], 'FAILED')
        self.assertEqual(self.row('SELECT status FROM summary_notes')['status'], 'FAILED')


from tests.test_postgres import PostgresMixin, PG_DSN


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGQAWorkerTests(PostgresMixin, QAWorkerTests):
    def setUp(self):
        super().setUp()
        self.worker.searcher = None   # production PostgresSearch: pgvector distance + tsvector BM25

    def test_postgres_keyword_bm25_matches_shared_bm25(self):
        from retrieval.keyword import BM25Index
        from easyq_worker.qa import PostgresSearch
        sid = self.live_lecture()
        texts = ['해시 충돌 해시 체이닝', '해시 함수', '충돌 회피 개방 주소법 충돌', '스택 큐', 'hash collision chaining']
        ids = self.document(sid, texts)
        other = self.second_lecture()
        self.document(other['session_id'], ['해시 충돌 해시 충돌'])
        tokens = ['해시', '충돌', '충돌', 'hash']
        expected = [i for i, _ in BM25Index(dict(zip(ids, (t.split() for t in texts)))).search(tokens, 20)]
        with self.db.transaction() as tx:
            got = PostgresSearch().keyword_ids(tx, sid, tokens, 20)
            vectors = PostgresSearch().vector_ids(tx, sid, [1.0] + [0.0] * 767, 20)
        self.assertEqual(got, expected)
        self.assertEqual(set(vectors), set(ids))
