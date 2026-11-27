"""Notices and 1:1 inquiries (API-42~44 · 49~52).

Notice responses add `version` (and ETag on API-49/50) so the admin screen can send If-Match to API-50
(review F-6: notices.version exists in 8.2.32 but the 7.3.1 Notice object had no field for it).
Admin changes write audit_events in the same transaction (7.3.7).
"""
import base64
import json

from easyq_common.contracts import APIError, Result, dt, dumps, fields, text, uid, uuid_value


def page_args(query, allowed):
    """cursor/limit (7.1.2): limit 1~100 (default 20); cursor = opaque (created_at, id) of the last item."""
    if set(query) - set(allowed) - {'cursor', 'limit'}:
        raise APIError(422, 'VALIDATION_ERROR')
    try:
        limit = int(query.get('limit', 20))
    except ValueError:
        raise APIError(422, 'VALIDATION_ERROR') from None
    if not 1 <= limit <= 100:
        raise APIError(422, 'VALIDATION_ERROR')
    after = None
    if query.get('cursor'):
        try:
            raw = json.loads(base64.urlsafe_b64decode(query['cursor'].encode()))
            after = (dt(raw['t']), uuid_value(raw['id']))
        except (ValueError, KeyError, TypeError):
            raise APIError(422, 'VALIDATION_ERROR') from None
    return limit, after


def next_cursor(rows, limit, idcol):
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return base64.urlsafe_b64encode(json.dumps({'t': str(last['created_at']), 'id': str(last[idcol])}).encode()).decode()


class NoticesMixin:
    ROUTES = [
        ('GET', r'/notices', 'list_notices'),
        ('POST', r'/admin/notices', 'create_notice'),
        ('PATCH', r'/admin/notices/(?P<notice_id>[^/]+)', 'update_notice'),
        ('DELETE', r'/admin/notices/(?P<notice_id>[^/]+)', 'delete_notice'),
        ('POST', r'/inquiries', 'create_inquiry'),
        ('GET', r'/inquiries', 'list_inquiries'),
        ('PUT', r'/admin/inquiries/(?P<inquiry_id>[^/]+)/reply', 'reply_inquiry'),
    ]
    NOTICE_FIELDS = ('notice_id', 'title', 'content', 'version', 'created_at', 'updated_at')
    INQUIRY_FIELDS = ('inquiry_id', 'title', 'content', 'status', 'reply', 'replied_at', 'created_at')

    def notice_audit(self, tx, actor, action, target_type, target_ref, changes, reason=None):
        tx.execute("""INSERT INTO audit_events(audit_event_id,actor_user_id,action,target_type,target_ref,changes,reason,request_id,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (uid(), actor, action, target_type, target_ref, dumps(changes), reason, uid(), self.clock()))

    def notice_public(self, row):
        return {k: row[k] for k in self.NOTICE_FIELDS}

    def inquiry_public(self, row, admin=False):
        data = {k: row[k] for k in self.INQUIRY_FIELDS}
        if admin:
            data['author_display_name'] = row.get('author_display_name')   # never the e-mail (API-44)
        return data

    # ---- API-42 공지 목록 ----------------------------------------------------------------
    def list_notices(self, tx, req, p):
        self.member(p)   # MEMBER · ADMIN; guests have no account screen
        limit, after = page_args(req.query, ())
        base = 'FROM notices WHERE deleted_at IS NULL'
        total = tx.one('SELECT count(*) AS n ' + base)['n']
        args = []
        if after:
            base += ' AND (created_at<%s OR (created_at=%s AND notice_id<%s))'
            args = [after[0], after[0], after[1]]
        rows = tx.all('SELECT * ' + base + ' ORDER BY created_at DESC, notice_id DESC LIMIT %s', tuple(args + [limit + 1]))
        return Result(200, {'items': [self.notice_public(r) for r in rows[:limit]], 'next_cursor': next_cursor(rows, limit, 'notice_id'),
                            'total_count': total})

    # ---- API-49 공지 등록 ----------------------------------------------------------------
    def create_notice(self, tx, req, p):
        admin = self.require_admin(p)
        fields(req.body, ['title', 'content'], ['title', 'content'])
        title, content = text(req.body['title'], 100), text(req.body['content'], 2000)
        nid, moment = uid(), self.clock()
        tx.execute("""INSERT INTO notices(notice_id,author_user_id,title,content,version,created_at,updated_at)
            VALUES(%s,%s,%s,%s,1,%s,%s)""", (nid, admin, title, content, moment, moment))
        self.notice_audit(tx, admin, 'NOTICE_CREATED', 'NOTICE', nid, {'version': {'before': None, 'after': 1}})
        row = tx.one('SELECT * FROM notices WHERE notice_id=%s', (nid,))
        return Result(201, self.notice_public(row), {'ETag': '"1"'})

    # ---- API-50 공지 수정 ----------------------------------------------------------------
    def update_notice(self, tx, req, p, notice_id):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['title', 'content'])
        if not b:
            raise APIError(422, 'VALIDATION_ERROR')
        changes = {}
        if 'title' in b:
            changes['title'] = text(b['title'], 100)
        if 'content' in b:
            changes['content'] = text(b['content'], 2000)
        row = tx.one('SELECT * FROM notices WHERE notice_id=%s FOR UPDATE', (notice_id,))
        if not row or row['deleted_at']:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        self.version(req, row)
        version = row['version'] + 1
        sets = ','.join(f'{k}=%s' for k in changes)
        tx.execute(f'UPDATE notices SET {sets},version=%s,updated_at=%s WHERE notice_id=%s',
                   (*changes.values(), version, self.clock(), notice_id))
        self.notice_audit(tx, admin, 'NOTICE_UPDATED', 'NOTICE', notice_id,
                         {'fields': sorted(changes), 'version': {'before': row['version'], 'after': version}})
        row = tx.one('SELECT * FROM notices WHERE notice_id=%s', (notice_id,))
        return Result(200, self.notice_public(row), {'ETag': f'"{version}"'})

    # ---- API-51 공지 삭제 ----------------------------------------------------------------
    def delete_notice(self, tx, req, p, notice_id):
        admin = self.require_admin(p)
        row = tx.one('SELECT * FROM notices WHERE notice_id=%s FOR UPDATE', (notice_id,))
        if not row:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if not row['deleted_at']:
            tx.execute('UPDATE notices SET deleted_at=%s WHERE notice_id=%s', (self.clock(), notice_id))
            self.notice_audit(tx, admin, 'NOTICE_DELETED', 'NOTICE', notice_id, {'deleted': {'before': False, 'after': True}})
        return Result(204)

    # ---- API-43 1:1 문의 등록 -------------------------------------------------------------
    def create_inquiry(self, tx, req, p):
        user = self.member(p)
        fields(req.body, ['title', 'content'], ['title', 'content'])
        title, content = text(req.body['title'], 100), text(req.body['content'], 2000)
        iid = uid()
        tx.execute("""INSERT INTO inquiries(inquiry_id,user_id,title,content,status,created_at)
            VALUES(%s,%s,%s,%s,'WAITING',%s)""", (iid, user, title, content, self.clock()))
        return Result(201, self.inquiry_public(tx.one('SELECT * FROM inquiries WHERE inquiry_id=%s', (iid,))))

    # ---- API-44 문의 목록 ----------------------------------------------------------------
    def list_inquiries(self, tx, req, p):
        user = self.member(p)
        admin = p['user_role'] == 'ADMIN'
        limit, after = page_args(req.query, ('status',))
        where, args = [], []
        if not admin:
            where.append('i.user_id=%s')
            args.append(user)
        if 'status' in req.query:
            if req.query['status'] not in ('WAITING', 'ANSWERED'):
                raise APIError(422, 'VALIDATION_ERROR')
            where.append('i.status=%s')
            args.append(req.query['status'])
        base = 'FROM inquiries i JOIN users u ON u.user_id=i.user_id' + (' WHERE ' + ' AND '.join(where) if where else '')
        total = tx.one('SELECT count(*) AS n ' + base, tuple(args))['n']
        if after:
            base += (' AND ' if where else ' WHERE ') + '(i.created_at<%s OR (i.created_at=%s AND i.inquiry_id<%s))'
            args += [after[0], after[0], after[1]]
        rows = tx.all('SELECT i.*,u.display_name AS author_display_name ' + base + ' ORDER BY i.created_at DESC, i.inquiry_id DESC LIMIT %s',
                      tuple(args + [limit + 1]))
        return Result(200, {'items': [self.inquiry_public(r, admin) for r in rows[:limit]],
                            'next_cursor': next_cursor(rows, limit, 'inquiry_id'), 'total_count': total})

    # ---- API-52 문의 답변 ----------------------------------------------------------------
    def reply_inquiry(self, tx, req, p, inquiry_id):
        admin = self.require_admin(p)
        fields(req.body, ['reply'], ['reply'])
        reply = text(req.body['reply'], 2000)
        row = tx.one('SELECT * FROM inquiries WHERE inquiry_id=%s FOR UPDATE', (inquiry_id,))
        if not row:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        tx.execute("UPDATE inquiries SET reply=%s,replied_by=%s,replied_at=%s,status='ANSWERED' WHERE inquiry_id=%s",
                   (reply, admin, self.clock(), inquiry_id))
        self.notice_audit(tx, admin, 'INQUIRY_REPLIED', 'INQUIRY', inquiry_id, {'status': {'before': row['status'], 'after': 'ANSWERED'}})
        row = tx.one("""SELECT i.*,u.display_name AS author_display_name FROM inquiries i JOIN users u ON u.user_id=i.user_id
            WHERE i.inquiry_id=%s""", (inquiry_id,))
        return Result(200, self.inquiry_public(row, True))


OPENAPI = [
    ('GET', '/notices', 'API-42', '공지 목록 (cursor · limit, Notice에 version 포함)'),
    ('POST', '/inquiries', 'API-43', '1:1 문의 등록'),
    ('GET', '/inquiries', 'API-44', '문의 목록: 회원은 본인 것, ADMIN은 전체(작성자 표시명)'),
    ('POST', '/admin/notices', 'API-49', '공지 등록 (ETag)'),
    ('PATCH', '/admin/notices/{notice_id}', 'API-50', '공지 수정 (If-Match: Notice.version)'),
    ('DELETE', '/admin/notices/{notice_id}', 'API-51', '공지 삭제 (deleted_at, 반복 시 204)'),
    ('PUT', '/admin/inquiries/{inquiry_id}/reply', 'API-52', '문의 답변 (다시 보내면 수정)'),
]
