"""Notices and 1:1 inquiries (API-42~44 · 49~52)."""
from datetime import timedelta
import unittest

from easyq_common.contracts import uid
from easyq_common.http import HTTPApplication

from tests.support import Client
from tests.test_api import APIBase


class NoticesTests(APIBase):
    def admin(self):
        from easyq_auth.admin import create_admin
        create_admin(self.db, 'admin@example.com', '관리자', 'admin2026', clock=lambda: self.moment)
        auth, api = Client(HTTPApplication(self.service, 'auth')), Client(HTTPApplication(self.service, 'backend'))
        auth.call('POST', '/api/v1/auth/sessions', {'email': 'admin@example.com', 'password': 'admin2026'})
        api.cookies, api.csrf = auth.cookies.copy(), auth.csrf
        return api

    def test_notice_lifecycle_with_version_and_audit(self):
        admin = self.admin()
        self.login()
        self.assertEqual(self.api.call('POST', '/api/v1/admin/notices', {'title': 't', 'content': 'c'})[0], 403)
        status, payload, headers = admin.call('POST', '/api/v1/admin/notices', {'title': '점검 안내', 'content': '내일 점검'})
        self.assertEqual(status, 201, payload)
        notice = payload['data']
        self.assertEqual((notice['version'], headers['etag']), (1, '"1"'))
        path = '/api/v1/admin/notices/' + notice['notice_id']
        self.assertEqual(admin.call('PATCH', path, {'title': 'x'}, headers={'if-match': '"9"'})[1]['error']['code'], 'VERSION_CONFLICT')
        self.assertEqual(admin.call('PATCH', path, {}, headers={'if-match': '"1"'})[0], 422)
        status, payload, headers = admin.call('PATCH', path, {'content': '모레 점검'}, headers={'if-match': '"1"'})
        self.assertEqual((status, payload['data']['content'], payload['data']['title'], headers['etag']), (200, '모레 점검', '점검 안내', '"2"'))
        self.moment += timedelta(minutes=1)
        admin.call('POST', '/api/v1/admin/notices', {'title': '두 번째', 'content': '내용'})
        listed = self.api.call('GET', '/api/v1/notices?limit=1')[1]['data']
        self.assertEqual((listed['total_count'], listed['items'][0]['title']), (2, '두 번째'))
        rest = self.api.call('GET', '/api/v1/notices?limit=1&cursor=' + listed['next_cursor'])[1]['data']
        self.assertEqual(([i['title'] for i in rest['items']], rest['next_cursor']), (['점검 안내'], None))
        self.assertEqual(admin.call('DELETE', path)[0], 204)
        self.assertEqual(admin.call('DELETE', path)[0], 204)
        self.assertEqual(admin.call('DELETE', '/api/v1/admin/notices/' + uid())[0], 404)
        self.assertEqual(admin.call('PATCH', path, {'title': 'x'}, headers={'if-match': '"2"'})[0], 404)
        self.assertEqual(self.api.call('GET', '/api/v1/notices')[1]['data']['total_count'], 1)
        actions = [r['action'] for r in self.db_rows('SELECT action FROM audit_events ORDER BY created_at,action')]
        self.assertEqual(sorted(actions), ['NOTICE_CREATED', 'NOTICE_CREATED', 'NOTICE_DELETED', 'NOTICE_UPDATED'])
        self.assertEqual(self.api.call('GET', '/api/v1/notices?limit=101')[0], 422)

    def test_inquiries_own_list_admin_list_and_reply(self):
        admin = self.admin()
        self.login()
        self.assertEqual(self.api.call('POST', '/api/v1/inquiries', {'title': '', 'content': 'x'})[0], 422)
        self.assertEqual(self.api.call('POST', '/api/v1/inquiries', {'title': 't', 'content': 'x' * 2001})[0], 422)
        status, payload, _ = self.api.call('POST', '/api/v1/inquiries', {'title': '질문', 'content': '요약 노트가 안 보여요'})
        self.assertEqual(status, 201, payload)
        inquiry = payload['data']
        self.assertEqual((inquiry['status'], inquiry['reply'], inquiry['replied_at']), ('WAITING', None, None))
        self.assertNotIn('author_display_name', inquiry)
        mine = self.api.call('GET', '/api/v1/inquiries')[1]['data']
        self.assertEqual(mine['total_count'], 1)
        listed = admin.call('GET', '/api/v1/inquiries?status=WAITING')[1]['data']
        self.assertEqual((listed['total_count'], listed['items'][0]['author_display_name']), (1, '조현준'))
        self.assertNotIn('email', listed['items'][0])
        path = '/api/v1/admin/inquiries/' + inquiry['inquiry_id'] + '/reply'
        self.assertEqual(self.api.call('PUT', path, {'reply': '확인'})[0], 403)
        self.assertEqual(admin.call('PUT', '/api/v1/admin/inquiries/' + uid() + '/reply', {'reply': '확인'})[0], 404)
        status, payload, _ = admin.call('PUT', path, {'reply': '확인했습니다'})
        self.assertEqual((status, payload['data']['status'], payload['data']['reply']), (200, 'ANSWERED', '확인했습니다'))
        self.assertEqual(admin.call('PUT', path, {'reply': '수정한 답변'})[1]['data']['reply'], '수정한 답변')
        self.assertEqual(self.api.call('GET', '/api/v1/inquiries?status=ANSWERED')[1]['data']['items'][0]['reply'], '수정한 답변')
        self.assertEqual(admin.call('GET', '/api/v1/inquiries?status=WAITING')[1]['data']['total_count'], 0)
        self.assertEqual(self.api.call('GET', '/api/v1/inquiries?status=ACTIVE')[0], 422)
        self.assertEqual(self.row("SELECT count(*) n FROM audit_events WHERE action='INQUIRY_REPLIED'")['n'], 2)

    def test_guest_has_no_notice_or_inquiry_access(self):
        self.login()
        s = self.api.call('POST', '/api/v1/sessions', {'title': '강연', 'scheduled_at': self.moment.isoformat()})[1]['data']
        visitor = Client(HTTPApplication(self.service, 'backend'))
        visitor.call('POST', '/api/v1/participations', {'join_code': s['join_code'], 'nickname': 'g', 'service_consent': True})
        self.assertEqual(visitor.call('GET', '/api/v1/notices')[0], 403)
        self.assertEqual(visitor.call('POST', '/api/v1/inquiries', {'title': 't', 'content': 'c'})[0], 403)
        self.assertEqual(Client(HTTPApplication(self.service, 'backend')).call('GET', '/api/v1/notices')[0], 401)

    def db_rows(self, sql, args=()):
        with self.db.transaction() as tx:
            return tx.all(sql, args)


from tests.test_postgres import PostgresMixin, PG_DSN  # noqa: E402


@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PGNoticesTests(PostgresMixin, NoticesTests):
    pass
