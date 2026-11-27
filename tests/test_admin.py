"""Admin console and training-server integration (API-45~48, 53~65; DATASET · TRAIN · DEPLOY · SYNC jobs)."""
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from easyq_common.contracts import dumps, obj, uid
from easyq_common.http import HTTPApplication
from easyq_common.trainer import TrainerClient, TrainerError, TrainerUnavailable, active_serving

from .support import Client
from .test_api import APIBase


class FakeTrainer:
    """In-process stand-in for the 7.4 adapter (same method names as easyq_common.trainer.TrainerClient)."""

    def __init__(self):
        self.calls, self.down, self.fail = [], False, {}
        self.datasets = {'ds-v1': {'dataset_version': 'ds-v1', 'parent_version': None, 'status': 'ACTIVE', 'contains_aihub': True,
                                   'statistics': {'TRAIN': {'questions': 10}}, 'script_version': 'build@1', 'retrieval_config_version': 'rc-v1',
                                   'validation_set_version': 'val-v1', 'test_set_version': 'test-v1', 'eval_slide_version': 'es-v1',
                                   'test_use_count': 0, 'change_note': 'initial', 'created_at': '2026-11-01T09:00:00+09:00',
                                   'courses_by_split': {'TRAIN': ['6.006']}, 'storage_uri': '/home/t26112/easyq-data/datasets/ds-v1/'}}
        self.models, self.evaluations, self.jobs, self.merges = {}, {}, {}, {}
        self.deployment = {'mode': 'LLM_ONLY', 'serving_version': None, 'previous_version': None, 'threshold': None,
                           'llm_only_threshold': 70, 'llm_model_id': 'claude-sonnet-x', 'llm_prompt_version': 'llm-only-v1',
                           'deployed_at': '2026-11-01T09:00:00+09:00'}

    def _call(self, name, *args):
        self.calls.append((name, args))
        if self.down:
            raise TrainerUnavailable('down')
        if name in self.fail:
            raise TrainerError(self.fail.pop(name))

    def list_datasets(self, page=1, size=100):
        self._call('list_datasets')
        keys = ('dataset_version', 'parent_version', 'status', 'contains_aihub', 'statistics', 'script_version',
                'retrieval_config_version', 'test_set_version', 'test_use_count', 'change_note', 'created_at')
        items = [{k: d[k] for k in keys} for d in self.datasets.values()]
        return {'datasets': items[(page - 1) * size: page * size], 'total_count': len(items)}

    def dataset(self, version):
        self._call('dataset', version)
        if version not in self.datasets:
            raise TrainerError(613)
        return dict(self.datasets[version])

    def merge_labels(self, base, request_id, items, description=None):
        self._call('merge_labels', base, request_id, items)
        if request_id not in self.merges:
            new = 'ds-v%d' % (len(self.datasets) + 1)
            self.datasets[new] = {**self.datasets[base], 'dataset_version': new, 'parent_version': base, 'change_note': description or ''}
            self.merges[request_id] = {'dataset_version': new, 'added': len(items), 'excluded_overlap': 0, 'masked': 0, 'skipped': 0}
        return self.merges[request_id]

    def start_training(self, dataset_version, request_id, config_override=None, memo=None):
        self._call('start_training', dataset_version, request_id, config_override)
        for j in self.jobs.values():
            if j['request_id'] == request_id:
                return {'job_id': j['job_id'], 'status': j['status']}
        job_id = 'train-%04d' % (len(self.jobs) + 1)
        self.jobs[job_id] = {'job_id': job_id, 'request_id': request_id, 'status': 'QUEUED', 'dataset_version': dataset_version}
        return {'job_id': job_id, 'status': 'QUEUED'}

    def finish_training(self, job_id, version='judge-v1', deployable=True):
        eid = 'eval-%04d' % (len(self.evaluations) + 1)
        self.evaluations[eid] = {'evaluation_id': eid, 'status': 'SUCCEEDED', 'dataset_version': 'ds-v1', 'validation_set_version': 'val-v1',
                                 'test_set_version': 'test-v1', 'retrieval_config_version': 'rc-v1', 'threshold': 0.62,
                                 'valid_auc_fp32': 0.884, 'valid_auc_fp16': 0.882, 'precision_auc_drop': 0.002, 'latency_p95_ms': 180,
                                 'roc_auc': {'value': 0.88}, 'false_answer_rate': {'value': 0.04}, 'tpr': {'value': 0.61},
                                 'pair_roc_auc': 0.91, 'service_metrics': {'retrieval_success_rate': 0.88},
                                 'baselines': [{'evaluator': 'SIMILARITY', 'roc_auc': 0.71, 'reused': True},
                                               {'evaluator': 'SONNET_ONLY', 'roc_auc': 0.83, 'threshold': 70}],
                                 'llm_only': {'threshold': 70, 'llm_model_id': 'claude-sonnet-x', 'llm_prompt_version': 'llm-only-v1'},
                                 'test_use_count': 1}
        self.models[version] = {'model_version': version, 'dataset_version': 'ds-v1', 'test_set_version': 'test-v1', 'roc_auc': 0.88,
                                'false_answer_rate': 0.042, 'tpr': 0.61, 'threshold': 0.62, 'precision_auc_drop': 0.002, 'latency_p95_ms': 180,
                                'status': 'REGISTERED', 'serving': False, 'served_before': False, 'deployable': deployable,
                                'compared_with': None, 'memo': 'mock', 'created_at': '2026-11-02T09:00:00+09:00', '_eval': eid}
        self.jobs[job_id].update(status='SUCCEEDED', stage='register', progress=1.0, curve=[{'epoch': 1, 'valid_auc': 0.86}],
                                 metrics={'roc_auc': 0.88}, model_version=version, deployable=deployable, evaluation_id=eid)

    def training_status(self, job_id):
        self._call('training_status', job_id)
        j = self.jobs[job_id]
        return {k: v for k, v in j.items() if k not in ('request_id', 'dataset_version')}

    def stop_training(self, job_id):
        self._call('stop_training', job_id)
        if self.jobs[job_id]['status'] not in ('QUEUED', 'RUNNING'):
            raise TrainerError(608)
        self.jobs[job_id]['status'] = 'CANCELLED'
        return {'status': 'CANCELLED'}

    def evaluation(self, eid):
        self._call('evaluation', eid)
        return dict(self.evaluations[eid])

    def list_models(self, status=None):
        self._call('list_models')
        return {'models': [{k: v for k, v in m.items() if k != '_eval'} for m in self.models.values()]}

    def model(self, version):
        self._call('model', version)
        m = self.models[version]
        return {'manifest': {'model_version': version, 'format': 'pytorch-fp16', 'threshold': m['threshold'], 'artifact_sha256': 'b' * 64},
                'evaluation_id': m['_eval'], 'deployable': m['deployable'], 'compared_with': m['compared_with']}

    def serving(self):
        self._call('serving')
        return dict(self.deployment)

    def _serve(self, mode, version):
        for m in self.models.values():
            m['serving'] = False
        prev = self.deployment['serving_version']
        self.deployment.update(mode=mode, serving_version=version, previous_version=prev,
                               threshold=self.models[version]['threshold'] if version else None)
        if version:
            self.models[version].update(serving=True, served_before=True)
        return dict(self.deployment)

    def switch(self, version):
        self._call('switch', version)
        m = self.models[version]
        if m['served_before']:
            raise TrainerError(610)
        return self._serve('CUSTOM', version)

    def rollback(self, target, reason):
        self._call('rollback', target)
        if target == 'LLM_ONLY':
            return self._serve('LLM_ONLY', None)
        if not self.models[target]['served_before']:
            raise TrainerError(610)
        return self._serve('CUSTOM', target)


class AdminTests(APIBase):
    def setUp(self):
        super().setUp()
        self.trainer = FakeTrainer()
        self.service.training_client = self.trainer
        self.worker.training_client = self.trainer

    # ---- helpers ---------------------------------------------------------------------------------------------------
    def client(self, address):
        user = self.login(address)
        c = Client(HTTPApplication(self.service, 'backend'))
        c.cookies, c.csrf = self.api.cookies.copy(), self.api.csrf
        return user, c

    def admin(self, address='admin@example.com'):
        user, c = self.client(address)
        self.sql("UPDATE users SET role='ADMIN' WHERE user_id=%s", (user,))
        return user, c

    def sql(self, statement, args=()):
        with self.db.transaction() as tx:
            tx.execute(statement, args)

    def run_jobs(self, kind, limit=10):
        with patch.object(self.worker, 'job_types', lambda: (kind,)):
            n = 0
            while n < limit and self.worker.run_one():
                n += 1
        return n

    def host_lecture(self, live=False):
        """member@example.com hosts a lecture; returns (host_id, lecture, client)."""
        lecture, etag = self.lecture()
        host = self.row("SELECT host_user_id FROM lecture_sessions WHERE session_id=%s", (lecture['session_id'],))['host_user_id']
        host_client = Client(HTTPApplication(self.service, 'backend'))
        host_client.cookies, host_client.csrf = self.api.cookies.copy(), self.api.csrf
        if live:
            status, payload, _ = host_client.call('PATCH', f"/api/v1/sessions/{lecture['session_id']}", {'status': 'LIVE'}, {'if-match': etag})
            self.assertEqual(status, 200, payload)
        return str(host), lecture, host_client

    def candidate(self, sid, host, asker, event_type='AUTO_ESCALATED', train=True, created=None, status='PENDING'):
        """SQL fixture: participant, consents, question, provenance and candidate (questions/QA are other modules)."""
        cid, pid, qid = uid(), uid(), uid()
        evidence = [{'evidence_id': uid(), 'evidence_type': 'DOCUMENT', 'evidence_text': '해시 충돌은 체이닝으로 처리', 'location_label': '교안 p.3'},
                    {'evidence_id': uid(), 'evidence_type': 'TRANSCRIPT', 'evidence_text': '개방 주소법', 'location_label': '발화 10:00'}]
        with self.db.transaction() as tx:
            if not tx.one('SELECT participant_id FROM participants WHERE session_id=%s AND user_id=%s', (sid, asker)):
                tx.execute("INSERT INTO participants(participant_id,session_id,user_id,kind,status,joined_at) VALUES(%s,%s,%s,'MEMBER','ACTIVE',%s)",
                           (pid, sid, asker, self.moment))
            pid = tx.one('SELECT participant_id FROM participants WHERE session_id=%s AND user_id=%s', (sid, asker))['participant_id']
            for user, granted in ((host, True), (asker, train)):
                self.service.save_consent(tx, 'TRAINING', {'policy_version': 'training-v1', 'granted': granted}, user=user, sid=sid)
            tx.execute("""INSERT INTO questions(question_id,session_id,participant_id,content,status,idempotency_key,created_at)
                VALUES(%s,%s,%s,%s,'ESCALATED',%s,%s)""", (qid, sid, pid, '해시 충돌은?', uid(), self.moment))
            tx.execute("""INSERT INTO feedback_candidates(candidate_id,event_type,default_label,sanitized_pair,retrieved_evidence,judgment_snapshot,
                review_status,created_at) VALUES(%s,%s,'INSUFFICIENT_EVIDENCE',%s,%s,%s,%s,%s)""",
                (cid, event_type, dumps({'question_text': '해시 충돌은?', 'evidence_id': evidence[0]['evidence_id'], 'evidence_type': 'DOCUMENT',
                                         'evidence_text': evidence[0]['evidence_text'], 'page_no': 3, 'start_ms': None, 'end_ms': None,
                                         'course_id': 'svc-1'}),
                 dumps(evidence), dumps({'mode': 'CUSTOM', 'model_version': 'judge-v1', 'score': 0.5, 'threshold': 0.62}), status,
                 created or self.moment))
            tx.execute("""INSERT INTO candidate_provenance(candidate_id,question_id,source_revision,consent_snapshot,event_key,created_at,session_id)
                VALUES(%s,%s,1,%s,%s,%s,%s)""", (cid, qid, '{}', uid().replace('-', '') + 'a' * 32, self.moment, sid))
        return cid, evidence

    def dataset_copy(self):
        self.run_jobs('SYNC') if self.queue_full_sync() else None
        return self.row("SELECT * FROM datasets WHERE version_name='ds-v1'")

    def queue_full_sync(self):
        from easyq_common.trainer import queue_sync
        with self.db.transaction() as tx:
            queue_sync(self.service, tx)
        return True

    # ---- adapter ----------------------------------------------------------------------------------------------------
    def test_trainer_adapter_envelope_mapping_and_fixed_paths(self):
        from dataclasses import replace
        settings = replace(self.settings, training_api_base_url='http://trainer:9000/api/v1', training_api_key='secret-key-123')
        seen = []

        def transport(method, url, headers, body, timeout):
            seen.append((method, url, headers, body))
            if url.endswith('/train-jobs'):
                return 409, json.dumps({'result_code': 609, 'result_msg': 'busy', 'data': {}}).encode()
            if url.endswith('/deployment'):
                return 200, json.dumps({'mode': 'LLM_ONLY', 'serving_version': None}).encode()   # bare data (A-15)
            return 200, json.dumps({'result_code': 200, 'result_msg': '', 'data': {'models': []}}).encode()

        client = TrainerClient(settings, transport)
        self.assertEqual(client.list_models(), {'models': []})
        self.assertEqual(client.serving()['mode'], 'LLM_ONLY')
        with self.assertRaises(TrainerError) as ctx:
            client.start_training('ds-v2', uid(), {'epochs': 2})
        self.assertEqual(ctx.exception.code, 'TRAINER_BUSY')
        self.assertEqual(seen[0][2]['X-API-Key'], 'secret-key-123')
        self.assertNotIn('secret-key-123', repr(client))
        for bad in ('../etc', 'ds-v1/../../x', 'judge-v1?x=1'):
            with self.assertRaises(ValueError):
                client.dataset(bad)
        with self.assertRaises(ValueError):
            client.rollback('previous', 'x')   # explicit version names only (API-62)

        def broken(*args):
            raise OSError('timeout')
        with self.assertRaises(TrainerUnavailable):
            TrainerClient(settings, broken).serving()
        with self.assertRaises(TrainerUnavailable):
            TrainerClient(replace(settings, training_api_base_url='')).serving()

    # ---- API-45 · 46 ----------------------------------------------------------------------------------------------
    def test_users_list_suspend_force_end_and_restore(self):
        host, lecture, host_client = self.host_lecture(live=True)
        admin, c = self.admin()
        status, payload, _ = c.call('GET', '/api/v1/admin/users?q=example&limit=1')
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data']['total_count'], 2)
        self.assertEqual(len(payload['data']['items']), 1)
        self.assertIsNotNone(payload['data']['next_cursor'])
        status, page2, _ = c.call('GET', '/api/v1/admin/users?q=example&limit=1&cursor=' + payload['data']['next_cursor'])
        self.assertEqual(len(page2['data']['items']), 1)
        user = next(u for u in payload['data']['items'] + page2['data']['items'] if u['user_id'] == host)
        self.assertEqual(user['hosted_count'], 1)
        self.assertNotIn('password_hash', user)
        self.assertEqual(user['consents']['SERVICE']['granted'], True)
        self.assertEqual(host_client.call('GET', '/api/v1/admin/users')[0], 403)

        status, payload, _ = c.call('PATCH', f'/api/v1/admin/users/{host}', {'status': 'SUSPENDED', 'reason': '운영 정책 위반'})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data']['status'], 'SUSPENDED')
        self.assertEqual(payload['data']['suspension_reason'], '운영 정책 위반')
        s = self.row('SELECT status,end_reason FROM lecture_sessions WHERE session_id=%s', (lecture['session_id'],))
        self.assertEqual((s['status'], s['end_reason']), ('ENDED', 'ADMIN'))
        self.assertEqual(host_client.call('GET', '/api/v1/sessions?scope=hosted')[0], 401)   # blocked at once
        actions = [r['action'] for r in self._all('SELECT action FROM audit_events')]
        self.assertIn('USER_SUSPENDED', actions)
        self.assertIn('SESSION_FORCE_ENDED', actions)
        self.assertNotIn(host, json.dumps(self._all('SELECT target_ref FROM audit_events')))   # pseudonymized
        job = self.row("SELECT payload FROM jobs WHERE job_type='AUTH_REVOKE' AND target_id=%s", (host,))
        self.assertEqual(obj(job['payload']), {'user_id': host, 'reason': 'ACCOUNT_SUSPENDED'})
        # INT-02 runs as easyq_auth under EASYQ_TEST_ROLES; core.event() takes the event number from the sequence (D-2).
        self.assertEqual(self.run_jobs('AUTH_REVOKE'), 1)
        self.assertIsNotNone(self.row('SELECT revoked_at FROM auth_sessions WHERE user_id=%s', (host,))['revoked_at'])

        status, payload, _ = self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})
        self.assertEqual((status, payload['error']['code']), (403, 'ACCOUNT_SUSPENDED'))

        calls = []
        self.service.guest_client = type('Revoker', (), {'revoke': lambda _, body: calls.append(body) or {'revoked_count': 0}})()
        status, payload, _ = c.call('PATCH', f'/api/v1/admin/users/{host}', {'status': 'ACTIVE', 'reason': '소명 확인'})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['status'], payload['data']['suspension_reason']), ('ACTIVE', None))
        self.assertEqual(calls[0]['user_id'], host)

        def down(_, body):
            raise OSError('auth down')
        self.service.guest_client = type('Down', (), {'revoke': down})()
        c.call('PATCH', f'/api/v1/admin/users/{host}', {'status': 'SUSPENDED', 'reason': 'x'})
        status, payload, _ = c.call('PATCH', f'/api/v1/admin/users/{host}', {'status': 'ACTIVE', 'reason': 'y'})
        self.assertEqual((status, payload['error']['code']), (503, 'AUTH_SERVICE_UNAVAILABLE'))
        self.assertEqual(self.row('SELECT status FROM users WHERE user_id=%s', (host,))['status'], 'SUSPENDED')

        status, payload, _ = c.call('PATCH', f'/api/v1/admin/users/{admin}', {'status': 'SUSPENDED', 'reason': 'self'})
        self.assertEqual((status, payload['error']['code']), (409, 'SESSION_STATE_CONFLICT'))   # last active admin

    def _all(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.all(sql, args)

    # ---- API-47 · 48 ----------------------------------------------------------------------------------------------
    def test_admin_lecture_list_eval_mark_and_force_end(self):
        host, lecture, host_client = self.host_lecture()
        sid = lecture['session_id']
        admin, c = self.admin()
        status, payload, _ = c.call('GET', '/api/v1/admin/sessions?status=DRAFT')
        self.assertEqual(status, 200, payload)
        item = payload['data']['items'][0]
        self.assertEqual((item['session_id'], item['host_user_id'], item['question_count']), (sid, host, 0))
        status, payload, _ = c.call('PATCH', f'/api/v1/admin/sessions/{sid}', {'status': 'ENDED', 'reason': 'x'}, {'if-match': '"1"'})
        self.assertEqual((status, payload['error']['code']), (409, 'SESSION_STATE_CONFLICT'))   # DRAFT is not force-ended
        status, payload, _ = c.call('PATCH', f'/api/v1/admin/sessions/{sid}', {'is_eval_lecture': True, 'reason': '검증 강좌'}, {'if-match': '"9"'})
        self.assertEqual((status, payload['error']['code']), (409, 'VERSION_CONFLICT'))
        self.assertEqual(c.call('PATCH', f'/api/v1/admin/sessions/{sid}', {'is_eval_lecture': False, 'reason': 'x'}, {'if-match': '"1"'})[0], 422)
        status, payload, headers = c.call('PATCH', f'/api/v1/admin/sessions/{sid}', {'is_eval_lecture': True, 'reason': '검증 강좌'}, {'if-match': '"1"'})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data'], {'session_id': sid, 'status': 'DRAFT', 'is_eval_lecture': True, 'version': 2})
        status, payload, _ = host_client.call('PATCH', f'/api/v1/sessions/{sid}', {'status': 'LIVE'}, {'if-match': '"2"'})
        self.assertEqual(status, 200, payload)
        status, payload, _ = c.call('PATCH', f'/api/v1/admin/sessions/{sid}', {'status': 'ENDED', 'reason': '운영 정책 위반'}, {'if-match': '"3"'})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['status'], payload['data']['version']), ('ENDED', 4))
        self.assertEqual(self.row('SELECT end_reason FROM lecture_sessions WHERE session_id=%s', (sid,))['end_reason'], 'ADMIN')
        self.assertTrue(self.row("SELECT job_id FROM jobs WHERE job_type='FINALIZE' AND target_id=%s", (sid,)))

    # ---- API-53 · 54 ----------------------------------------------------------------------------------------------
    def test_candidate_list_rechecks_consent_and_two_person_review(self):
        host, lecture, _ = self.host_lecture()
        sid = lecture['session_id']
        asker = self.verified('asker@example.com')
        cid, evidence = self.candidate(sid, host, asker)
        hidden, _ = self.candidate(sid, host, self.verified('nope@example.com'), train=False)
        a1, c1 = self.admin('admin1@example.com')
        a2, c2 = self.admin('admin2@example.com')
        a3, c3 = self.admin('admin3@example.com')
        status, payload, _ = c1.call('GET', '/api/v1/admin/feedback-candidates')
        self.assertEqual(status, 200, payload)
        self.assertEqual([i['candidate_id'] for i in payload['data']['items']], [cid])   # the hidden one's asker refused TRAINING
        item = payload['data']['items'][0]
        self.assertEqual(item['session_title'], 'EasyQ 자료구조')
        self.assertNotIn(asker, json.dumps(payload))
        status, payload, _ = c1.call('GET', '/api/v1/admin/feedback-candidates?sort=NEAR_THRESHOLD&model_version=judge-v1')
        self.assertEqual(payload['data']['total_count'], 1)
        self.assertEqual(c1.call('GET', '/api/v1/admin/feedback-candidates?sort=NEAR_THRESHOLD')[0], 422)

        url = f'/api/v1/admin/feedback-candidates/{cid}/reviews/me'
        self.assertEqual(c1.call('PUT', url, {'label': 'ANSWERABLE'})[0], 422)   # AUTO_ESCALATED needs the evidence
        self.assertEqual(c1.call('PUT', url, {'label': 'ANSWERABLE', 'evidence_id': uid()})[0], 422)   # not a retrieved candidate
        status, payload, _ = c1.call('PUT', url, {'label': 'ANSWERABLE', 'evidence_id': evidence[1]['evidence_id'], 'comment': '근거 확인'})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data']['review_status'], 'PENDING')
        status, payload, _ = c2.call('GET', '/api/v1/admin/feedback-candidates')
        self.assertEqual(payload['data']['items'][0]['reviews'], [])   # hidden before my own review
        status, payload, _ = c2.call('PUT', url, {'label': 'OPINION'})
        self.assertEqual(payload['data']['review_status'], 'DISAGREEMENT')
        self.assertEqual(len(payload['data']['reviews']), 2)
        status, payload, _ = c3.call('PUT', url, {'label': 'OPINION'})
        self.assertEqual((status, payload['error']['code']), (409, 'REVIEW_CONFLICT'))
        status, payload, _ = c2.call('PUT', url, {'label': 'ANSWERABLE', 'evidence_id': evidence[1]['evidence_id']})
        self.assertEqual((payload['data']['review_status'], payload['data']['final_label']), ('APPROVED', 'ANSWERABLE'))

        # Questioner withdraws TRAINING for the account: the candidate disappears and cannot be reviewed.
        self.moment += timedelta(seconds=1)
        with self.db.transaction() as tx:
            self.service.save_consent(tx, 'TRAINING', {'policy_version': 'training-v1', 'granted': True}, user=asker)
        self.moment += timedelta(seconds=1)
        with self.db.transaction() as tx:
            self.service.save_consent(tx, 'TRAINING', {'policy_version': 'training-v1', 'granted': False}, user=asker)
        self.assertEqual(c1.call('GET', '/api/v1/admin/feedback-candidates')[1]['data']['total_count'], 0)
        status, payload, _ = c1.call('PUT', url, {'label': 'OPINION'})
        self.assertEqual((status, payload['error']['code']), (409, 'CANDIDATE_NOT_ELIGIBLE'))
        self.assertTrue(hidden)

    # ---- API-55 · 56 + DATASET -------------------------------------------------------------------------------------
    def approved(self, sid, host, asker, evidence_pick=1):
        cid, evidence = self.candidate(sid, host, asker)
        with self.db.transaction() as tx:
            for reviewer in self.reviewers:
                tx.execute("INSERT INTO label_reviews(candidate_id,reviewer_user_id,label,evidence_id,reviewed_at) VALUES(%s,%s,'ANSWERABLE',%s,%s)",
                           (cid, reviewer, evidence[evidence_pick]['evidence_id'], self.moment))
            tx.execute("UPDATE feedback_candidates SET review_status='APPROVED',final_label='ANSWERABLE' WHERE candidate_id=%s", (cid,))
        return cid, evidence

    def test_merge_sends_sanitized_pairs_and_cleans_up(self):
        host, lecture, _ = self.host_lecture()
        sid = lecture['session_id']
        asker = self.verified('asker@example.com')
        a1, c1 = self.admin('admin1@example.com')
        a2, _ = self.admin('admin2@example.com')
        self.reviewers = (a1, a2)
        self.dataset_copy()
        old, _ = self.candidate(sid, host, asker, created=self.moment - timedelta(days=1))
        cid, evidence = self.approved(sid, host, asker)
        body = {'base_dataset_version': 'ds-v1', 'candidate_ids': [cid], 'description': '승인 후보 병합'}
        self.assertEqual(c1.call('POST', '/api/v1/admin/datasets', body)[0], 422)   # Idempotency-Key required
        status, payload, headers = c1.call('POST', '/api/v1/admin/datasets', body, {'idempotency-key': 'k1'})
        self.assertEqual(status, 202, payload)
        self.assertEqual(headers['location'], '/api/v1/jobs/' + payload['data']['job_id'])
        self.assertEqual(c1.call('POST', '/api/v1/admin/datasets', body, {'idempotency-key': 'k1'})[1]['data'], payload['data'])
        self.assertEqual(c1.call('POST', '/api/v1/admin/datasets', {**body, 'description': 'x'}, {'idempotency-key': 'k1'})[1]['error']['code'],
                         'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.row('SELECT review_status FROM feedback_candidates WHERE candidate_id=%s', (cid,))['review_status'], 'MERGING')
        status, again, _ = c1.call('POST', '/api/v1/admin/datasets', body, {'idempotency-key': 'k2'})
        self.assertEqual((status, again['error']['code']), (409, 'CANDIDATE_NOT_ELIGIBLE'))

        self.assertEqual(self.run_jobs('DATASET'), 1)
        call = next(args for name, args in self.trainer.calls if name == 'merge_labels')
        self.assertEqual(call[1], payload['data']['resource_id'])   # request_id = merge_request_id
        item = call[2][0]
        self.assertEqual((item['evidence_id'], item['label'], item['event_type']), (evidence[1]['evidence_id'], 'ANSWERABLE', 'AUTO_ESCALATED'))
        self.assertNotIn('session_title', item)
        self.assertIsNone(self.row('SELECT candidate_id FROM feedback_candidates WHERE candidate_id=%s', (cid,)))
        self.assertIsNone(self.row('SELECT candidate_id FROM feedback_candidates WHERE candidate_id=%s', (old,)))   # unreviewed before cutoff
        self.assertIsNone(self.row('SELECT candidate_id FROM candidate_provenance WHERE candidate_id=%s', (cid,)))
        job = self.row('SELECT status,result FROM jobs WHERE job_id=%s', (payload['data']['job_id'],))
        self.assertEqual((job['status'], obj(job['result'])['dataset_version']), ('SUCCEEDED', 'ds-v2'))
        status, payload, _ = c1.call('GET', '/api/v1/admin/datasets')
        self.assertEqual(status, 200, payload)
        self.assertEqual([d['version_name'] for d in payload['data']['items']][:2], ['ds-v2', 'ds-v1'] if payload['data']['items'][0]['version_name'] == 'ds-v2' else ['ds-v1', 'ds-v2'])
        self.assertNotIn('storage_uri', json.dumps(payload))

        # A refused merge (613) restores the candidates to APPROVED.
        cid2, _ = self.approved(sid, host, asker)
        self.trainer.fail['merge_labels'] = 613
        status, payload, _ = c1.call('POST', '/api/v1/admin/datasets', {'base_dataset_version': 'ds-v1', 'candidate_ids': [cid2]}, {'idempotency-key': 'k3'})
        self.assertEqual(status, 202, payload)
        self.run_jobs('DATASET')
        self.assertEqual(self.row('SELECT review_status,merge_request_id FROM feedback_candidates WHERE candidate_id=%s', (cid2,)),
                         {'review_status': 'APPROVED', 'merge_request_id': None})
        self.assertEqual(self.row('SELECT status,error_code FROM jobs WHERE job_id=%s', (payload['data']['job_id'],)),
                         {'status': 'FAILED', 'error_code': 'RESOURCE_NOT_FOUND'})

    # ---- API-57 · 58 · 59 · 60 · 61 + TRAIN · SYNC -----------------------------------------------------------------
    def test_training_flow_sync_and_stop(self):
        host, lecture, host_client = self.host_lecture(live=True)
        admin, c = self.admin()
        dataset = self.dataset_copy()
        body = {'dataset_id': str(dataset['dataset_id']), 'config': {'epochs': 3}}
        status, payload, _ = c.call('POST', '/api/v1/admin/training-runs', body, {'idempotency-key': 't1'})
        self.assertEqual((status, payload['error']['code']), (409, 'LIVE_SESSION_IN_PROGRESS'))
        c.call('PATCH', f"/api/v1/admin/sessions/{lecture['session_id']}", {'status': 'ENDED', 'reason': 'x'}, {'if-match': '"2"'})
        self.assertEqual(c.call('POST', '/api/v1/admin/training-runs', {**body, 'config': {'epochs': 11}}, {'idempotency-key': 't0'})[0], 422)
        status, payload, _ = c.call('POST', '/api/v1/admin/training-runs', body, {'idempotency-key': 't1'})
        self.assertEqual(status, 202, payload)
        run_id = payload['data']['resource_id']
        self.assertEqual(self.run_jobs('TRAIN'), 1)
        run = self.row('SELECT * FROM training_runs WHERE training_run_id=%s', (run_id,))
        self.assertEqual((run['job_id'], run['status']), ('train-0001', 'QUEUED'))
        self.assertEqual(self.trainer.calls[-1], ('start_training', ('ds-v1', run_id, {'epochs': 3})))

        self.trainer.finish_training('train-0001')
        self.moment += timedelta(minutes=1)
        self.run_jobs('SYNC')
        status, payload, _ = c.call('GET', f'/api/v1/admin/training-runs/{run_id}')
        self.assertEqual(status, 200, payload)
        data = payload['data']
        self.assertEqual((data['status'], data['stage'], data['model_version'], data['deployable'], data['progress']),
                         ('SUCCEEDED', 'register', 'judge-v1', True, 1.0))
        status, payload, _ = c.call('GET', '/api/v1/admin/model-versions?status=REGISTERED')
        model = payload['data']['items'][0]
        self.assertEqual((model['version_name'], model['threshold'], model['serving'], model['deployable']), ('judge-v1', 0.62, False, True))
        self.assertEqual(str(self.row('SELECT training_run_id FROM model_versions')['training_run_id']), run_id)
        status, payload, _ = c.call('GET', f"/api/v1/admin/evaluation-runs?dataset_id={dataset['dataset_id']}&split=TEST&scope=QUESTION")
        self.assertEqual(status, 200, payload)
        evaluators = sorted(e['evaluator'] for e in payload['data']['items'])
        self.assertEqual(evaluators, ['CUSTOM', 'SIMILARITY', 'SONNET_ONLY'])
        custom = next(e for e in payload['data']['items'] if e['evaluator'] == 'CUSTOM')
        self.assertEqual(custom['model_version_id'], model['model_version_id'])
        self.assertEqual(c.call('GET', '/api/v1/admin/evaluation-runs')[0], 422)

        status, payload, _ = c.call('DELETE', f'/api/v1/admin/training-runs/{run_id}')
        self.assertEqual((status, payload['error']['code']), (409, 'TRAINING_ALREADY_FINISHED'))

        # A second run: 609 at the training server -> 202 kept, job and run FAILED with TRAINER_BUSY (API-57).
        self.trainer.fail['start_training'] = 609
        status, payload, _ = c.call('POST', '/api/v1/admin/training-runs', body, {'idempotency-key': 't2'})
        self.run_jobs('TRAIN')
        self.assertEqual(self.row('SELECT status,error_code FROM jobs WHERE job_id=%s', (payload['data']['job_id'],)),
                         {'status': 'FAILED', 'error_code': 'TRAINER_BUSY'})
        self.assertEqual(self.row('SELECT status,error_code FROM training_runs WHERE training_run_id=%s', (payload['data']['resource_id'],)),
                         {'status': 'FAILED', 'error_code': 'TRAINER_BUSY'})

        # A third run is stopped while running: 7.4.9 is called and the SYNC picks up CANCELLED.
        status, payload, _ = c.call('POST', '/api/v1/admin/training-runs', body, {'idempotency-key': 't3'})
        third = payload['data']['resource_id']
        self.run_jobs('TRAIN')
        status, payload, _ = c.call('DELETE', f'/api/v1/admin/training-runs/{third}')
        self.assertEqual(status, 202, payload)
        self.run_jobs('SYNC')
        self.assertEqual(self.row('SELECT status FROM training_runs WHERE training_run_id=%s', (third,))['status'], 'CANCELLED')
        self.assertIn('TRAINING_REQUESTED', [r['action'] for r in self._all('SELECT action FROM audit_events')])

    # ---- API-62 · 63 + DEPLOY ---------------------------------------------------------------------------------------
    def test_deploy_switch_rollback_and_conflicts(self):
        admin, c = self.admin()
        self.dataset_copy()
        self.trainer.start_training('ds-v1', uid())
        self.trainer.finish_training('train-0001', 'judge-v1')
        self.trainer.start_training('ds-v1', uid())
        self.trainer.finish_training('train-0002', 'judge-v2', deployable=False)
        self.moment += timedelta(minutes=1)
        self.queue_full_sync()
        self.run_jobs('SYNC')
        models = {m['version_name']: m for m in c.call('GET', '/api/v1/admin/model-versions')[1]['data']['items']}
        status, payload, _ = c.call('GET', '/api/v1/admin/deployments')
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['active']['mode'], payload['data']['active']['stale']), ('LLM_ONLY', False))

        body = {'mode': 'CUSTOM', 'model_version_id': models['judge-v2']['model_version_id'], 'expected_active_deployment_id': None, 'reason': '배포'}
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', body, {'idempotency-key': 'd0'})
        self.assertEqual((status, payload['error']['code']), (409, 'MODEL_NOT_DEPLOYABLE'))
        body['model_version_id'] = models['judge-v1']['model_version_id']
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', {**body, 'expected_active_deployment_id': uid()}, {'idempotency-key': 'd1'})
        self.assertEqual((status, payload['error']['code']), (409, 'VERSION_CONFLICT'))
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', body, {'idempotency-key': 'd2'})
        self.assertEqual(status, 202, payload)
        first = payload['data']['resource_id']
        self.assertEqual(self.trainer.calls[-1], ('switch', ('judge-v1',)))
        self.assertEqual(self.run_jobs('DEPLOY'), 1)
        d = self.row('SELECT status,operation,serving_snapshot FROM deployments WHERE deployment_id=%s', (first,))
        self.assertEqual((d['status'], d['operation'], obj(d['serving_snapshot'])['serving_version']), ('ACTIVE', 'SWITCH', 'judge-v1'))
        self.assertTrue(self.row("SELECT serving FROM model_versions WHERE version_name='judge-v1'")['serving'])
        with self.db.transaction() as tx:
            current = active_serving(tx)
        self.assertEqual((current['mode'], current['threshold'], current['model_version_id']),
                         ('CUSTOM', 0.62, models['judge-v1']['model_version_id']))

        # LLM_ONLY goes through 7.4.23 with the explicit target and supersedes the previous ACTIVE row.
        llm = {'mode': 'LLM_ONLY', 'expected_active_deployment_id': first, 'reason': '철회 증가'}
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', llm, {'idempotency-key': 'd3'})
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.trainer.calls[-1], ('rollback', ('LLM_ONLY',)))
        self.run_jobs('DEPLOY')
        second = payload['data']['resource_id']
        self.assertEqual(self.row('SELECT status FROM deployments WHERE deployment_id=%s', (first,))['status'], 'SUPERSEDED')
        self.assertEqual(self.row('SELECT status FROM deployments WHERE deployment_id=%s', (second,))['status'], 'ACTIVE')

        # Back to judge-v1: served before, so ROLLBACK; a 610 rejection is a synchronous 409 with a FAILED history row.
        back = {'mode': 'CUSTOM', 'model_version_id': models['judge-v1']['model_version_id'], 'expected_active_deployment_id': second, 'reason': '복구'}
        self.trainer.fail['rollback'] = 610
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', back, {'idempotency-key': 'd4'})
        self.assertEqual((status, payload['error']['code']), (409, 'MODEL_NOT_DEPLOYABLE'))
        self.assertEqual(self.row("SELECT count(*) AS n FROM deployments WHERE status='FAILED'")['n'], 1)

        # Training server unreachable: 202, the DEPLOY job checks 7.4.21 and sends once when not applied.
        self.trainer.down = True
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments', back, {'idempotency-key': 'd5'})
        self.assertEqual(status, 202, payload)
        status, history, _ = c.call('GET', '/api/v1/admin/deployments')
        self.assertTrue(history['data']['active']['stale'])
        latest = next(i for i in history['data']['items'] if i['deployment_id'] == payload['data']['resource_id'])
        self.assertEqual((latest['requested_by'], latest['route'], latest['status']), ('조현준', 'ROLLBACK', 'PENDING'))
        self.assertNotIn('requested_by_user_id', latest)
        self.trainer.down = False
        self.run_jobs('DEPLOY')
        self.assertEqual(self.row('SELECT status FROM deployments WHERE deployment_id=%s', (payload['data']['resource_id'],))['status'], 'ACTIVE')
        self.assertEqual(self.trainer.deployment['serving_version'], 'judge-v1')
        self.assertEqual([r['action'] for r in self._all("SELECT action FROM audit_events WHERE target_type='DEPLOYMENT'")].count('DEPLOYMENT_REQUESTED'), 4)

    def test_serving_change_on_training_server_is_synced_and_audited(self):
        admin, c = self.admin()
        status, payload, _ = c.call('POST', '/api/v1/admin/deployments',
                                    {'mode': 'LLM_ONLY', 'expected_active_deployment_id': None, 'reason': '초기'}, {'idempotency-key': 'a'})
        self.run_jobs('DEPLOY')
        self.trainer.deployment['llm_only_threshold'] = 75   # changed directly on the department server
        self.queue_full_sync()
        self.run_jobs('SYNC')
        snap = obj(self.row("SELECT serving_snapshot FROM deployments WHERE status='ACTIVE'")['serving_snapshot'])
        self.assertEqual(snap['llm_only_threshold'], 75)
        self.assertTrue(self.row("SELECT audit_event_id FROM audit_events WHERE action='SERVING_CONFIG_SYNCED'"))

    # ---- API-64 · 65 --------------------------------------------------------------------------------------------------
    def test_budget_settings_and_metrics(self):
        host, lecture, _ = self.host_lecture()
        sid = lecture['session_id']
        asker = self.verified('asker@example.com')
        admin, c = self.admin()
        self.assertEqual(c.call('PUT', '/api/v1/admin/budget-settings', {'monthly_budget_krw': 0, 'usd_krw_rate': 1400, 'hard_cap_enabled': True, 'reason': 'x'})[0], 422)
        status, payload, _ = c.call('PUT', '/api/v1/admin/budget-settings',
                                    {'monthly_budget_krw': 100000, 'usd_krw_rate': 1400, 'hard_cap_enabled': True, 'reason': '월 예산'})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['monthly_budget_krw'], payload['data']['hard_cap_enabled']), (100000, True))
        cid, _ = self.candidate(sid, host, asker)
        with self.db.transaction() as tx:
            q = tx.one('SELECT question_id FROM questions')['question_id']
            tx.execute("UPDATE questions SET decision_snapshot=%s,finalized_at=%s WHERE question_id=%s",
                       (dumps({'route': 'FALLBACK_LLM_ONLY', 'progress_ms': 400}), self.moment + timedelta(seconds=3), q))
            for cost, outcome in (('40', 'SUCCESS'), ('20', 'RESERVED')):
                tx.execute("""INSERT INTO api_usage(usage_id,session_id,provider,model_name,operation,estimated_cost,currency,latency_ms,outcome,request_id,created_at)
                    VALUES(%s,%s,'ANTHROPIC','sonnet','ANSWER',%s,'USD',10,%s,%s,%s)""", (uid(), sid, cost, outcome, uid(), self.moment))
        start = (self.moment - timedelta(days=1)).isoformat().replace('+', '%2B')
        end = (self.moment + timedelta(days=1)).isoformat().replace('+', '%2B')
        status, payload, _ = c.call('GET', f'/api/v1/admin/metrics?from={start}&to={end}&group_by=session')
        self.assertEqual(status, 200, payload)
        group = next(g for g in payload['data']['groups'] if g['session_id'] == sid)
        self.assertEqual((group['question_count'], group['escalated_count'], group['fallback_count'], group['final_p50_ms']), (1, 1, 1, 3000))
        self.assertEqual(group['escalation_rate'], 1.0)
        self.assertIsNone(group['first_token_p50_ms'])
        self.assertEqual(group['estimated_cost_by_currency'], {'USD': 60.0})
        budget = payload['data']['budget']
        self.assertEqual((budget['month_to_date_krw'], budget['reserved_krw'], budget['cap_reached'], budget['warning']), (56000.0, 28000.0, False, True))
        self.assertNotIn('해시 충돌은?', json.dumps(payload, ensure_ascii=False))
        far = (self.moment + timedelta(days=100)).isoformat().replace('+', '%2B')
        self.assertEqual(c.call('GET', f'/api/v1/admin/metrics?from={start}&to={far}&group_by=day')[0], 422)
        self.assertTrue(cid)


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGAdminTests(PostgresMixin, AdminTests):
    pass
