"""v1.4 changes to the v0.3 entry APIs: 24-hour retry cookie, capacity rule, HOST SERVICE withdrawal, past-lecture tab."""
from datetime import timedelta
import unittest

from easyq_common.contracts import uid
from easyq_common.http import HTTPApplication

from tests.support import Client
from tests.test_api import APIBase

CONSENT = {'policy_version': 'service-v1', 'granted': True}


class EntryV14Tests(APIBase):
    def join_guest(self, code):
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, headers = visitor.call('POST', '/api/v1/participations', {'join_code': code, 'nickname': '청중', 'service_consent': CONSENT})
        self.assertEqual(status, 201, payload)
        return visitor, payload['data'], headers

    def test_retry_cookie_lasts_24_hours_and_finds_the_same_guest(self):
        s, _ = self.lecture()
        visitor, data, headers = self.join_guest(s['join_code'])
        self.assertIn('easyq_join_retry=', headers['set-cookie'])
        self.assertIn('Max-Age=86400', headers['set-cookie'])
        self.assertIn('Path=/api/v1/participations', headers['set-cookie'])
        visitor.cookies.pop('easyq_session')
        self.moment += timedelta(hours=23)
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': CONSENT})
        self.assertEqual((status, payload['data']['participant_id']), (200, data['participant_id']))
        # Expired or tampered markers start a new guest instead.
        self.moment += timedelta(hours=2)
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '청중', 'service_consent': CONSENT})
        self.assertEqual(status, 201)
        self.assertNotEqual(payload['data']['participant_id'], data['participant_id'])

    def test_capacity_skips_guests_whose_session_ended(self):
        s, _ = self.lecture()
        with self.db.transaction() as tx:
            for _ in range(50):   # guests who joined an hour ago and hold no live guest session
                tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname,archived,joined_at) VALUES(%s,%s,'GUEST','g',false,%s)",
                           (uid(), s['session_id'], self.moment - timedelta(hours=1)))
        self.join_guest(s['join_code'])
        with self.db.transaction() as tx:
            for _ in range(49):   # fresh joins (< 10 min) count even before their session exists
                tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname,archived) VALUES(%s,%s,'GUEST','g',false)",
                           (uid(), s['session_id']))
        visitor = Client(HTTPApplication(self.service, 'backend'))
        status, payload, _ = visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': '늦음', 'service_consent': CONSENT})
        self.assertEqual((status, payload['error']['code']), (409, 'SESSION_FULL'))

    def test_host_service_withdrawal_keeps_draft_and_blocks_start(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        status, payload, _ = self.api.call('POST', path + '/consents', {'purpose': 'SERVICE', 'policy_version': 'service-v1', 'granted': False})
        self.assertEqual(status, 201, payload)
        self.assertEqual(self.row('SELECT status FROM lecture_sessions WHERE session_id=%s', (s['session_id'],))['status'], 'DRAFT')
        etag = self.api.call('GET', path)[2]['etag']
        status, payload, _ = self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})
        self.assertEqual((status, payload['error']['code']), (403, 'CONSENT_REQUIRED'))

    def test_host_service_withdrawal_ends_live_lecture_without_if_match(self):
        s, etag = self.lecture()
        path = '/api/v1/sessions/' + s['session_id']
        self.assertEqual(self.api.call('PATCH', path, {'status': 'LIVE'}, headers={'if-match': etag})[0], 200)
        self.assertEqual(self.api.call('POST', path + '/consents', {'purpose': 'SERVICE', 'policy_version': 'service-v1', 'granted': False})[0], 201)
        row = self.row('SELECT status,end_reason FROM lecture_sessions WHERE session_id=%s', (s['session_id'],))
        self.assertEqual((row['status'], row['end_reason']), ('ENDED', 'HOST'))

    def test_hosted_past_tab_accepts_purged(self):
        s, _ = self.lecture()
        with self.db.transaction() as tx:
            tx.execute("UPDATE lecture_sessions SET status='PURGED' WHERE session_id=%s", (s['session_id'],))
        status, payload, _ = self.api.call('GET', '/api/v1/sessions?scope=hosted&status=PURGED')
        self.assertEqual(status, 200, payload)
        self.assertEqual([i['session_id'] for i in payload['data']['items']], [s['session_id']])
        with self.db.transaction() as tx:
            tx.execute("UPDATE lecture_sessions SET host_deleted_at=%s WHERE session_id=%s", (self.moment, s['session_id']))
        self.assertEqual(self.api.call('GET', '/api/v1/sessions?scope=hosted')[1]['data']['items'], [])


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGEntryV14Tests(PostgresMixin, EntryV14Tests):
    pass
