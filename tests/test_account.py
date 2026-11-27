"""API-10 display name / password change (auth)."""
import unittest

from easyq_common.http import HTTPApplication
from easyq_common.security import password_matches

from tests.support import Client
from tests.test_api import APIBase


class AccountTests(APIBase):
    def second_device(self):
        other = Client(HTTPApplication(self.service, 'auth'))
        status, _, _ = other.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2026'})
        self.assertEqual(status, 201)
        return other

    def test_display_name_change_returns_user(self):
        self.login()
        status, payload, headers = self.auth.call('PATCH', '/api/v1/me', {'display_name': '  새 이름 '})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload['data']['display_name'], payload['data']['role']), ('새 이름', 'MEMBER'))
        self.assertEqual(set(payload['data']), {'user_id', 'email', 'display_name', 'role', 'status', 'email_verified_at', 'consents'})
        self.assertNotIn('set-cookie', headers)
        self.assertEqual(self.auth.call('GET', '/api/v1/me')[1]['data']['display_name'], '새 이름')

    def test_validation_csrf_and_route(self):
        self.login()
        for body in ({}, {'display_name': ''}, {'display_name': 'x' * 51}, {'new_password': 'easyq2027'},
                     {'current_password': 'easyq2026'}, {'current_password': 'easyq2026', 'new_password': 'short'},
                     {'email': 'other@example.com'}):
            self.assertEqual(self.auth.call('PATCH', '/api/v1/me', body)[0], 422, body)
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'A'}, headers={'x-csrf-token': 'bad'})[0], 403)
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'A'}, headers={'origin': 'https://evil.test'})[0], 403)
        self.assertEqual(self.api.call('PATCH', '/api/v1/me', {'display_name': 'A'})[0], 404)   # 7.1.4: auth only
        self.auth.cookies = {}
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'A'})[0], 401)

    def test_password_change_revokes_all_sessions_and_issues_new_cookie(self):
        user = self.login()
        other = self.second_device()
        old_cookie = self.auth.cookies['easyq_session']
        status, payload, headers = self.auth.call('PATCH', '/api/v1/me', {'current_password': 'easyq2026', 'new_password': 'easyq2027',
                                                                          'display_name': '바뀜'})
        self.assertEqual(status, 200, payload)
        self.assertIn('HttpOnly', headers['set-cookie'])
        self.assertNotEqual(self.auth.cookies['easyq_session'], old_cookie)
        self.assertNotIn('csrf_token', payload['data'])
        self.assertEqual(self.row('SELECT count(*) n FROM auth_sessions WHERE user_id=%s AND revoked_at IS NULL', (user,))['n'], 1)
        self.assertEqual(other.call('GET', '/api/v1/me')[0], 401)
        revoked = self.row("SELECT count(*) n FROM ws_events WHERE event_type='session.revoked'")['n']
        self.assertEqual(revoked, 2)   # both earlier sessions: their WebSockets close
        # The client recovers its CSRF token with API-06, then mutates again.
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'A'})[0], 403)
        status, payload, _ = self.auth.call('POST', '/api/v1/auth/csrf-tokens', {}, headers={'x-easyq-client': 'web'})
        self.assertEqual(status, 200)
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'A'})[0], 200)
        stored = self.row('SELECT password_hash,display_name FROM users WHERE user_id=%s', (user,))
        self.assertTrue(password_matches('easyq2027', stored['password_hash']))
        self.assertEqual(self.auth.call('POST', '/api/v1/auth/sessions', {'email': 'member@example.com', 'password': 'easyq2027'})[0], 201)

    def test_wrong_current_password_is_limited_per_account(self):
        user = self.login()
        body = {'current_password': 'wrong-pass1', 'new_password': 'easyq2027'}
        for _ in range(10):
            status, payload, _ = self.auth.call('PATCH', '/api/v1/me', body)
            self.assertEqual((status, payload['error']['code']), (401, 'AUTH_INVALID_CREDENTIALS'))
        status, payload, headers = self.auth.call('PATCH', '/api/v1/me', {**body, 'current_password': 'easyq2026'})
        self.assertEqual((status, payload['error']['code']), (429, 'RATE_LIMITED'))
        self.assertGreater(int(headers['retry-after']), 0)
        self.assertTrue(password_matches('easyq2026', self.row('SELECT password_hash FROM users WHERE user_id=%s', (user,))['password_hash']))
        # The limit is per account: a display-name change needs no password check.
        self.assertEqual(self.auth.call('PATCH', '/api/v1/me', {'display_name': 'B'})[0], 200)


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGAccountTests(PostgresMixin, AccountTests):
    pass
