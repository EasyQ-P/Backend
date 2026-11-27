"""Entry (API-18 · 19): join-code lookup and rate limit, member/guest participation."""
from datetime import timedelta
from uuid import UUID

from easyq_common.contracts import (APIError, Result, dt, fields,
                                    text, uid, uuid_value)
from easyq_common.security import (credential_hash, digest)


class ParticipationMixin:
    def entry_principal(self, tx, req):
        """Principal for join preview/entry. A stale guest cookie (ended lecture,
        revoked or expired) must not block joining another lecture as a new guest;
        an invalid member session still asks the member to log in again."""
        try:
            return self.principal(tx, req, optional=True)
        except APIError:
            row = tx.one('SELECT user_id FROM auth_session_view WHERE token_hash=%s', (digest(req.cookies['easyq_session']),))
            if row and row['user_id']:
                raise
            return None

    def join_lookup(self, tx, code, ip):
        code = text(code, 5).upper()
        self.check_invalid_join_rate(tx, ip)
        s = tx.one('SELECT * FROM lecture_sessions WHERE join_code=%s FOR UPDATE', (code,))
        if not s:
            self.rate(tx, 'invalid-join', ip, 20, 900)
            bucket = credential_hash(self.settings.hmac_key, 'rate', 'invalid-join', ip)
            tx.execute('UPDATE auth_rate_limits SET expires_at=%s WHERE bucket_key=%s AND request_count>=20', (self.clock() + timedelta(minutes=15), bucket))
            revoked = tx.one('SELECT session_id FROM lecture_sessions WHERE previous_join_code=%s', (code,))
            raise APIError(410 if revoked else 404, 'JOIN_CODE_REVOKED' if revoked else 'JOIN_CODE_INVALID', commit=True)
        if s['status'] not in ('DRAFT', 'LIVE') or s['host_deleted_at']:
            raise APIError(409, 'SESSION_ENDED', commit=True)
        return s

    def check_invalid_join_rate(self, tx, ip):
        bucket = credential_hash(self.settings.hmac_key, 'rate', 'invalid-join', ip)
        row = tx.one('SELECT * FROM auth_rate_limits WHERE bucket_key=%s ORDER BY window_start DESC LIMIT 1', (bucket,))
        if row and row['request_count'] >= 20 and dt(row['expires_at']) > self.clock():
            raise APIError(429, 'RATE_LIMITED', commit=True, headers={'Retry-After': str(max(1, int((dt(row['expires_at']) - self.clock()).total_seconds())))})

    def guest_retry_id(self, sid, key):
        return str(UUID(credential_hash(self.settings.hmac_key, 'guest-idempotency', sid, key)[:32]))

    def join(self, tx, req, p):
        b = req.body
        fields(b, ['join_code', 'nickname', 'service_consent', 'training_consent'], ['join_code'])
        s = self.join_lookup(tx, b['join_code'], req.ip)
        sid = str(s['session_id'])
        if p and p['user_id'] and str(p['user_id']) == str(s['host_user_id']):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if p and not p['user_id'] and str(p['guest_session_id']) != sid:
            # A guest session belongs to one lecture; entering another lecture starts a new guest.
            p = None
        existing = self.participant(tx, sid, p) if p else None
        user_id = str(p['user_id']) if p and p['user_id'] else None
        # Guest retry identity: a client-secret UUID idempotency header or the signed
        # retry cookie; never nickname/IP. The header value is never used as the
        # participant ID itself, so knowing another guest's participant_id is useless.
        retry_pid = None
        if not p:
            key = req.headers.get('idempotency-key')
            marker = req.cookies.get('easyq_join_retry', '')
            if key:
                retry_pid = self.guest_retry_id(sid, uuid_value(key))
            elif marker:
                candidate = self.join_retry_participant(marker, sid)
                if candidate:
                    retry_pid = uuid_value(candidate)
        if retry_pid:
            tx.lock('guest:' + retry_pid)
            prior = tx.one('SELECT * FROM participants WHERE participant_id=%s', (retry_pid,))
            if prior and prior['status'] == 'ACTIVE' and prior['kind'] == 'GUEST' and str(prior['session_id']) == sid:
                existing = prior
            elif prior:
                retry_pid = None  # Revoked or foreign row: never reuse its ID, start a fresh guest.
        previous = None
        if user_id and not existing:
            # UNIQUE(session_id,user_id) covers REVOKED/archived rows too: reactivate instead of inserting.
            previous = tx.one('SELECT * FROM participants WHERE session_id=%s AND user_id=%s', (sid, user_id))
        if not existing and self.participant_count(tx, sid) >= 50:
            raise APIError(409, 'SESSION_FULL')
        if user_id:
            self.require_service(tx, user=user_id)
            scoped = self.effective(tx, 'SERVICE', user=user_id, sid=sid)
            if not scoped or not scoped['granted']:
                # This lecture's SERVICE consent was withdrawn: rejoining needs a new explicit grant.
                if 'service_consent' not in b or not self.entrance_consent(b['service_consent'], 'SERVICE')['granted']:
                    raise APIError(403, 'CONSENT_REQUIRED')
                self.save_consent(tx, 'SERVICE', self.entrance_consent(b['service_consent'], 'SERVICE'), user=user_id, sid=sid)
            nickname = None
        else:
            nickname = text(b.get('nickname'), 20)
            if not self.entrance_consent(b.get('service_consent', {}), 'SERVICE')['granted']:
                raise APIError(403, 'CONSENT_REQUIRED')
        if existing:
            pid = str(existing['participant_id'])
        elif previous:
            pid = str(previous['participant_id'])
            tx.execute("UPDATE participants SET status='ACTIVE',archived=true WHERE participant_id=%s", (pid,))
        else:
            pid = retry_pid or uid()
            # joined_at from the service clock: the guest auth period (joined_at + 24 h) and the retry cookie use it.
            tx.execute("""INSERT INTO participants(participant_id,session_id,user_id,kind,nickname,archived,joined_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", (pid, sid, user_id, 'MEMBER' if user_id else 'GUEST', nickname, bool(user_id), self.clock()))
        if not user_id:
            self.save_consent(tx, 'SERVICE', self.entrance_consent(b['service_consent'], 'SERVICE'), participant=pid, sid=sid)
        if not user_id or 'training_consent' in b:
            self.save_consent(tx, 'TRAINING', self.entrance_consent(b.get('training_consent', False), 'TRAINING'), user=user_id, participant=None if user_id else pid, sid=sid)
        headers = {}
        # 7.3.1 Participation: nickname = guest nickname, or the member's display_name; session_status DRAFT/LIVE.
        if user_id:
            shown = tx.one('SELECT display_name FROM users WHERE user_id=%s', (user_id,))['display_name']
        else:
            shown = existing['nickname'] if existing and existing.get('nickname') else nickname
        data = {'participant_id': pid, 'session_id': sid, 'kind': 'MEMBER' if user_id else 'GUEST', 'nickname': shown,
                'session_status': s['status'], 'expires_at': self.clock() + timedelta(hours=24)}
        if not user_id:
            # Participant must commit before auth can verify it in a separate transaction.
            # execute() invokes the exchange after the participant transaction commits.
            data['_guest_exchange'] = True
            joined = existing['joined_at'] if existing else self.clock()
            data['_retry_expires'] = data['expires_at'] = dt(joined) + timedelta(hours=24)
        if not existing:
            self.event(tx, 'session.updated', {'participant_count': self.participant_count(tx, sid)}, sid)
        return Result(200 if existing else 201, data, headers)
