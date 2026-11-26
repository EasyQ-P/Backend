"""Auth sessions: issue/revoke, auth public routes (API-01~09) and internal API INT-01~03."""
from datetime import timedelta

from easyq_common.contracts import (APIError, Result, dt, email, fields,
                                    uid, uuid_value)
from easyq_common.security import (digest, token)


class SessionsMixin:
    def issue(self, tx, user=None, participant=None, expires=None):
        raw, csrf, aid = token(), token(), uid()
        expires = expires or self.clock() + timedelta(hours=12)
        tx.execute("""INSERT INTO auth_sessions(auth_session_id,user_id,participant_id,token_hash,csrf_token_hash,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s)""", (aid, user, participant, digest(raw), digest(csrf), expires))
        max_age = max(0, int((expires - self.clock()).total_seconds()))
        cookie = f'easyq_session={raw}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}'
        if self.settings.cookie_secure:
            cookie += '; Secure'
        return {'csrf_token': csrf, 'expires_at': expires}, cookie

    def revoke(self, tx, user=None, participant=None, aid=None):
        column, value = ('user_id', user) if user else (('participant_id', participant) if participant else ('auth_session_id', aid))
        rows = tx.all(f"SELECT auth_session_id FROM auth_sessions WHERE {column}=%s AND revoked_at IS NULL FOR UPDATE", (value,))
        tx.execute(f"UPDATE auth_sessions SET revoked_at=%s WHERE {column}=%s AND revoked_at IS NULL", (self.clock(), value))
        for r in rows:
            self.event(tx, 'session.revoked', {'auth_session_id': str(r['auth_session_id'])})
        return len(rows)

    def auth_route(self, tx, req, path):
        method, b = req.method, req.body
        if method == 'POST':
            if path == '/users':
                return self.register(tx, req)
            if path == '/auth/email-verifications/confirm':
                return self.confirm(tx, req)
            if path == '/auth/sessions':
                return self.login(tx, req)
            if path == '/auth/password-resets':
                return self.reset_password(tx, req)
            if path in ('/auth/email-verifications', '/auth/password-reset-requests'):
                fields(b, ['email'], ['email'])
                address = email(b['email'])
                self.mail_rate(tx, address, req.ip)
                user = tx.one('SELECT * FROM users WHERE email=%s FOR UPDATE', (address,))
                reset = path == '/auth/password-reset-requests'
                if user and ((reset and user['status'] == 'ACTIVE' and user['email_verified_at']) or (not reset and user['status'] == 'PENDING_VERIFICATION')):
                    self.queue_mail(tx, user, reset)
                return Result(202, {} if reset else {'message': '인증이 필요한 계정이면 메일을 발송합니다.'})
        if path == '/auth/sessions/current' and method == 'DELETE':
            # Repeated expired logout still checks Origin, but needs no valid CSRF.
            try:
                p = self.principal(tx, req)
            except APIError as e:
                if e.status != 401:
                    raise
                p = None
            if p:
                self.csrf(req, p)
                self.revoke(tx, aid=str(p['auth_session_id']))
            return Result(204, headers={'Set-Cookie': 'easyq_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' + ('; Secure' if self.settings.cookie_secure else '')})
        p = self.principal(tx, req)
        if path == '/auth/csrf-tokens' and method == 'POST':
            fields(b, [])
            if req.headers.get('x-easyq-client') != 'web':
                raise APIError(403, 'FORBIDDEN')
            csrf = token()
            tx.execute('UPDATE auth_sessions SET csrf_token_hash=%s WHERE auth_session_id=%s', (digest(csrf), p['auth_session_id']))
            self.event(tx, 'auth.csrf_rotated',{'auth_session_id': str(p['auth_session_id'])})
            return Result(200, {'csrf_token': csrf})
        if path == '/me' and method == 'GET':
            user_id = self.member(p)
            return Result(200, self.user_public(tx, tx.one('SELECT * FROM users WHERE user_id=%s', (user_id,))))
        raise APIError(404, 'RESOURCE_NOT_FOUND')

    def internal(self, tx, req):
        b = req.body
        if req.method != 'POST':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if req.path == '/internal/v1/guest-sessions':
            fields(b, ['participant_id', 'request_id'], ['participant_id', 'request_id'])
            pid = uuid_value(b['participant_id'])
            uuid_value(b['request_id'])
            tx.lock('guest-auth:' + pid)
            g = tx.one("SELECT * FROM participants WHERE participant_id=%s AND kind='GUEST' AND status='ACTIVE'", (pid,))
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s', (g['session_id'],)) if g else None
            if not s or s['status'] not in ('DRAFT', 'LIVE') or self.clock() >= dt(g['joined_at']) + timedelta(hours=24):
                raise APIError(403, 'FORBIDDEN')
            self.revoke(tx, participant=pid)
            data, cookie = self.issue(tx, participant=pid, expires=dt(g['joined_at']) + timedelta(hours=24))
            return Result(200, {**data, 'set_cookie': cookie})
        if req.path == '/internal/v1/session-revocations':
            fields(b, ['user_id', 'participant_id', 'reason', 'request_id'], ['reason', 'request_id'])
            if bool(b.get('user_id')) == bool(b.get('participant_id')):
                raise APIError(422, 'VALIDATION_ERROR')
            uuid_value(b['request_id'])
            column = 'user_id' if b.get('user_id') else 'participant_id'
            return Result(200, {'revoked_count': self.revoke(tx, **{column.removesuffix('_id'): uuid_value(b[column])})})
        if req.path == '/internal/v1/host-session-renewals':
            fields(b, ['auth_session_id', 'session_id', 'request_id'], ['auth_session_id', 'session_id', 'request_id'])
            aid, sid = uuid_value(b['auth_session_id']), uuid_value(b['session_id'])
            a = tx.one('SELECT * FROM auth_sessions WHERE auth_session_id=%s FOR UPDATE', (aid,))
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,))
            u = tx.one('SELECT * FROM users WHERE user_id=%s', (a['user_id'],)) if a else None
            if not a or not u or u['status'] != 'ACTIVE' or not u['email_verified_at'] or not s or a['revoked_at'] or dt(a['expires_at']) <= self.clock() or s['status'] != 'LIVE' or str(s['host_user_id']) != str(a['user_id']):
                raise APIError(403, 'FORBIDDEN')
            if (a['renewed_at'] and self.clock() - dt(a['renewed_at']) < timedelta(minutes=15)) or self.clock() >= dt(a['created_at']) + timedelta(hours=24):
                return Result(200, {'expires_at': a['expires_at'], 'renewed': False})
            expires = min(self.clock() + timedelta(hours=12), dt(a['created_at']) + timedelta(hours=24))
            tx.execute('UPDATE auth_sessions SET expires_at=%s,renewed_at=%s WHERE auth_session_id=%s', (expires, self.clock(), aid))
            return Result(200, {'expires_at': expires, 'renewed': True})
        raise APIError(404, 'RESOURCE_NOT_FOUND')
