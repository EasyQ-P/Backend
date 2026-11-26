"""ServiceCore: what auth, backend and worker share — transaction/execute flow, sessions and CSRF, consents,
events and jobs, rate limits, lecture access and the lecture end flow (used by API-16 and the worker AUTO_END).

Container-specific behaviour lives in mixins: easyq_auth (accounts, sessions, internal API) and
easyq_backend (lectures, participation, consents, documents, routes). All writes use parameterized SQL and
explicit transactions; expected failures that must keep rate/attempt counters set commit=True.
"""
from datetime import timedelta
import hmac

from easyq_common.contracts import (APIError, Result, consent, dt, dumps, now, uid)
from easyq_common.security import (credential_hash, digest, password_hash)


class ServiceCore:
    def __init__(self, db, settings, storage, guest_client=None, clock=now):
        self.db, self.settings, self.storage = db, settings, storage
        self.guest_client, self.clock = guest_client, clock
        self.dummy_password = password_hash('TimingOnly123')

    def execute(self, req, mode='backend'):
        try:
            return self._execute(req, mode)
        except Exception:
            staged = getattr(req, '_staged_object', None)
            if staged:
                try:
                    self.storage.delete(staged)
                except Exception:
                    pass  # Bucket lifecycle policy handles abandoned generated keys.
            raise

    def _execute(self, req, mode='backend'):
        """Do not catch DB errors here: the transport translates them to safe 503s."""
        if req.path == '/health/live' and req.method == 'GET':
            return Result(200, {'status': 'ok'})
        error = None
        with self.db.transaction() as tx:
            try:
                result = self.route(tx, req, mode)
            except APIError as exc:
                if not exc.commit:
                    raise
                error = exc
        if error:
            raise error
        if isinstance(result.data, dict) and result.data.pop('_guest_exchange', False):
            pid = result.data['participant_id']
            marker = pid + '.' + credential_hash(self.settings.hmac_key, 'guest-retry', result.data['session_id'], pid)
            retry_cookie = 'easyq_join_retry=' + marker + '; Path=/api/v1/participations; HttpOnly; SameSite=Lax; Max-Age=600' + ('; Secure' if self.settings.cookie_secure else '')
            try:
                exchange = self.guest_client.issue(pid, uid())
            except Exception:
                raise APIError(503, 'AUTH_SERVICE_UNAVAILABLE', headers={'Set-Cookie': retry_cookie}) from None
            result.data.update(csrf_token=exchange['csrf_token'], expires_at=exchange['expires_at'])
            result.headers['Set-Cookie'] = exchange['set_cookie']
        if mode == 'backend' and isinstance(result.data, dict) and result.data.get('my_role') == 'HOST' and result.data.get('status') == 'LIVE' and hasattr(self.guest_client, 'renew'):
            try:
                with self.db.transaction() as tx:
                    principal = self.principal(tx, req)
                renewed = self.guest_client.renew(str(principal['auth_session_id']), str(result.data['session_id']), uid())
                if renewed['renewed']:
                    remaining = max(0, int((dt(renewed['expires_at']) - self.clock()).total_seconds()))
                    result.headers['Set-Cookie'] = 'easyq_session=' + req.cookies['easyq_session'] + '; Path=/; HttpOnly; SameSite=Lax; Max-Age=' + str(remaining) + ('; Secure' if self.settings.cookie_secure else '')
            except Exception:
                pass  # Existing valid sessions keep working while auth is down.
        return result

    def event(self, tx, event_type, payload, sid=None):
        row = tx.one("""INSERT INTO ws_events(event_type,payload,session_id,expires_at)
            VALUES(%s,%s,%s,%s) RETURNING event_id""",
            (event_type, dumps(payload), sid, self.clock() + timedelta(hours=24)))
        tx.notify(row['event_id'])

    def job(self, tx, kind, target, key, payload, user=None, sid=None, participant=None, when=None):
        jid = uid()
        tx.execute("""INSERT INTO jobs(job_id,job_type,target_id,idempotency_key,payload,
            requested_by,session_id,participant_id,available_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (jid, kind, target, key, dumps(payload), user, sid, participant, when or self.clock()))
        return jid

    def rate(self, tx, purpose, identity, limit, seconds=3600, gap=0, count=True):
        moment = self.clock()
        # Fixed windows plus a cooldown, both shared by all auth replicas.
        start = moment.fromtimestamp(int(moment.timestamp()) // seconds * seconds, moment.tzinfo)
        bucket = credential_hash(self.settings.hmac_key, 'rate', purpose, identity)
        tx.lock('rate:' + bucket)
        latest = tx.one("SELECT * FROM auth_rate_limits WHERE bucket_key=%s ORDER BY window_start DESC LIMIT 1", (bucket,))
        if latest and gap and (moment - dt(latest['last_request_at'])).total_seconds() < gap:
            remaining = max(1, int(gap - (moment - dt(latest['last_request_at'])).total_seconds()))
            raise APIError(429, 'RATE_LIMITED', commit=True, headers={'Retry-After': str(remaining)})
        row = tx.one("SELECT * FROM auth_rate_limits WHERE bucket_key=%s AND window_start=%s", (bucket, start))
        if row and row['request_count'] >= limit:
            raise APIError(429, 'RATE_LIMITED', commit=True, headers={'Retry-After': str(max(1, int((start + timedelta(seconds=seconds) - moment).total_seconds())))})
        if not count:
            return
        tx.execute("""INSERT INTO auth_rate_limits(bucket_key,window_start,request_count,last_request_at,expires_at)
            VALUES(%s,%s,1,%s,%s) ON CONFLICT(bucket_key,window_start) DO UPDATE
            SET request_count=auth_rate_limits.request_count+1,last_request_at=excluded.last_request_at""",
            (bucket, start, moment, start + timedelta(seconds=seconds)))

    def principal(self, tx, req, optional=False):
        raw = req.cookies.get('easyq_session')
        if not raw:
            if optional:
                return None
            raise APIError(401, 'AUTH_REQUIRED')
        p = tx.one("SELECT * FROM auth_session_view WHERE token_hash=%s", (digest(raw),))
        if not p or p['revoked_at'] or dt(p['expires_at']) <= self.clock():
            raise APIError(401, 'AUTH_REQUIRED')
        if p['user_id']:
            if p['user_status'] != 'ACTIVE' or not p['email_verified']:
                raise APIError(401, 'AUTH_REQUIRED')
        else:
            lecture = tx.one("SELECT status FROM lecture_sessions WHERE session_id=%s", (p['guest_session_id'],))
            if p['guest_status'] != 'ACTIVE' or not lecture or lecture['status'] not in ('DRAFT', 'LIVE'):
                raise APIError(401, 'AUTH_REQUIRED')
        return p

    def csrf(self, req, principal):
        value = req.headers.get('x-csrf-token', '')
        if not value or not hmac.compare_digest(digest(value), principal['csrf_token_hash']):
            # Code stays FORBIDDEN (8.4.2); details.reason lets the client re-issue (API-06) and retry once.
            raise APIError(403, 'FORBIDDEN', {'reason': 'CSRF'})

    def require_admin(self, principal):
        """Admin APIs (7.1.1): ADMIN role of the current account, never the HOST/participant relation."""
        if not principal or not principal['user_id'] or principal['user_role'] != 'ADMIN':
            raise APIError(403, 'FORBIDDEN')
        return str(principal['user_id'])

    def member(self, principal):
        if not principal or not principal['user_id']:
            raise APIError(403, 'FORBIDDEN')
        return str(principal['user_id'])

    def save_consent(self, tx, purpose, value, user=None, participant=None, sid=None):
        cid = uid()
        value = consent(value)
        recorded = self.clock()
        tx.execute("""INSERT INTO consents(consent_id,session_id,user_id,participant_id,purpose,policy_version,granted,recorded_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, sid, user, participant, purpose, value['policy_version'], value['granted'], recorded))
        return {'consent_id': cid, 'purpose': purpose, **value, 'recorded_at': recorded}

    def entrance_consent(self, value, purpose):
        # The final HWP's guest example uses booleans; pin their policy versions
        # on the server while also accepting the explicit policy object contract.
        if type(value) is bool:
            return {'granted': value, 'policy_version': self.settings.service_policy_version if purpose == 'SERVICE' else self.settings.training_policy_version}
        return consent(value)

    def effective(self, tx, purpose, user=None, participant=None, sid=None):
        if user:
            rows = tx.all("""SELECT * FROM consents WHERE user_id=%s AND purpose=%s
                AND (session_id=%s OR session_id IS NULL) ORDER BY recorded_at DESC,consent_id DESC""", (user, purpose, sid))
        else:
            rows = tx.all("""SELECT * FROM consents WHERE participant_id=%s AND session_id=%s AND purpose=%s
                ORDER BY recorded_at DESC,consent_id DESC""", (participant, sid, purpose))
        scoped = next((r for r in rows if r['session_id'] is not None), None)
        account = next((r for r in rows if r['session_id'] is None), None)
        # Account training withdrawal overrides any older lecture grant.
        # The signup's initial false is an optional refusal, not a later
        # account-wide withdrawal. An explicit later false overrides all scopes.
        if purpose == 'TRAINING' and account and not account['granted'] and sum(r['session_id'] is None for r in rows) > 1:
            return account
        return scoped or account

    def require_service(self, tx, user=None, participant=None, sid=None):
        value = self.effective(tx, 'SERVICE', user, participant, sid)
        if not value or not value['granted']:
            raise APIError(403, 'CONSENT_REQUIRED')

    def lecture(self, tx, sid, principal, host=False):
        lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
        if not lecture or lecture['status'] == 'DELETING' or lecture['host_deleted_at']:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        owner = principal['user_id'] and str(lecture['host_user_id']) == str(principal['user_id'])
        participant = self.participant(tx, sid, principal)
        if (host and not owner) or (not host and not owner and not participant):
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        return lecture

    def participant(self, tx, sid, principal):
        column, value = ('user_id', principal['user_id']) if principal['user_id'] else ('participant_id', principal['participant_id'])
        # archived = the lecture is in the member's archive (8.2.8: default true, false after archive delete; guests false).
        return tx.one(f"SELECT * FROM participants WHERE session_id=%s AND {column}=%s AND status='ACTIVE' AND (kind='GUEST' OR archived=true)", (sid, value))

    def participant_count(self, tx, sid):
        return tx.one("SELECT count(*) AS n FROM participants WHERE session_id=%s AND status='ACTIVE'", (sid,))['n']

    def version(self, req, lecture):
        if req.headers.get('if-match') != f'"{lecture["version"]}"':
            raise APIError(409, 'VERSION_CONFLICT')

    def parse_date(self, value):
        try:
            parsed = dt(value)
            if parsed.tzinfo is None:
                raise ValueError()
            return parsed
        except (ValueError, TypeError):
            raise APIError(422, 'VALIDATION_ERROR') from None

    def end(self, tx, s, reason='HOST'):
        when = self.clock()
        tx.execute("""UPDATE lecture_sessions SET status='ENDED',stt_status='STOPPED',end_reason=%s,
            ended_at=%s,raw_data_expires_at=%s,version=version+1 WHERE session_id=%s""", (reason, when, when + timedelta(days=90), s['session_id']))
        tx.execute("UPDATE participants SET status='REVOKED' WHERE session_id=%s AND kind='GUEST'", (s['session_id'],))
        # Auth revocation is retried out of process; backend rejects revoked guests immediately.
        guests = tx.all("SELECT participant_id FROM participants WHERE session_id=%s AND kind='GUEST'", (s['session_id'],))
        for g in guests:
            self.job(tx, 'AUTH_REVOKE', str(g['participant_id']), 'end-revoke:' + str(g['participant_id']),
                     {'participant_id': str(g['participant_id']), 'reason': 'LECTURE_ENDED'}, sid=str(s['session_id']))
        self.job(tx, 'FINALIZE', str(s['session_id']), 'finalize:' + str(s['session_id']), {}, user=str(s['host_user_id']), sid=str(s['session_id']))

    def route(self, tx, req, mode):
        path = req.path.removeprefix('/api/v1')
        method = req.method
        if req.path == '/health/live':
            return Result(200, {'status': 'ok'})
        if req.path == '/health/ready':
            tx.one('SELECT 1 AS ok')
            return Result(200, {'status': 'ready'})
        if req.path.startswith('/internal/v1/'):
            supplied = req.headers.get('x-service-key', '')
            backend_key = hmac.compare_digest(supplied, self.settings.internal_key)
            worker_key = bool(self.settings.worker_internal_key) and hmac.compare_digest(supplied, self.settings.worker_internal_key)
            if mode != 'auth' or not (backend_key or (worker_key and req.path == '/internal/v1/session-revocations')):
                raise APIError(403, 'FORBIDDEN')
            return self.internal(tx, req)
        if not req.path.startswith('/api/v1/'):
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        auth_paths = ['/users', '/auth/email-verifications', '/auth/email-verifications/confirm', '/auth/sessions',
                      '/auth/sessions/current', '/auth/csrf-tokens', '/auth/password-reset-requests', '/auth/password-resets', '/me']
        if (mode == 'auth') != (path in auth_paths):
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if method not in ('GET', 'HEAD'):
            if req.headers.get('origin') != self.settings.origin:
                raise APIError(403, 'FORBIDDEN')
        if mode == 'auth':
            return self.auth_route(tx, req, path)
        return self.backend_route(tx, req, path)
