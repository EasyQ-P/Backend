from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet

from easyq_common.storage import LocalStorage
from easyq_common.config import Settings
from easyq_common.contracts import APIError, Request, dt, dumps, obj, uid
from easyq_common.http import HTTPApplication
from easyq_common.contracts import PDF, PPTX
from easyq_backend.documents import validate_document
from easyq_worker.retrieval import check_vectors, chunks, extract_pages, overlap_ratio
from easyq_common.security import decrypt, digest, password_matches
from easyq_worker.worker import Worker
from .support import Client, Service, SQLiteDatabase, pdf, pptx


def whitespace_spans(text):
    """Offline stand-in for the bge-reranker token counter used by retrieval.chunk_slides."""
    import re
    return [(m.start(), m.end()) for m in re.finditer(r'\S+', text)]


class Tokenizer:
    """Kiwi stand-in: whitespace tokens with positions; ASCII words are SL, a leading capital Korean-free
    word is still SL, '#'-prefixed words act as proper nouns (NNP), everything else NNG."""
    def tokenize(self, value):
        import re
        from types import SimpleNamespace
        result = []
        for m in re.finditer(r'\S+', value):
            form = m.group()
            tag = 'NNP' if form.startswith('#') else ('SL' if form.isascii() and form[0].isalpha() else 'NNG')
            form = form.lstrip('#')
            result.append(SimpleNamespace(form=form, tag=tag, start=m.start() + (m.group() != form), len=len(form)))
        return result


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


class FakeSocket:
    """In-memory WebSocket: plays a script of client messages, then disconnects."""
    def __init__(self, script):
        self.script, self.sent, self.closed, self.accepted = list(script), [], None, False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        import asyncio
        await asyncio.sleep(0)
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                item()            # e.g. a REST call made while the socket is open
            elif 'sleep' in item:
                await asyncio.sleep(item['sleep'])
            else:
                return item
        await asyncio.sleep(0.05)
        return None

    async def send_text(self, value):
        import json
        self.sent.append(json.loads(value))

    async def close(self, code):
        self.closed = code


class FakeSTT:
    """STT stand-in: one final utterance per second of audio (10 frames)."""
    def __init__(self):
        self.frames, self.terms = 0, None

    def open(self, terms, on_final):
        self.terms = terms
        return FakeSTTStream(self, on_final)


class FakeSTTStream:
    def __init__(self, owner, on_final):
        self.owner, self.on_final, self.start, self.end, self.count = owner, on_final, None, None, 0

    async def send(self, pcm, start_ms):
        self.owner.frames += 1
        self.count += 1
        if self.start is None:
            self.start = start_ms
        self.end = start_ms + len(pcm) // 32
        if self.count % 10 == 0:
            await self.on_final(self.start, self.end, f'발화 {self.count // 10}')
            self.start = None

    async def close(self):
        if self.start is not None:
            await self.on_final(self.start, self.end, '마지막')
            self.start = None


class FailingSTT(FakeSTT):
    """STT whose provider is down (send fails) or whose close fails."""
    def __init__(self, fail_send=False, fail_close=False):
        super().__init__()
        self.fail_send, self.fail_close = fail_send, fail_close

    def open(self, terms, on_final):
        stream = super().open(terms, on_final)
        owner = self
        send, close = stream.send, stream.close

        async def failing_send(pcm, start_ms):
            if owner.fail_send:
                owner.frames += 1
                raise RuntimeError('provider down')
            await send(pcm, start_ms)

        async def failing_close():
            if owner.fail_close:
                raise RuntimeError('provider close failed')
            await close()
        stream.send, stream.close = failing_send, failing_close
        return stream


def frame(stream_id, sequence):
    """7.3.10 binary frame: stream UUID + sequence + start_ms, then 100 ms of 16 kHz mono PCM16 silence."""
    from uuid import UUID
    from easyq_backend.live import FRAME_HEADER
    return FRAME_HEADER.pack(UUID(stream_id).bytes, sequence, sequence * 100) + bytes(2) * 1600


class APIBase(unittest.TestCase):
    """Fixtures shared by every HTTP suite (SQLite here; tests/test_postgres.PostgresMixin swaps in PostgreSQL)."""

    def setUp(self):
        from easyq_backend.live import LiveConnection
        LiveConnection.streams.clear()
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
        self.worker.token_spans = whitespace_spans
        # retrieval.chunk_slides counts Korean content words with Kiwi; use the same offline stand-in.
        from unittest.mock import patch
        kiwi = patch('retrieval.chunking._kiwi', return_value=Tokenizer())
        kiwi.start()
        self.addCleanup(kiwi.stop)
        keyword_kiwi = patch('retrieval.keyword._KIWI', Tokenizer())   # rc-v1 tokenize_keywords (6.1.5)
        keyword_kiwi.start()
        self.addCleanup(keyword_kiwi.stop)

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


class APITests(APIBase):
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

    def test_signup_rejects_special_use_domains_the_mailer_cannot_send_to(self):
        # fastapi-mail (email-validator) refuses .local/.test/.localhost, so API-01 refuses them up front.
        body = {'password': 'easyq2026', 'display_name': 'A', 'consents': {'SERVICE': {'policy_version': 'v1', 'granted': True}}}
        for address in ['someone@easyq.local', 'someone@easyq.test', 'someone@localhost.localhost']:
            status, payload, _ = self.auth.call('POST', '/api/v1/users', {**body, 'email': address})
            self.assertEqual((status, payload['error']['code']), (422, 'VALIDATION_ERROR'), address)
        self.assertEqual(self.row('SELECT count(*) n FROM users')['n'], 0)

    def test_repeated_signup_within_sixty_seconds_reports_existing_email(self):
        # Order: IP limit -> duplicate check -> per-address limit, so the duplicate wins over the 60-second gap.
        self.register('again@example.com')
        status, payload, _ = self.auth.call('POST', '/api/v1/users', {'email': 'again@example.com', 'password': 'easyq2026', 'display_name': 'A',
            'consents': {'SERVICE': {'policy_version': 'v1', 'granted': True}}})
        self.assertEqual((status, payload['error']['code']), (409, 'EMAIL_ALREADY_EXISTS'))

    def test_failed_mail_job_is_logged_without_address_or_exception_text(self):
        import io
        from contextlib import redirect_stderr
        self.register('logged@example.com')
        def refuse(*message):
            raise ValueError('logged@example.com is not deliverable')
        self.mailer.send = refuse
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.worker.run_one()
        lines = [obj(line) for line in stderr.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(len(lines), 1, stderr.getvalue())
        line = lines[0]
        self.assertEqual((line['service'], line['event'], line['level']), ('worker', 'email.verification_failed', 'WARNING'))
        self.assertEqual((line['meta']['job_type'], line['meta']['exception'], line['meta']['final']), ('EMAIL_VERIFICATION', 'ValueError', False))
        self.assertTrue(line['code'])
        self.assertNotIn('logged@example.com', stderr.getvalue())

    def test_signup_without_service_consent_is_403(self):
        # 8.4.2: CONSENT_REQUIRED is 403 (API-01 aligned; the frontend branches on error.code).
        status, payload, _ = self.auth.call('POST', '/api/v1/users', {'email': 'c@example.com', 'password': 'easyq2026', 'display_name': 'C',
            'consents': {'SERVICE': {'policy_version': 'v1', 'granted': False}}})
        self.assertEqual((status, payload['error']['code']), (403, 'CONSENT_REQUIRED'))

    def test_keyword_index_uses_rc_v1_tokens(self):
        from easyq_worker.retrieval import keywords
        # Fake Kiwi: ASCII words are SL (lower-cased by tokenize_keywords), other words NNG.
        self.assertEqual(keywords('Hash 충돌은 처리'), 'hash 충돌은 처리')

    def test_judge_model_keepalive_only_while_lectures_need_it(self):
        # 6.4.14 lease: called while a lecture is LIVE or a DRAFT lecture has audience; never logs the API key.
        import io
        from contextlib import redirect_stderr
        from dataclasses import replace as dc_replace
        calls = []

        class Trainer:
            fail = False
            def keepalive(self):
                calls.append(1)
                if self.fail:
                    raise OSError('tunnel down for dev-only-trainer-key')
                return {'loaded': False, 'loading': True}

        self.worker.settings = dc_replace(self.worker.settings, training_api_base_url='http://trainer:9000/api/v1', training_api_key='dev-only-trainer-key')
        self.worker.trainer = Trainer()
        self.assertFalse(self.worker.keep_judge_model_warm())
        s, etag = self.lecture()
        self.assertFalse(self.worker.keep_judge_model_warm())          # DRAFT without audience
        visitor = Client(HTTPApplication(self.service, 'backend'))
        visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': True})
        self.assertTrue(self.worker.keep_judge_model_warm())           # audience waiting: preload
        self.api.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertTrue(self.worker.keep_judge_model_warm())
        self.worker.trainer.fail = True
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(self.worker.keep_judge_model_warm())
        self.assertIn('judge_model.keepalive_failed', stderr.getvalue())
        self.assertNotIn('dev-only-trainer-key', stderr.getvalue())
        self.assertEqual(len(calls), 3)

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
        # v1.4 grants: auth cannot UPDATE jobs; the old mail job stays and the worker skips the revoked code
        # (tests/test_account.py checks the worker side).
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='EMAIL_VERIFICATION'")['n'], 2)

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
        # slides-v4: page 2 ('벡터 검색과 키워드 검색', 13 chars) is under slide_min_chars=20, so it yields no chunk.
        self.assertEqual(self.row('SELECT count(*) n FROM knowledge_chunks')['n'], 1)
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
        # retrieval.chunk_slides (slides-v4): a page over 400 tokens splits; a low-content page yields no chunk.
        long_page = [' '.join(f'hash{i} collision vector search' for i in range(150))]
        result = chunks([long_page, ['p. 2']], whitespace_spans)
        self.assertEqual([x['page_no'] for x in result], [1, 1])
        self.assertEqual([x['ordinal'] for x in result], [0, 1])
        with self.assertRaises(APIError):
            check_vectors([[float('nan')] * 768], 1)
        with self.assertRaises(APIError):
            check_vectors([[1.0] * 767], 1)
        self.assertEqual(extract_pages('test.pptx', pptx(('First', 'Second'))), [['First'], ['Second']])
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
        self.assertEqual(self.row('SELECT count(*) n FROM api_usage')['n'], 0)
        with self.db.transaction() as tx:
            tx.execute("UPDATE app_settings SET hard_cap_enabled=false WHERE setting_key='MONTHLY_API_BUDGET'")
        self.worker.maintenance()   # also enqueues the daily PURGE/CLEANUP and the hourly SYNC
        while self.worker.run_one():
            pass
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
        self.assertEqual(row['error_code'], 'INDEX_BUILD_FAILED')
        self.assertTrue(self.api.call('GET', '/api/v1/sessions/' + s['session_id'] + '/documents')[1]['data']['items'][0]['retryable'])

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

    def test_invalid_join_code_locks_ip_after_twenty_failures(self):
        visitor = Client(HTTPApplication(self.service, 'backend'))
        for _ in range(20):
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
        self.assertEqual([[b.strip() for b in page] for page in extract_pages('lecture.pdf', pdf())], [['Hash collision and vector search']])
        with self.assertRaises(APIError) as error:
            extract_pages('scan.pdf', pdf(''))
        self.assertEqual(error.exception.code, 'DOCUMENT_NO_TEXT')

    def test_pptx_expanded_size_guard(self):
        with self.assertRaises(APIError) as error:
            validate_document('test.pptx', PPTX, pptx(), replace(self.settings, expanded_limit=100))
        self.assertEqual(error.exception.status, 413)

    # ---------------------------------------------------------------- v0.3 ----
    def audience(self, s, address='audience@example.com'):
        """Log a second member in on a fresh client and join lecture s."""
        self.moment += timedelta(seconds=61)
        auth, api = Client(HTTPApplication(self.service, 'auth')), Client(HTTPApplication(self.service, 'backend'))
        saved = self.auth
        self.auth = auth
        self.register(address)
        row = self.row('SELECT * FROM email_verifications v JOIN users u ON u.user_id=v.user_id WHERE u.email=%s', (address,))
        code = decrypt(self.settings.encryption_key, row['delivery_ciphertext'])
        auth.call('POST', '/api/v1/auth/email-verifications/confirm', {'email': address, 'code': code})
        auth.call('POST', '/api/v1/auth/sessions', {'email': address, 'password': 'easyq2026'})
        self.auth = saved
        api.cookies, api.csrf = auth.cookies.copy(), auth.csrf
        status, payload, _ = api.call('POST', '/api/v1/participations', {'join_code': s['join_code']})
        self.assertEqual(status, 201, payload)
        return api

    def test_session_object_shows_code_and_terms_only_to_host(self):
        s, _ = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        host = self.api.call('GET', path)[1]['data']
        self.assertEqual((host['join_code'], host['terms']), (s['join_code'], []))
        member = self.audience(s)
        seen = member.call('GET', path)[1]['data']
        self.assertEqual(seen['my_role'], 'AUDIENCE')
        self.assertNotIn('join_code', seen)
        self.assertNotIn('terms', seen)

    def test_terms_dictionary_is_normalized_draft_only_and_remembers_removed(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id'] + '/terms'
        status, payload, headers = self.api.call('PUT', path, {'terms': [' 해시  함수 ', 'Chaining', 'chaining', 'B-tree']}, headers={'if-match': etag})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['data']['terms'], ['해시 함수', 'Chaining', 'B-tree'])
        self.assertEqual(self.api.call('PUT', path, {'terms': ['x']}, headers={'if-match': etag})[0], 409)
        self.assertEqual(self.api.call('PUT', path, {'terms': ['a' * 41]}, headers={'if-match': headers['etag']})[0], 422)
        self.assertEqual(self.api.call('PUT', path, {'terms': [f't{i}' for i in range(101)]}, headers={'if-match': headers['etag']})[0], 422)
        status, payload, headers = self.api.call('PUT', path, {'terms': ['해시 함수']}, headers={'if-match': headers['etag']})
        self.assertEqual(status, 200)
        stored = self.row('SELECT terms,removed_terms FROM lecture_sessions WHERE session_id=%s', (s['session_id'],))
        self.assertEqual(sorted(obj(stored['removed_terms'])), ['B-tree', 'Chaining'])
        # Extraction never re-adds a removed term; the dictionary freezes once LIVE.
        self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents',
                      upload=('t.pptx', PPTX, pptx(('chaining chaining 충돌 해결', 'chaining 과 #개방주소법'),)), headers={'idempotency-key': uid()})
        self.drain()
        terms = obj(self.row('SELECT terms FROM lecture_sessions WHERE session_id=%s', (s['session_id'],))['terms'])
        self.assertIn('개방주소법', terms)
        self.assertNotIn('chaining', [t.lower() for t in terms])
        version = self.api.call('GET', '/api/v1/sessions/' + s['session_id'])[2]['etag']
        _, _, h = self.api.call('PATCH', '/api/v1/sessions/' + s['session_id'], {'status': 'LIVE'}, headers={'if-match': version})
        self.assertEqual(self.api.call('PUT', path, {'terms': []}, headers={'if-match': h['etag']})[0], 409)

    def test_ingest_runs_terms_stage_and_fills_only_free_slots(self):
        s, etag = self.lecture()
        full = [f'용어{i}' for i in range(99)]
        self.api.call('PUT', '/api/v1/sessions/' + s['session_id'] + '/terms', {'terms': full}, headers={'if-match': etag})
        self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents',
                      upload=('t.pptx', PPTX, pptx(('#RAM #CPU 해시 함수 해시 함수', 'linked list linked list'))), headers={'idempotency-key': uid()})
        self.drain()
        document = self.row('SELECT * FROM documents')
        self.assertEqual(document['status'], 'READY')
        self.assertTrue(len(obj(document['terms'])) >= 3)
        stages = [obj(r['payload']).get('stage') for r in self.db_rows("SELECT payload FROM ws_events WHERE event_type='document.updated' ORDER BY event_id")]
        self.assertEqual([x for x in stages if x], ['UPLOADED', 'EXTRACTING', 'INDEXING', 'TERMS'])
        terms = obj(self.row('SELECT terms FROM lecture_sessions WHERE session_id=%s', (s['session_id'],))['terms'])
        self.assertEqual(len(terms), 100)   # one free slot filled, never more

    def drain(self):
        for _ in range(50):
            if not self.worker.run_one():
                return

    def db_rows(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.all(sql, args)

    def test_failed_document_retry_reindexes_from_original(self):
        s, _ = self.lecture()
        class Broken:
            def embed(self, values):
                raise RuntimeError('down')
        good, self.worker.embedder = self.worker.embedder, Broken()
        base = '/api/v1/sessions/' + s['session_id'] + '/documents'
        _, accepted, _ = self.api.call('POST', base, upload=('r.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
        did = accepted['data']['resource_id']
        for _ in range(8):
            self.worker.run_one()
            self.moment += timedelta(minutes=3)
        self.assertEqual(self.row('SELECT status,error_code FROM documents')['error_code'], 'INDEX_BUILD_FAILED')
        self.worker.embedder = good
        key = uid()
        status, payload, headers = self.api.call('POST', f'{base}/{did}/retries', {}, headers={'idempotency-key': key})
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.api.call('POST', f'{base}/{did}/retries', {}, headers={'idempotency-key': key})[1]['data']['job_id'], payload['data']['job_id'])
        self.assertEqual(self.row('SELECT status FROM documents')['status'], 'PENDING')
        self.drain()
        self.assertEqual(self.row('SELECT status FROM documents')['status'], 'READY')
        status, payload, _ = self.api.call('POST', f'{base}/{did}/retries', {}, headers={'idempotency-key': uid()})
        self.assertEqual((status, payload['error']['code']), (409, 'DOCUMENT_NOT_RETRYABLE'))
        self.assertEqual(self.api.call('POST', f'{base}/{uid()}/retries', {}, headers={'idempotency-key': uid()})[0], 404)

    def test_document_broken_by_original_is_not_retryable(self):
        s, _ = self.lecture()
        base = '/api/v1/sessions/' + s['session_id'] + '/documents'
        _, accepted, _ = self.api.call('POST', base, upload=('scan.pdf', PDF, pdf('')), headers={'idempotency-key': uid()})
        self.drain()
        item = self.api.call('GET', base)[1]['data']['items'][0]
        self.assertEqual((item['status'], item['error_code'], item['retryable']), ('FAILED', 'DOCUMENT_PARSE_FAILED', False))
        status, payload, _ = self.api.call('POST', f"{base}/{accepted['data']['resource_id']}/retries", {}, headers={'idempotency-key': uid()})
        self.assertEqual((status, payload['error']['code']), (409, 'DOCUMENT_NOT_RETRYABLE'))
        job = self.api.call('GET', '/api/v1/jobs/' + accepted['data']['job_id'])[1]['data']
        self.assertEqual((job['status'], job['error_code']), ('FAILED', 'DOCUMENT_PARSE_FAILED'))

    def test_my_lecture_lists_hosted_and_joined_with_cursor(self):
        first, _ = self.lecture()
        self.moment += timedelta(hours=1)
        second = self.api.call('POST', '/api/v1/sessions', {'title': '운영체제', 'scheduled_at': self.moment.isoformat()})[1]['data']
        status, page, _ = self.api.call('GET', '/api/v1/sessions?scope=hosted&limit=1')
        self.assertEqual(status, 200, page)
        self.assertEqual((page['data']['total_count'], [i['session_id'] for i in page['data']['items']]), (2, [second['session_id']]))
        nxt = self.api.call('GET', '/api/v1/sessions?scope=hosted&limit=1&cursor=' + page['data']['next_cursor'])[1]['data']
        self.assertEqual(([i['session_id'] for i in nxt['items']], nxt['next_cursor']), ([first['session_id']], None))
        self.assertEqual(self.api.call('GET', '/api/v1/sessions?scope=hosted&status=LIVE')[1]['data']['items'], [])
        self.assertEqual(self.api.call('GET', '/api/v1/sessions?scope=other')[0], 422)
        member = self.audience(first)
        joined = member.call('GET', '/api/v1/sessions?scope=joined')[1]['data']
        self.assertEqual([(i['session_id'], i['my_role'], i['my_question_count']) for i in joined['items']], [(first['session_id'], 'AUDIENCE', 0)])
        from urllib.parse import quote
        self.assertEqual(member.call('GET', '/api/v1/sessions?scope=joined&q=' + quote('자료'))[1]['data']['total_count'], 1)
        self.assertEqual(member.call('GET', '/api/v1/sessions?scope=joined&q=' + quote('없는%'))[1]['data']['total_count'], 0)
        self.assertEqual(member.call('GET', '/api/v1/sessions?scope=hosted')[1]['data']['total_count'], 0)

    def test_admin_is_created_only_by_server_command_and_logs_in_normally(self):
        from easyq_auth.admin import create_admin
        user = create_admin(self.db, 'Admin@Example.com', '관리자', 'admin2026', clock=lambda: self.moment)
        with self.assertRaises(APIError) as error:
            create_admin(self.db, 'admin@example.com', '관리자', 'admin2026', clock=lambda: self.moment)
        self.assertEqual(error.exception.code, 'EMAIL_ALREADY_EXISTS')
        status, payload, _ = self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'admin@example.com', 'password': 'admin2026'})
        self.assertEqual((status, payload['data']['user']['role']), (201, 'ADMIN'))
        with self.db.transaction() as tx:
            p = tx.one('SELECT * FROM auth_session_view WHERE user_id=%s', (user,))
            self.assertEqual(self.service.require_admin(p), user)
        self.login()
        with self.db.transaction() as tx:
            p = tx.one("SELECT v.* FROM auth_session_view v JOIN users u ON u.user_id=v.user_id WHERE u.role='MEMBER'")
            with self.assertRaises(APIError):
                self.service.require_admin(p)

    # ------------------------------------------------------- WebSocket ----
    def live_lecture(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        _, _, h = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.api.call('POST', path + '/consents', {'purpose': 'STT', 'policy_version': 'stt-v1', 'granted': True})
        status, payload, h = self.api.call('PATCH', path, {'stt_status': 'RUNNING'}, headers={'if-match': self.api.call('GET', path)[2]['etag']})
        self.assertEqual(payload['data']['stt_status'], 'CONNECTING')
        return s

    def run_socket(self, client, sid, script, stt=None, origin=None):
        import asyncio
        from easyq_backend.live import EventHub, LiveConnection
        socket = FakeSocket(script)
        hub = EventHub(self.service)
        headers = {'origin': origin or self.settings.origin}
        connection = LiveConnection(self.service, hub, stt or FakeSTT(), socket, sid, headers, dict(client.cookies))
        asyncio.run(connection.run())
        return socket

    def test_websocket_requires_origin_and_lecture_permission(self):
        s = self.live_lecture()
        self.assertEqual(self.run_socket(self.api, s['session_id'], [], origin='https://evil.test').closed, 1008)
        outsider = Client(HTTPApplication(self.service, 'backend'))
        self.assertEqual(self.run_socket(outsider, s['session_id'], []).closed, 1008)
        socket = self.run_socket(self.api, s['session_id'], [{'text': dumps({'type': 'ping'})}])
        self.assertEqual([m['type'] for m in socket.sent][:2], ['connection.ready', 'pong'])
        self.assertEqual(socket.sent[0]['data']['role'], 'HOST')

    def test_audience_cannot_stream_audio(self):
        s = self.live_lecture()
        member = self.audience(s)
        socket = self.run_socket(member, s['session_id'], [{'text': dumps({'type': 'audio.start', 'data': {'stream_id': uid(), 'sample_rate': 16000, 'channels': 1}})}])
        self.assertEqual([m['data'].get('code') for m in socket.sent if m['type'] == 'error'], ['FORBIDDEN'])

    def test_host_audio_is_relayed_to_stt_and_indexed_as_30_second_chunks(self):
        s = self.live_lecture()
        sid = s['session_id']
        self.api.call('GET', '/api/v1/sessions/' + sid)
        stream = uid()
        script = [{'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream, 'sample_rate': 16000, 'channels': 1}})}]
        script += [{'bytes': frame(stream, n)} for n in range(350)]                  # 35 s of 100 ms frames
        script += [{'bytes': frame(stream, 5)}]                                       # duplicate frame is ignored
        script += [{'text': dumps({'type': 'audio.stop', 'data': {'stream_id': stream}})}]
        stt = FakeSTT()
        socket = self.run_socket(self.api, sid, script, stt)
        kinds = [m['type'] for m in socket.sent]
        self.assertIn('audio.accepted', kinds)
        self.assertEqual(stt.frames, 350)
        self.assertEqual(stt.terms, [])
        self.assertEqual(sum(k == 'transcript.final' for k in kinds), 35)
        chunks = self.db_rows("SELECT * FROM knowledge_chunks WHERE source_type='TRANSCRIPT' ORDER BY start_ms")
        self.assertEqual([(c['start_ms'], c['end_ms'], c['index_status']) for c in chunks], [(0, 30000, 'PENDING'), (30000, 35000, 'PENDING')])
        self.assertTrue(chunks[0]['content'].startswith('발화 1 발화 2'))
        self.assertEqual(self.row("SELECT count(*) n FROM jobs WHERE job_type='TRANSCRIPT_INDEX'")['n'], 2)
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'PAUSED')
        # Audio never reaches storage: no event or row carries PCM, only text.
        for row in self.db_rows('SELECT payload FROM ws_events'):
            self.assertNotIn('\\u0000', str(row['payload']))
        # Transcript indexing outranks document processing and makes the chunk searchable.
        self.api.call('POST', '/api/v1/sessions/' + sid + '/documents', upload=('later.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
        self.worker.run_one()
        self.assertEqual(self.row("SELECT count(*) n FROM knowledge_chunks WHERE source_type='TRANSCRIPT' AND index_status='READY'")['n'], 1)
        self.assertEqual(self.row('SELECT status FROM documents')['status'], 'PENDING')
        self.worker.run_one()
        self.assertEqual(self.row("SELECT count(*) n FROM knowledge_chunks WHERE source_type='TRANSCRIPT' AND index_status='READY'")['n'], 2)
        status = [obj(r['payload']) for r in self.db_rows("SELECT payload FROM ws_events WHERE event_type='stt.status'")]
        # Both chunks share created_at in the test clock, so their indexing order is not fixed.
        self.assertEqual(sorted(x['indexed_until_ms'] for x in status), [30000, 35000])

    def test_audio_needs_live_stt_consent_and_one_stream(self):
        s, etag = self.lecture()
        start = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': uid(), 'sample_rate': 16000, 'channels': 1}})}
        socket = self.run_socket(self.api, s['session_id'], [start])
        self.assertEqual([m['data']['code'] for m in socket.sent if m['type'] == 'error'], ['SESSION_NOT_LIVE'])
        path = '/api/v1/sessions/' + s['session_id']
        self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        socket = self.run_socket(self.api, s['session_id'], [start])
        self.assertEqual([m['data']['code'] for m in socket.sent if m['type'] == 'error'], ['CONSENT_REQUIRED'])
        bad = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': uid(), 'sample_rate': 44100, 'channels': 1}})}
        socket = self.run_socket(self.api, s['session_id'], [bad])
        self.assertEqual([m['data']['code'] for m in socket.sent if m['type'] == 'error'], ['VALIDATION_ERROR'])

    def test_resumed_stream_skips_already_received_frames(self):
        s = self.live_lecture()
        stream = uid()
        start = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream, 'sample_rate': 16000, 'channels': 1}})}
        self.run_socket(self.api, s['session_id'], [start] + [{'bytes': frame(stream, n)} for n in range(20)])
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'DEGRADED')
        stt = FakeSTT()
        socket = self.run_socket(self.api, s['session_id'], [start] + [{'bytes': frame(stream, n)} for n in range(15, 25)], stt)
        accepted = [m for m in socket.sent if m['type'] == 'audio.accepted'][0]
        self.assertEqual(accepted['data']['next_sequence'], 20)
        self.assertEqual(stt.frames, 5)
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'DEGRADED')

    def test_oversized_frame_closes_with_1009(self):
        s = self.live_lecture()
        stream = uid()
        start = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream, 'sample_rate': 16000, 'channels': 1}})}
        socket = self.run_socket(self.api, s['session_id'], [start, {'bytes': b'\x00' * (32 * 1024 + 1)}])
        self.assertEqual(socket.closed, 1009)

    def test_event_hub_delivers_only_what_each_connection_may_see(self):
        import asyncio
        from easyq_backend.live import EventHub, LiveConnection
        s, _ = self.lecture()
        member = self.audience(s)
        hub = EventHub(self.service)
        host = LiveConnection(self.service, hub, FakeSTT(), FakeSocket([]), s['session_id'], {'origin': self.settings.origin}, dict(self.api.cookies))
        guest = LiveConnection(self.service, hub, FakeSTT(), FakeSocket([]), s['session_id'], {'origin': self.settings.origin}, dict(member.cookies))

        async def scenario():
            self.assertTrue(await host.authorize())
            self.assertTrue(await guest.authorize())
            await hub.poll_once()   # first poll only records the current end of the event log
            hub.subscribe(host)
            hub.subscribe(guest)
            self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents', upload=('d.pptx', PPTX, pptx()), headers={'idempotency-key': uid()})
            with self.db.transaction() as tx:
                self.service.event(tx, 'session.updated', {'participant_count': 1}, s['session_id'])
                self.service.event(tx, 'auth.csrf_rotated', {'auth_session_id': str(host.principal['auth_session_id'])})
            await hub.poll_once()
            def drain(c):
                items = []
                while not c.outbox.empty():
                    items.append(c.outbox.get_nowait()['type'])
                return items
            return drain(host), drain(guest)
        host_events, guest_events = asyncio.run(scenario())
        self.assertEqual(host_events, ['document.updated', 'session.updated', 'auth.csrf_rotated'])
        self.assertEqual(guest_events, ['session.updated'])

    # ------------------------------------------------- review fixes ----
    def test_event_hub_delivers_events_committed_out_of_id_order(self):
        import asyncio
        from easyq_backend.live import EventHub
        s, _ = self.lecture()
        hub = EventHub(self.service)
        seen = []

        class Sink:
            def offer(self, row):
                seen.append(row['event_id'])

        def insert(event_id):
            with self.db.transaction() as tx:
                tx.execute("INSERT INTO ws_events(event_id,event_type,payload,session_id,expires_at) VALUES(%s,'session.updated','{}',%s,%s)",
                           (event_id, s['session_id'], self.moment + timedelta(hours=1)))

        async def scenario():
            await hub.poll_once()
            hub.subscribe(Sink())
            top = hub.last_event_id
            insert(top + 10)          # committed first, higher id
            await hub.poll_once()
            insert(top + 5)           # lower id that committed later
            await hub.poll_once()
            await hub.poll_once()     # never delivered twice
            return top
        top = asyncio.run(scenario())
        self.assertEqual(seen, [top + 10, top + 5])

    def test_stt_consent_withdrawal_stops_stream_and_discards_speech(self):
        from unittest.mock import patch
        s = self.live_lecture()
        sid, stream = s['session_id'], uid()
        start = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream, 'sample_rate': 16000, 'channels': 1}})}

        def withdraw():
            self.moment += timedelta(seconds=1)   # consents are ordered by recorded_at
            self.api.call('POST', '/api/v1/sessions/' + sid + '/consents', {'purpose': 'STT', 'policy_version': 'stt-v1', 'granted': False})
        script = [start] + [{'bytes': frame(stream, n)} for n in range(15)] + [withdraw, {'sleep': 0.3}]
        script += [{'bytes': frame(stream, n)} for n in range(15, 20)]
        stt = FakeSTT()
        with patch('easyq_backend.live.protocol.STATE_SECONDS', 0.05):
            socket = self.run_socket(self.api, sid, script, stt)
        self.assertEqual(stt.frames, 15)                      # nothing relayed after the withdrawal
        self.assertEqual(self.row("SELECT count(*) n FROM knowledge_chunks WHERE source_type='TRANSCRIPT'")['n'], 0)
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'STOPPED')
        self.assertIn('FORBIDDEN', [m['data'].get('code') for m in socket.sent if m['type'] == 'error'])

    def test_two_host_tabs_cannot_both_start_audio(self):
        import asyncio
        from easyq_backend.live import EventHub, LiveConnection
        s = self.live_lecture()
        hub = EventHub(self.service)
        tabs = [LiveConnection(self.service, hub, FakeSTT(), FakeSocket([]), s['session_id'], {'origin': self.settings.origin},
                               dict(self.api.cookies)) for _ in range(2)]

        async def scenario():
            for tab in tabs:
                await tab.authorize()
            await asyncio.gather(*(tab.on_audio_start({'stream_id': uid(), 'sample_rate': 16000, 'channels': 1}) for tab in tabs))
            started = [tab for tab in tabs if tab.audio]
            errors = [tab.outbox.get_nowait()['data']['code'] for tab in tabs if not tab.audio]
            for tab in started:
                await tab.stop_stream('PAUSED', ('RUNNING',))
            return len(started), errors
        self.assertEqual(asyncio.run(scenario()), (1, ['SESSION_STATE_CONFLICT']))

    def test_stream_cleanup_releases_lecture_even_when_stt_close_fails(self):
        from easyq_backend.live import LiveConnection
        s = self.live_lecture()
        sid, stream = s['session_id'], uid()

        def start(stream_id):
            return {'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream_id, 'sample_rate': 16000, 'channels': 1}})}
        script = [start(stream)] + [{'bytes': frame(stream, n)} for n in range(10)]
        script += [{'text': dumps({'type': 'audio.stop', 'data': {'stream_id': stream.upper()}})}]
        self.run_socket(self.api, sid, script, FailingSTT(fail_close=True))
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'PAUSED')   # uppercase stream_id accepted
        self.assertIsNone(LiveConnection.streams[sid]['connection'])
        path = '/api/v1/sessions/' + sid
        self.api.call('PATCH', path, {'stt_status': 'RUNNING'}, headers={'if-match': self.api.call('GET', path)[2]['etag']})
        other = self.run_socket(self.api, sid, [start(uid())])
        self.assertIn('audio.accepted', [m['type'] for m in other.sent])

    def test_stt_provider_failure_marks_lecture_degraded_once(self):
        s = self.live_lecture()
        stream = uid()
        start = {'text': dumps({'type': 'audio.start', 'data': {'stream_id': stream, 'sample_rate': 16000, 'channels': 1}})}
        script = [start] + [{'bytes': frame(stream, n)} for n in range(30)]
        socket = self.run_socket(self.api, s['session_id'], script, FailingSTT(fail_send=True))
        self.assertEqual(self.row('SELECT stt_status FROM lecture_sessions')['stt_status'], 'DEGRADED')
        degraded = [m for m in socket.sent if m['type'] == 'stt.status' and m['data']['status'] == 'DEGRADED']
        self.assertEqual(len(degraded), 1)

    def test_failed_ingest_never_revives_a_deleting_document(self):
        s, _ = self.lecture()
        self.api.call('POST', '/api/v1/sessions/' + s['session_id'] + '/documents', upload=('d.pptx', PPTX, pptx()),
                      headers={'idempotency-key': uid()})
        with self.db.transaction() as tx:
            tx.execute("UPDATE documents SET status='DELETING'")
        self.drain()
        self.assertEqual(self.row('SELECT status FROM documents')['status'], 'DELETING')
    # ---------------------------------------------- frontend integration ----
    def test_participation_carries_nickname_and_session_status(self):
        s, _ = self.lecture()
        visitor = Client(HTTPApplication(self.service, 'backend'))
        data = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중1', 'service_consent': True})[1]['data']
        self.assertEqual((data['kind'], data['nickname'], data['session_status']), ('GUEST', '청중1', 'DRAFT'))
        member = self.audience(s)
        again = member.call('POST', '/api/v1/participations', {'join_code': s['join_code']})[1]['data']
        self.assertEqual((again['kind'], again['nickname']), ('MEMBER', '조현준'))

    @unittest.skipUnless(__import__('importlib').util.find_spec('pydantic'), 'pydantic not installed')
    def test_swagger_models_match_real_auth_and_entry_responses(self):
        from easyq_common import schemas
        self.assertTrue({'User', 'AuthData', 'JoinPreview', 'Participation', 'ErrorResponse'} <= set(schemas.components()))
        body = {'email': 'swagger@example.com', 'password': 'easyq2026', 'display_name': '문서',
                'consents': {'SERVICE': {'policy_version': 'service-v1', 'granted': True}}}
        schemas.SignupRequest.model_validate(body)
        status, payload, _ = self.auth.call('POST', '/api/v1/users', body)
        schemas.SignupData.model_validate(payload['data'])
        schemas.ErrorResponse.model_validate(self.auth.call('POST', '/api/v1/users', body)[1])
        s, _ = self.lecture()
        login = self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})[1]
        schemas.AuthData.model_validate(login['data'])
        schemas.User.model_validate(self.auth.call('GET', '/api/v1/me')[1]['data'])
        visitor = Client(HTTPApplication(self.service, 'backend'))
        schemas.JoinPreview.model_validate(visitor.call('GET', '/api/v1/join-codes/' + s['join_code'])[1]['data'])
        request = {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': True}
        schemas.ParticipationRequest.model_validate(request)
        schemas.Participation.model_validate(visitor.call('POST', '/api/v1/participations', request)[1]['data'])
        schemas.CsrfData.model_validate(self.auth.call('POST', '/api/v1/auth/csrf-tokens', {}, headers={'x-easyq-client': 'web'})[1]['data'])

    def test_dev_seed_creates_login_ready_accounts_only_for_localhost(self):
        from easyq_auth.devseed import check_development, create_member
        create_member(self.db, 'member@easyq.local', '테스트회원', 'easyq2026', clock=lambda: self.moment)
        status, payload, _ = self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@easyq.local', 'password': 'easyq2026'})
        self.assertEqual((status, payload['data']['user']['status'], payload['data']['user']['consents']['SERVICE']['granted']), (201, 'ACTIVE', True))
        with self.assertRaises(APIError):
            create_member(self.db, 'member@easyq.local', '테스트회원', 'easyq2026', clock=lambda: self.moment)
        check_development({'EASYQ_DEV_SEED': '1', 'PUBLIC_ORIGIN': 'http://localhost:3000'})
        for env in ({'PUBLIC_ORIGIN': 'http://localhost:3000'}, {'EASYQ_DEV_SEED': '1', 'PUBLIC_ORIGIN': 'https://easyq.example.com'}):
            with self.assertRaises(SystemExit):
                check_development(env)
    def test_csrf_failure_is_forbidden_with_reason_so_client_can_refresh_and_retry(self):
        self.login()
        body = {'title': '강연', 'scheduled_at': self.moment.isoformat()}
        good, self.api.csrf = self.api.csrf, 'stale-token'
        status, payload, _ = self.api.call('POST', '/api/v1/sessions', body)
        self.assertEqual((status, payload['error']['code'], payload['error']['details']), (403, 'FORBIDDEN', {'reason': 'CSRF'}))
        status, payload, _ = self.auth.call('DELETE', '/api/v1/auth/sessions/current', headers={'x-csrf-token': 'stale-token'})
        self.assertEqual(payload['error']['details'], {'reason': 'CSRF'})
        # Other FORBIDDEN causes carry no reason; refreshing (API-06) and retrying once succeeds.
        status, payload, _ = self.api.call('POST', '/api/v1/sessions', body, headers={'origin': 'https://evil.test'})
        self.assertEqual((status, payload['error']['details']), (403, {}))
        self.api.csrf = good
        self.assertEqual(self.api.call('POST', '/api/v1/sessions', body)[0], 201)

if __name__ == '__main__':
    unittest.main()
