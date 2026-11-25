from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet

from easyq.adapters import LocalStorage
from easyq.config import Settings
from easyq.contracts import APIError, Request, dt, dumps, obj, uid
from easyq.http import HTTPApplication, ROUTES
from easyq.retrieval import PPTX, PDF, check_vectors, chunks, extract_pages, overlap_ratio, validate_document
from easyq.security import decrypt, digest, password_matches
from easyq.service import Service
from easyq.worker import Worker
from .support import Client, SQLiteDatabase, pdf, pptx


class Tokenizer:
    def tokenize(self, value):
        from types import SimpleNamespace
        return [SimpleNamespace(form=x, tag='NNG') for x in value.split()]


class Embedder:
    def embed(self, values):
        return [[1.0] + [0.0] * 767 for _ in values]


class Mailer:
    def __init__(self):
        self.messages = []

    def send(self, *message):
        self.messages.append(message)


class GuestClient:
    def __init__(self, auth):
        self.auth, self.fail = auth, False

    def issue(self, pid, rid):
        if self.fail:
            raise RuntimeError('Auth unavailable')
        return self.auth.execute(Request('POST', '/internal/v1/guest-sessions',
            {'x-service-key': self.auth.settings.internal_key}, {'participant_id': pid, 'request_id': rid}), 'auth').data

    def revoke(self, body):
        return self.auth.execute(Request('POST', '/internal/v1/session-revocations',
            {'x-service-key': self.auth.settings.internal_key}, body), 'auth').data


class APITests(unittest.TestCase):
    def setUp(self):
        self.moment = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('unused-test-url', 'https://easyq.test', 'test-hmac-' + 'x' * 40,
            Fernet.generate_key().decode(), 'test-service-' + 'y' * 40, storage_backend='local', storage_root=self.tmp.name)
        self.db = SQLiteDatabase(lambda: self.moment)
        self.service = Service(self.db, self.settings, LocalStorage(self.tmp.name), clock=lambda: self.moment)
        self.guest = GuestClient(self.service)
        self.service.guest_client = self.guest
        self.auth = Client(HTTPApplication(self.service, 'auth'))
        self.api = Client(HTTPApplication(self.service, 'backend'))
        self.mailer = Mailer()
        self.worker = Worker(self.service, Embedder(), self.mailer, Tokenizer())

    def tearDown(self):
        self.db.connection.close()
        self.tmp.cleanup()

    def row(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.one(sql, args)

    def register(self, address='member@example.com'):
        status, payload, _ = self.auth.call('POST', '/api/v1/users', {'email': address, 'password': 'easyq2026', 'display_name': '조현준',
            'consents': {'SERVICE': {'policy_version': 'service-v1', 'granted': True}}})
        self.assertEqual(status, 201, payload)
        return payload['data']['user_id']

    def verified(self, address='member@example.com'):
        user = self.register(address)
        row = self.row('SELECT * FROM email_verifications WHERE user_id=%s', (user,))
        code = decrypt(self.settings.encryption_key, row['delivery_ciphertext'])
        status, payload, headers = self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': address, 'code': code})
        self.assertEqual(status, 200, payload)
        self.assertNotIn('set-cookie', headers)
        return user

    def login(self, address='member@example.com'):
        user = self.verified(address)
        status, payload, headers = self.auth.call('POST', '/api/v1/auth/sessions', {'email': address, 'password': 'easyq2026'})
        self.assertEqual(status, 201, payload)
        self.assertIn('HttpOnly', headers['set-cookie'])
        self.assertIn('Secure', headers['set-cookie'])
        self.api.cookies, self.api.csrf = self.auth.cookies.copy(), self.auth.csrf
        return user

    def lecture(self):
        self.login()
        status, payload, headers = self.api.call('POST', '/api/v1/sessions', {'title': 'EasyQ 자료구조', 'scheduled_at': self.moment.isoformat(), 'description': '해시'})
        self.assertEqual(status, 201, payload)
        return payload['data'], headers['etag']

    def test_registration_is_atomic_and_stores_only_protected_credentials(self):
        user = self.register('MEMBER@EXAMPLE.COM')
        stored = self.row('SELECT * FROM users WHERE user_id=%s', (user,))
        self.assertEqual(stored['email'], 'member@example.com')
        self.assertEqual(stored['status'], 'PENDING_VERIFICATION')
        self.assertTrue(password_matches('easyq2026', stored['password_hash']))
        self.assertNotIn('easyq2026', stored['password_hash'])
        self.assertEqual(self.row('SELECT count(*) n FROM consents')['n'], 2)
        self.assertEqual(self.row('SELECT count(*) n FROM jobs')['n'], 1)
        self.assertFalse(self.row("SELECT granted FROM consents WHERE purpose='TRAINING'")['granted'])
        self.assertFalse(self.auth.cookies)

    def test_registration_rollback_on_storage_error(self):
        original = self.service.queue_mail
        def fail(*args):
            raise RuntimeError('Do not expose secret')
        self.service.queue_mail = fail
        status, payload, _ = self.auth.call('POST', '/api/v1/users', {'email': 'a@example.com', 'password': 'easyq2026', 'display_name': 'A', 'consents': {'SERVICE': {'policy_version': 'v1', 'granted': True}}})
        self.assertEqual(status, 503)
        self.assertEqual(self.row('SELECT count(*) n FROM users')['n'], 0)
        self.assertNotIn('secret', dumps(payload))
        self.service.queue_mail = original

    def test_signup_validation_and_origin(self):
        for password in ['short1', 'abcdefgh', '12345678', 'a1' * 11]:
            status, _, _ = self.auth.call('POST', '/api/v1/users', {'email': 'a@example.com', 'password': password, 'display_name': 'A', 'consents': {'SERVICE': {'policy_version': 'v1', 'granted': True}}})
            self.assertEqual(status, 422)
        self.assertEqual(self.auth.call('POST', '/api/v1/users', {}, headers={'origin': 'https://evil.test'})[0], 403)

    def test_wrong_codes_commit_attempts_and_lock_after_five(self):
        user = self.register()
        record = self.row('SELECT * FROM email_verifications')
        real = decrypt(self.settings.encryption_key, record['delivery_ciphertext'])
        wrong = '000001' if real != '000001' else '000002'
        for index in range(5):
            status, payload, _ = self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': 'member@example.com', 'code': wrong})
            self.assertEqual(status, 429 if index == 4 else 422, payload)
        self.assertEqual(self.row('SELECT attempt_count FROM email_verifications')['attempt_count'], 5)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': 'member@example.com', 'code': real})[0], 429)
        self.assertEqual(self.row('SELECT status FROM users')['status'], 'PENDING_VERIFICATION')

    def test_code_expiry_and_reuse(self):
        self.register()
        row = self.row('SELECT * FROM email_verifications')
        code = decrypt(self.settings.encryption_key, row['delivery_ciphertext'])
        self.moment += timedelta(minutes=11)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': 'member@example.com', 'code': code})[0], 422)

    def test_verification_consumed_once(self):
        self.register()
        code = decrypt(self.settings.encryption_key, self.row('SELECT * FROM email_verifications')['delivery_ciphertext'])
        body = {'email': 'member@example.com', 'code': code}
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', body)[0], 200)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/email-verifications/confirm', body)[0], 422)

    def test_resend_revokes_old_code_without_changing_password(self):
        self.register()
        old = self.row('SELECT * FROM email_verifications')
        self.moment += timedelta(seconds=61)
        status, _, _ = self.auth.call('POST', '/api/v1/auth/email-verifications', {'email': 'member@example.com'})
        self.assertEqual(status, 202)
        self.assertIsNotNone(self.row('SELECT revoked_at FROM email_verifications WHERE verification_id=%s', (old['verification_id'],))['revoked_at'])
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE status='CANCELLED'")['n'], 1)

    def test_mail_rate_returns_retry_after(self):
        self.register()
        status, _, headers = self.auth.call('POST', '/api/v1/auth/email-verifications', {'email': 'member@example.com'})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers['retry-after']), 0)

    def test_login_requires_verified_account_and_wrong_password_hides_status(self):
        self.register()
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'wrong123'})[0], 401)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})[0], 403)

    def test_login_does_not_issue_more_verification_mail(self):
        self.login()
        self.assertEqual(self.row('SELECT count(*) n FROM email_verifications')['n'], 1)
        me = self.auth.call('GET', '/api/v1/me')
        self.assertEqual(me[0], 200)
        self.assertNotIn('password_hash', dumps(me[1]))

    def test_csrf_rotation_invalidates_old_value(self):
        self.login()
        old = self.auth.csrf
        status, _, _ = self.auth.call('POST', '/api/v1/auth/csrf-tokens', {}, headers={'x-easyq-client': 'web'})
        self.assertEqual(status, 200)
        self.assertNotEqual(old, self.auth.csrf)
        self.assertEqual(self.auth.call('DELETE', '/api/v1/auth/sessions/current', headers={'x-csrf-token': old})[0], 403)
        self.assertEqual(self.auth.call('DELETE', '/api/v1/auth/sessions/current')[0], 204)
        self.assertEqual(self.auth.call('DELETE', '/api/v1/auth/sessions/current')[0], 204)
        self.assertEqual(self.auth.call('GET', '/api/v1/me')[0], 401)

    def test_expired_session_is_rejected(self):
        self.login()
        self.moment += timedelta(hours=12, seconds=1)
        self.assertEqual(self.auth.call('GET', '/api/v1/me')[0], 401)

    def test_password_reset_is_single_use_and_revokes_all_sessions(self):
        self.login()
        self.moment += timedelta(seconds=61)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/password-reset-requests', {'email': 'member@example.com'})[0], 202)
        record = self.row('SELECT * FROM password_resets')
        raw = decrypt(self.settings.encryption_key, record['delivery_ciphertext'])
        body = {'token': raw, 'new_password': 'easyq2027'}
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/password-resets', body)[0], 204)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/password-resets', body)[0], 422)
        self.assertEqual(self.auth.call('GET', '/api/v1/me')[0], 401)
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2027'})[0], 201)

    def test_password_reset_unknown_email_uses_same_response(self):
        status, payload, _ = self.auth.call('POST', '/api/v1/auth/password-reset-requests', {'email': 'unknown@example.com'})
        self.assertEqual(status, 202)
        self.assertEqual(payload['data'], {})
        self.assertEqual(self.row('SELECT count(*) n FROM password_resets')['n'], 0)

    def test_lecture_creation_matches_final_design_and_state_start_needs_no_document(self):
        s, etag = self.lecture()
        self.assertEqual(s['status'], 'DRAFT')
        self.assertRegex(s['join_code'], r'^[A-HJ-NP-Z2-9]{5}$')
        path = '/api/v1/sessions/' + s['session_id']
        status, payload, _ = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertEqual(status, 200, payload)
        auto = self.row("SELECT * FROM jobs WHERE job_type='AUTO_END'")
        self.assertEqual(dt(auto['available_at']), self.moment + timedelta(hours=12))

    def test_version_and_owner_checks(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        self.assertEqual(self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': '"999"'})[0], 409)
        self.api.csrf = 'bad'
        self.assertEqual(self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})[0], 403)
        self.api.cookies = {}
        self.assertEqual(self.api.call('GET', path)[0], 401)

    def test_join_link_rotation_revokes_immediate_previous_code(self):
        s, etag = self.lecture()
        status, payload, _ = self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/join-links', {}, headers={'if-match': etag})
        self.assertEqual(status, 201)
        visitor = Client(HTTPApplication(self.service, 'backend'))
        self.assertEqual(visitor.call('GET', '/api/v1/join-codes/' + s['join_code'])[0], 410)
        self.assertEqual(visitor.call('GET', '/api/v1/join-codes/' + payload['data']['join_code'].lower())[0], 200)

    def test_guest_can_join_draft_and_is_scoped_to_lecture(self):
        s, _ = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': {'policy_version': 'v1', 'granted': True}})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload['data']['kind'], 'GUEST')
        self.assertEqual(visitor.call('GET', '/api/v1/sessions/' + s['session_id'])[0], 200)
        self.assertEqual(visitor.call('GET', '/api/v1/sessions/' + uid())[0], 404)
        self.assertEqual(visitor.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': '"1"'})[0], 404)

    def test_failed_guest_auth_retry_does_not_duplicate_participant(self):
        s, _ = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        body = {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': {'policy_version': 'v1', 'granted': True}}
        self.guest.fail = True
        self.assertEqual(visitor.call('POST', '/api/v1/participations', body)[0], 503)
        self.assertEqual(self.row('SELECT count(*) n FROM participants')['n'], 1)
        self.guest.fail = False
        self.assertEqual(visitor.call('POST', '/api/v1/participations', body)[0], 200)
        self.assertEqual(self.row('SELECT count(*) n FROM participants')['n'], 1)

    def test_capacity_is_fifty_and_member_reentry_is_idempotent(self):
        s, _ = self.lecture()
        host_cookie = self.api.cookies.copy()
        self.moment += timedelta(seconds=61)
        self.auth.cookies = {}
        self.login('audience@example.com')
        body = {'join_code': s['join_code']}
        first = self.api.call('POST', '/api/v1/participations', body)
        self.assertEqual(first[0], 201, first[1])
        with self.db.transaction() as tx:
            for _ in range(49):
                tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname) VALUES(%s,%s,'GUEST','G')", (uid(), s['session_id']))
        self.assertEqual(self.api.call('POST', '/api/v1/participations', body)[0], 200)
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, _, _ = visitor.call('POST', '/api/v1/participations', {**body, 'nickname': '추가', 'service_consent': {'policy_version': 'v1', 'granted': True}})
        self.assertEqual(status, 409)

    def test_guest_entry_cookie_finds_same_participant_after_session_cookie_loss(self):
        # 7.1.1 / API-19: the signed entry-confirmation cookie re-identifies the guest; it is not authentication.
        s, _ = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        body = {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': True}
        status, payload, _ = visitor.call('POST', '/api/v1/participations', body)
        self.assertEqual(status, 201, payload)
        self.assertIn('easyq_join_retry', visitor.cookies)
        pid = payload['data']['participant_id']
        visitor.cookies.pop('easyq_session')
        status, payload, _ = visitor.call('POST', '/api/v1/participations', body)
        self.assertEqual((status, payload['data']['participant_id']), (200, pid))
        self.assertEqual(self.row('SELECT count(*) n FROM participants')['n'], 1)
        # A tampered or foreign-lecture cookie never reuses the participant.
        visitor.cookies['easyq_join_retry'] = visitor.cookies['easyq_join_retry'][:-1] + '0'
        visitor.cookies.pop('easyq_session')
        status, payload, _ = visitor.call('POST', '/api/v1/participations', body)
        self.assertEqual(status, 201, payload)
        self.assertNotEqual(payload['data']['participant_id'], pid)

    def test_capacity_ignores_guests_without_live_session(self):
        s, _ = self.lecture()
        with self.db.transaction() as tx:
            for _ in range(50):
                tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname,archived,joined_at) VALUES(%s,%s,'GUEST','G',false,%s)",
                           (uid(), s['session_id'], self.moment - timedelta(hours=1)))
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '새 청중', 'service_consent': True})
        self.assertEqual(status, 201, payload)

    def test_host_service_withdrawal_keeps_draft_and_blocks_start(self):
        # 7.3.3 API-20: DRAFT stays; starting needs SERVICE consent again.
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        self.moment += timedelta(seconds=1)
        self.assertEqual(self.api.call('POST', path + '/consents', {'purpose': 'SERVICE', 'policy_version': 'service-v1', 'granted': False})[0], 201)
        self.assertEqual(self.row('SELECT status FROM lecture_sessions')['status'], 'DRAFT')
        self.assertEqual(self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})[0], 403)

    def test_stt_consent_is_host_only_and_connecting_is_not_running(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        status, payload, h = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertEqual(status, 200)
        self.assertEqual(self.api.call('PATCH', path, {'stt_status': 'RUNNING'}, headers={'if-match': h['etag']})[0], 403)
        self.assertEqual(self.api.call('POST', path + '/consents', {'purpose': 'STT', 'policy_version': 'stt-v1', 'granted': True})[0], 201)
        status, payload, _ = self.api.call('PATCH', path, {'stt_status': 'RUNNING'}, headers={'if-match': h['etag']})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data']['stt_status'], 'CONNECTING')

    def test_ending_rejects_new_guests_and_sets_ninety_day_retention(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        _, _, headers = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        status, payload, _ = self.api.call('PATCH', path, {'status': 'ENDED'}, headers={'if-match': headers['etag']})
        self.assertEqual(status, 200)
        self.assertEqual(dt(payload['data']['raw_data_expires_at']), self.moment + timedelta(days=90))
        visitor = Client(HTTPApplication(self.service, 'backend'))
        self.assertEqual(visitor.call('GET', '/api/v1/join-codes/' + s['join_code'])[0], 409)

    def test_training_withdrawal_overrides_older_lecture_grant(self):
        s, _ = self.lecture()
        self.assertEqual(self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/consents', {'purpose': 'TRAINING', 'policy_version': 'v1', 'granted': True})[0], 201)
        self.moment += timedelta(seconds=1)
        self.assertEqual(self.api.call('POST', '/api/v1/me/consents', {'purpose': 'TRAINING', 'policy_version': 'v1', 'granted': False})[0], 201)
        result = self.api.call('GET', '/api/v1/sessions/' + s['session_id'])
        self.assertFalse(result[1]['data']['my_consents']['TRAINING']['granted'])
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='CLEANUP'")['n'], 1)

    def test_upload_worker_indexes_pptx_and_idempotency_conflicts(self):
        s, _ = self.lecture()
        path = '/api/v1/sessions/' + s['session_id'] + '/documents'
        key = uid()
        upload = ('lecture.pptx', PPTX, pptx())
        first = self.api.call('POST', path, upload=upload, headers={'idempotency-key': key})
        self.assertEqual(first[0], 202, first[1])
        second = self.api.call('POST', path, upload=upload, headers={'idempotency-key': key})
        self.assertEqual(first[1]['data'], second[1]['data'])
        different = self.api.call('POST', path, upload=('lecture.pptx', PPTX, pptx(('Different',))), headers={'idempotency-key': key})
        self.assertEqual(different[0], 409)
        for _ in range(4):
            self.worker.run_one()
        doc = self.row('SELECT * FROM documents')
        self.assertEqual(doc['status'], 'READY', doc)
        self.assertEqual(doc['page_count'], 2)
        self.assertEqual(self.row('SELECT count(*) n FROM knowledge_chunks')['n'], 2)
        self.assertEqual(self.api.call('GET', path)[0], 200)
        self.assertEqual(self.api.call('GET', first[2]['location'])[0], 200)

    def test_upload_allowed_while_live(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertEqual(self.api.call('POST', path + '/documents', upload=('live.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})[0], 202)

    def test_document_bad_format_and_oversize_rejected(self):
        s, _ = self.lecture()
        path = '/api/v1/sessions/' + s['session_id'] + '/documents'
        self.assertEqual(self.api.call('POST', path, upload=('fake.pdf', PDF, b'not pdf'), headers={'idempotency-key': uid()})[0], 415)
        self.service.settings = replace(self.settings, upload_limit=5)
        self.assertEqual(self.api.call('POST', path, upload=('big.pdf', PDF, b'%PDF-oversize'), headers={'idempotency-key': uid()})[0], 413)

    def test_worker_sends_encrypted_mail_and_clears_delivery_payload(self):
        self.register()
        self.assertTrue(self.worker.run_one())
        self.assertEqual(len(self.mailer.messages), 1)
        record = self.row('SELECT * FROM email_verifications')
        self.assertIsNone(record['delivery_ciphertext'])
        self.assertIsNotNone(record['sent_at'])

    def test_stale_lease_cannot_finish_job(self):
        self.register()
        first = self.worker.claim()
        self.moment += timedelta(minutes=6)
        second = self.worker.claim()
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        with self.db.transaction() as tx:
            with self.assertRaisesRegex(APIError, 'LEASE_LOST'):
                self.worker.finish(tx, first)

    def test_auto_end_executes_after_twelve_hours(self):
        s, etag = self.lecture()
        self.api.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': etag})
        self.moment += timedelta(hours=12, seconds=1)
        for _ in range(5):
            self.worker.run_one()
        row = self.row('SELECT status,end_reason FROM lecture_sessions')
        self.assertEqual(row, {'status': 'ENDED', 'end_reason': 'AUTO_TIMEOUT'})

    def test_malformed_json_and_wrong_process_do_not_execute(self):
        self.assertEqual(self.auth.call('POST', '/api/v1/users', raw=b'{bad', headers={'content-type': 'application/json'})[0], 422)
        self.assertEqual(self.api.call('POST', '/api/v1/users', {})[0], 404)
        self.assertEqual(self.auth.call('POST', '/internal/v1/guest-sessions', {})[0], 403)

    def test_chunking_preserves_pages_and_embedding_shape(self):
        result = chunks(['a' * 1000, 'second page'])
        self.assertEqual([x['page_no'] for x in result], [1, 1, 2])
        with self.assertRaises(APIError):
            check_vectors([[float('nan')] * 768], 1)
        with self.assertRaises(APIError):
            check_vectors([[1.0] * 767], 1)
        self.assertEqual(extract_pages('test.pptx', pptx(('First', 'Second'))), ['First', 'Second'])
        self.assertEqual(overlap_ratio('A B C D E F', 'a b c d e f'), 1.0)

    def test_final_hwp_boolean_guest_consent_example(self):
        s, _ = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중07', 'service_consent': True, 'training_consent': False})
        self.assertEqual(status, 201, payload)
        self.assertEqual(self.row("SELECT policy_version FROM consents WHERE participant_id=%s AND purpose='SERVICE'", (payload['data']['participant_id'],))['policy_version'], 'service-v1')

    def test_budget_holds_ingest_before_call_and_resumes_after_cap_disabled(self):
        s, _ = self.lecture()
        self.service.settings = replace(self.settings, embedding_max_batch_cost_usd='0.01', embedding_price_version='test-v1')
        self.worker.settings = self.service.settings
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO app_settings(setting_key,value,hard_cap_enabled) VALUES('MONTHLY_API_BUDGET',%s,true)", (dumps({'monthly_budget_krw': 1, 'usd_krw_rate': 1400}),))
        self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents', upload=('budget.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
        for _ in range(3):
            self.worker.run_one()
        job = self.row("SELECT * FROM jobs WHERE job_type='INGEST'")
        self.assertEqual(job['status'], 'HELD', job)
        self.assertEqual(job['error_code'], 'BUDGET_EXCEEDED')
        # 6.3.5: a held document stays PENDING with the budget reason, not FAILED.
        document = self.row('SELECT status,error_code FROM documents')
        self.assertEqual((document['status'], document['error_code']), ('PENDING', 'BUDGET_EXCEEDED'))
        self.assertEqual(self.row('SELECT count(*) n FROM api_usage')['n'], 0)
        with self.db.transaction() as tx:
            tx.execute("UPDATE app_settings SET hard_cap_enabled=false WHERE setting_key='MONTHLY_API_BUDGET'")
        self.worker.maintenance()
        self.worker.run_one()
        self.assertEqual(self.row('SELECT status FROM documents')['status'], 'READY')
        self.assertEqual(self.row('SELECT outcome FROM api_usage')['outcome'], 'SUCCESS')

    def test_host_session_renewal_requires_valid_owner_and_fifteen_minute_interval(self):
        s, etag = self.lecture()
        self.api.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': etag})
        a = self.row('SELECT * FROM auth_sessions')
        req = Request('POST', '/internal/v1/host-session-renewals', {'x-service-key': self.settings.internal_key},
            {'auth_session_id': str(a['auth_session_id']), 'session_id': s['session_id'], 'request_id': uid()})
        self.moment += timedelta(hours=1)
        first = self.service.execute(req, 'auth')
        self.assertTrue(first.data['renewed'])
        self.assertFalse(self.service.execute(req, 'auth').data['renewed'])
        self.moment += timedelta(hours=24)
        with self.assertRaises(APIError):
            self.service.execute(req, 'auth')

    def test_worker_key_cannot_mint_guest_sessions(self):
        self.service.settings = replace(self.settings, worker_internal_key='worker-' + 'z' * 40)
        req = Request('POST', '/internal/v1/guest-sessions', {'x-service-key': self.service.settings.worker_internal_key}, {'participant_id': uid(), 'request_id': uid()})
        with self.assertRaises(APIError) as caught:
            self.service.execute(req, 'auth')
        self.assertEqual(caught.exception.status, 403)

    def test_worker_dependency_failure_retries_then_records_failure(self):
        s, _ = self.lecture()
        class Broken:
            def embed(self, values):
                raise RuntimeError('provider password must not leak')
        self.worker.embedder = Broken()
        self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents', upload=('retry.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
        for _ in range(6):
            self.worker.run_one()
            self.moment += timedelta(minutes=3)
        row = self.row('SELECT * FROM documents')
        self.assertEqual(row['status'], 'FAILED')
        self.assertEqual(row['error_code'], 'WORKER_DEPENDENCY_UNAVAILABLE')

    def test_liveness_does_not_require_database(self):
        original = self.service.db
        self.service.db = None
        self.assertEqual(self.auth.call('GET', '/health/live')[0], 200)
        self.assertEqual(self.auth.call('GET', '/health/ready')[0], 503)
        self.service.db = original

    def test_ended_lecture_rejects_existing_guest_cookie_before_auth_revocation(self):
        s, etag = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': True})
        path = '/api/v1/sessions/' + s['session_id']
        _, _, headers = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.api.call('PATCH', path, {'status': 'ENDED'}, headers={'if-match': headers['etag']})
        self.assertEqual(visitor.call('GET', path)[0], 401)

    def second_lecture(self, title='EasyQ 운영체제'):
        status, payload, _ = self.api.call('POST', '/api/v1/sessions', {'title': title, 'scheduled_at': self.moment.isoformat()})
        self.assertEqual(status, 201, payload)
        return payload['data']

    def test_guest_of_ended_lecture_can_join_another_lecture(self):
        a, etag = self.lecture()
        b = self.second_lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        entry = {'nickname': '청중', 'service_consent': True}
        self.assertEqual(visitor.call('POST', '/api/v1/participations', {'join_code': a['join_code'], **entry})[0], 201)
        # While lecture A is still open, its guest cookie must not block lecture B.
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': b['join_code'], **entry})
        self.assertEqual(status, 201, payload)
        self.assertEqual(visitor.call('GET', '/api/v1/sessions/' + b['session_id'])[0], 200)
        # After lecture A ends, a stale A cookie still allows preview and entry elsewhere.
        visitor2 = Client(HTTPApplication(self.service, 'backend'))
        visitor2.call('POST', '/api/v1/participations', {'join_code': a['join_code'], **entry})
        path = '/api/v1/sessions/' + a['session_id']
        _, _, h = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.api.call('PATCH', path, {'status': 'ENDED'}, headers={'if-match': h['etag']})
        self.assertEqual(visitor2.call('GET', '/api/v1/join-codes/' + b['join_code'])[0], 200)
        status, payload, _ = visitor2.call('POST', '/api/v1/participations', {'join_code': b['join_code'], **entry})
        self.assertEqual(status, 201, payload)

    def test_invalid_member_cookie_still_requires_login_to_join(self):
        s, _ = self.lecture()
        self.moment += timedelta(hours=13)
        self.assertEqual(self.api.call('POST', '/api/v1/participations', {'join_code': s['join_code']})[0], 401)

    def test_guest_idempotency_key_cannot_take_over_another_guest(self):
        s, _ = self.lecture()
        victim = Client(HTTPApplication(self.service, 'backend'))
        _, payload, _ = victim.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '피해자', 'service_consent': True})
        victim_pid = payload['data']['participant_id']
        attacker = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = attacker.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '공격자', 'service_consent': True},
            headers={'idempotency-key': victim_pid})
        self.assertEqual(status, 201, payload)
        self.assertNotEqual(payload['data']['participant_id'], victim_pid)
        self.assertEqual(victim.call('GET', '/api/v1/sessions/' + s['session_id'])[0], 200)
        # The legitimate client retrying with its own key gets the same participant back.
        key = uid()
        retry = Client(HTTPApplication(self.service, 'backend'))
        body = {'join_code': s['join_code'], 'nickname': '재시도', 'service_consent': True}
        self.guest.fail = True
        self.assertEqual(retry.call('POST', '/api/v1/participations', body, headers={'idempotency-key': key})[0], 503)
        self.guest.fail = False
        retry.cookies.pop('easyq_join_retry', None)
        status, payload, _ = retry.call('POST', '/api/v1/participations', body, headers={'idempotency-key': key})
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(payload['data']['participant_id'], key)

    def test_member_can_rejoin_after_withdrawing_lecture_service_consent(self):
        s, _ = self.lecture()
        self.moment += timedelta(seconds=61)
        self.auth.cookies = {}
        self.login('audience@example.com')
        body = {'join_code': s['join_code']}
        first = self.api.call('POST', '/api/v1/participations', body)
        self.assertEqual(first[0], 201, first[1])
        pid = first[1]['data']['participant_id']
        path = '/api/v1/sessions/' + s['session_id']
        self.moment += timedelta(seconds=1)
        self.assertEqual(self.api.call('POST', path + '/consents', {'purpose': 'SERVICE', 'policy_version': 'service-v1', 'granted': False})[0], 201)
        self.assertEqual(self.api.call('GET', path)[0], 404)
        self.moment += timedelta(seconds=1)
        self.assertEqual(self.api.call('POST', '/api/v1/participations', body)[0], 403)
        status, payload, _ = self.api.call('POST', '/api/v1/participations', {**body, 'service_consent': True})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload['data']['participant_id'], pid)
        result = self.api.call('GET', path)
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]['data']['my_consents']['SERVICE']['granted'])

    def test_signup_optional_refusal_allows_later_lecture_training_grant(self):
        s, _ = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        self.moment += timedelta(seconds=1)
        self.api.call('POST', path + '/consents', {'purpose': 'TRAINING', 'policy_version': 'v1', 'granted': True})
        self.assertTrue(self.api.call('GET', path)[1]['data']['my_consents']['TRAINING']['granted'])

    def test_successful_logins_do_not_consume_failed_login_quota(self):
        self.login()
        for _ in range(11):
            self.assertEqual(self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})[0], 201)
        self.assertEqual(self.row("SELECT count(*) n FROM auth_rate_limits WHERE request_count>0")['n'], 3)  # mail IP/email + confirm IP

    def test_invalid_join_code_locks_ip_after_hundred_failures(self):
        # 7.1.2: 100 per IP per 15 minutes, because a classroom shares one public IP.
        visitor = Client(HTTPApplication(self.service, 'backend'))
        for _ in range(100):
            self.assertEqual(visitor.call('GET', '/api/v1/join-codes/AAAAA')[0], 404)
        self.assertEqual(visitor.call('GET', '/api/v1/join-codes/AAAAA')[0], 429)
        self.moment += timedelta(minutes=16)
        self.assertEqual(visitor.call('GET', '/api/v1/join-codes/AAAAA')[0], 404)

    def test_s3_equivalent_object_removed_on_sql_failure(self):
        s, _ = self.lecture()
        original = self.service.job
        def fail(*args, **kwargs):
            raise RuntimeError('DB failure')
        self.service.job = fail
        status, _, _ = self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents', upload=('rollback.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
        self.assertEqual(status, 503)
        self.assertEqual([p for p in Path(self.tmp.name).rglob('*') if p.is_file()], [])
        self.assertEqual(self.row('SELECT count(*) n FROM documents')['n'], 0)
        self.service.job = original

    def test_real_pdf_parser_extracts_text_and_rejects_scan_without_text(self):
        self.assertEqual(extract_pages('lecture.pdf', pdf()), ['Hash collision and vector search'])
        with self.assertRaises(APIError) as error:
            extract_pages('scan.pdf', pdf(''))
        self.assertEqual(error.exception.code, 'DOCUMENT_NO_TEXT')

    def test_pptx_expanded_size_guard(self):
        with self.assertRaises(APIError) as error:
            validate_document('test.pptx', PPTX, pptx(), replace(self.settings, expanded_limit=100))
        self.assertEqual(error.exception.status, 413)


if __name__ == '__main__':
    unittest.main()
