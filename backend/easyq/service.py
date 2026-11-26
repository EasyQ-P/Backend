"""Application service shared by the auth/backend FastAPI processes and API tests.

All writes use the supplied schema, parameterized SQL and explicit transactions.
Expected failures which must preserve rate/attempt counters set commit=True.
"""
from datetime import timedelta
import hmac
import re
import secrets
from uuid import UUID

from .contracts import (APIError, Result, consent, dt, dumps, email, fields,
                        now, obj, text, uid, uuid_value)
from .security import (credential_hash, digest, encrypt, password_hash,
                       password_matches, token, valid_password)


class Service:
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

    def mail_rate(self, tx, address, ip):
        self.rate(tx, 'mail-ip', ip, 20)
        self.rate(tx, 'mail-email', address, 5, gap=60)

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

    def csrf(self, req, principal):
        value = req.headers.get('x-csrf-token', '')
        if not value or not hmac.compare_digest(digest(value), principal['csrf_token_hash']):
            raise APIError(403, 'FORBIDDEN')

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

    def user_public(self, tx, user):
        data = {k: user[k] for k in ['user_id', 'email', 'display_name', 'role', 'status', 'email_verified_at']}
        data['consents'] = {}
        for purpose in ('SERVICE', 'TRAINING'):
            c = self.effective(tx, purpose, user=str(user['user_id']))
            data['consents'][purpose] = {'policy_version': c['policy_version'], 'granted': bool(c['granted'])} if c else None
        return data

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

    def queue_mail(self, tx, user, reset=False):
        table, idcol, hashcol, kind = ('password_resets', 'reset_id', 'token_hash', 'PASSWORD_RESET') if reset else ('email_verifications', 'verification_id', 'code_hash', 'EMAIL_VERIFICATION')
        current = self.clock()
        tx.execute(f"UPDATE {table} SET revoked_at=%s,delivery_ciphertext=NULL WHERE user_id=%s AND consumed_at IS NULL AND revoked_at IS NULL", (current, user['user_id']))
        tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE requested_by=%s
            AND job_type=%s AND status IN ('QUEUED','RUNNING')""", (current, user['user_id'], kind))
        rid = uid()
        raw = token() if reset else f'{secrets.randbelow(1000000):06d}'
        subject = 'reset' if reset else str(user['user_id'])
        hashed = credential_hash(self.settings.hmac_key, kind, subject, raw)
        tx.execute(f"""INSERT INTO {table}({idcol},user_id,{hashcol},key_id,delivery_ciphertext,expires_at)
            VALUES(%s,%s,%s,'v1',%s,%s)""", (rid, user['user_id'], hashed,
            encrypt(self.settings.encryption_key, raw), current + timedelta(minutes=30 if reset else 10)))
        self.job(tx, kind, rid, f'{kind}:{rid}', {}, user=str(user['user_id']))

    def register(self, tx, req):
        b = req.body
        fields(b, ['email', 'password', 'display_name', 'consents'], ['email', 'password', 'display_name', 'consents'])
        address, name = email(b['email']), text(b['display_name'], 50)
        if not valid_password(b['password']):
            raise APIError(422, 'VALIDATION_ERROR')
        fields(b['consents'], ['SERVICE', 'TRAINING'], ['SERVICE'])
        service = consent(b['consents']['SERVICE'])
        training = consent(b['consents'].get('TRAINING', {'policy_version': 'training-v1', 'granted': False}))
        if not service['granted']:
            raise APIError(422, 'CONSENT_REQUIRED')
        self.mail_rate(tx, address, req.ip)
        tx.lock('email:' + address)
        if tx.one('SELECT user_id FROM users WHERE email=%s', (address,)):
            raise APIError(409, 'EMAIL_ALREADY_EXISTS', commit=True)
        user_id = uid()
        tx.execute("INSERT INTO users(user_id,email,password_hash,display_name) VALUES(%s,%s,%s,%s)",
            (user_id, address, password_hash(b['password']), name))
        self.save_consent(tx, 'SERVICE', service, user=user_id)
        self.save_consent(tx, 'TRAINING', training, user=user_id)
        self.queue_mail(tx, {'user_id': user_id})
        return Result(201, {'user_id': user_id, 'status': 'PENDING_VERIFICATION', 'resend_available_at': self.clock() + timedelta(seconds=60)})

    def confirm(self, tx, req):
        fields(req.body, ['email', 'code'], ['email', 'code'])
        address, code = email(req.body['email']), req.body['code']
        if not isinstance(code, str) or not re.fullmatch('[0-9]{6}', code):
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'confirm-ip', req.ip, 100, 900)
        user = tx.one('SELECT * FROM users WHERE email=%s FOR UPDATE', (address,))
        record = tx.one("""SELECT * FROM email_verifications WHERE user_id=%s
            AND consumed_at IS NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (user['user_id'],)) if user else None
        if not record or user['status'] != 'PENDING_VERIFICATION' or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'EMAIL_VERIFICATION_INVALID', commit=True)
        if record['attempt_count'] >= 5:
            raise APIError(429, 'EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED', commit=True)
        hashed = credential_hash(self.settings.hmac_key, 'EMAIL_VERIFICATION', str(user['user_id']), code)
        if not hmac.compare_digest(hashed, record['code_hash']):
            attempts = record['attempt_count'] + 1
            tx.execute('UPDATE email_verifications SET attempt_count=%s WHERE verification_id=%s', (attempts, record['verification_id']))
            if attempts == 5:
                tx.execute('UPDATE email_verifications SET delivery_ciphertext=NULL WHERE verification_id=%s', (record['verification_id'],))
            raise APIError(429 if attempts == 5 else 422, 'EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED' if attempts == 5 else 'EMAIL_VERIFICATION_INVALID', commit=True)
        tx.execute('UPDATE email_verifications SET consumed_at=%s,delivery_ciphertext=NULL WHERE verification_id=%s', (self.clock(), record['verification_id']))
        tx.execute("UPDATE users SET status='ACTIVE',email_verified_at=%s,updated_at=%s WHERE user_id=%s", (self.clock(), self.clock(), user['user_id']))
        return Result(200, {'email_verified': True, 'next_action': 'LOGIN'})

    def login(self, tx, req):
        fields(req.body, ['email', 'password'], ['email', 'password'])
        address = email(req.body['email'])
        password = req.body['password']
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'login-ip', req.ip, 100, 900, count=False)
        self.rate(tx, 'login-email', address, 10, 900, count=False)
        user = tx.one('SELECT * FROM users WHERE email=%s FOR UPDATE', (address,))
        matched = password_matches(password, user['password_hash'] if user else self.dummy_password)
        if not user or not matched:
            self.rate(tx, 'login-ip', req.ip, 100, 900)
            self.rate(tx, 'login-email', address, 10, 900)
            raise APIError(401, 'AUTH_INVALID_CREDENTIALS', commit=True)
        if user['status'] == 'SUSPENDED':
            raise APIError(403, 'ACCOUNT_SUSPENDED', {'suspension_reason': user['suspension_reason']}, commit=True)
        if not user['email_verified_at']:
            raise APIError(403, 'EMAIL_NOT_VERIFIED', commit=True)
        if user['status'] != 'ACTIVE':
            raise APIError(401, 'AUTH_INVALID_CREDENTIALS', commit=True)
        data, cookie = self.issue(tx, user=str(user['user_id']))
        return Result(201, {'user': self.user_public(tx, user), **data}, {'Set-Cookie': cookie})

    def reset_password(self, tx, req):
        fields(req.body, ['token', 'new_password'], ['token', 'new_password'])
        raw = text(req.body['token'], 200)
        if not valid_password(req.body['new_password']):
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'reset-confirm-ip', req.ip, 30, 900)
        hashed = credential_hash(self.settings.hmac_key, 'PASSWORD_RESET', 'reset', raw)
        record = tx.one('SELECT * FROM password_resets WHERE token_hash=%s', (hashed,))
        if not record or record['consumed_at'] or record['revoked_at'] or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        user = tx.one('SELECT * FROM users WHERE user_id=%s FOR UPDATE', (record['user_id'],))
        # Same user -> credential lock order as resend, avoiding reset/resend deadlocks.
        record = tx.one('SELECT * FROM password_resets WHERE token_hash=%s FOR UPDATE', (hashed,))
        if record['consumed_at'] or record['revoked_at'] or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        if user['status'] != 'ACTIVE' or not user['email_verified_at']:
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        tx.execute('UPDATE users SET password_hash=%s,updated_at=%s WHERE user_id=%s', (password_hash(req.body['new_password']), self.clock(), user['user_id']))
        tx.execute('UPDATE password_resets SET consumed_at=%s,delivery_ciphertext=NULL WHERE reset_id=%s', (self.clock(), record['reset_id']))
        self.revoke(tx, user=str(user['user_id']))
        return Result(204)

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

    def session_public(self, tx, s, p, include_code=False):
        keys = ['session_id', 'title', 'description', 'scheduled_at', 'host_display_name', 'status', 'stt_status', 'end_reason',
                'version', 'started_at', 'ended_at', 'raw_data_expires_at', 'is_eval_lecture']
        data = {k: s[k] for k in keys}
        owner = p['user_id'] and str(p['user_id']) == str(s['host_user_id'])
        data.update(my_role='HOST' if owner else 'AUDIENCE', participant_count=self.participant_count(tx, s['session_id']), capacity=50, has_summary=False)
        data['my_consents'] = {}
        participant = self.participant(tx, s['session_id'], p)
        for purpose in ('SERVICE', 'TRAINING', 'STT'):
            c = self.effective(tx, purpose, user=p['user_id'], participant=participant['participant_id'] if participant else None, sid=s['session_id'])
            data['my_consents'][purpose] = {'granted': bool(c['granted']), 'policy_version': c['policy_version']} if c else None
        if owner or include_code:
            # 7.3.1 Session: join_code · join_url · terms only for the HOST.
            data.update(join_code=s['join_code'], join_url=self.settings.origin + '/join?code=' + s['join_code'],
                        terms=list(obj(s['terms']) or []))
        return data

    def participant_count(self, tx, sid):
        return tx.one("SELECT count(*) AS n FROM participants WHERE session_id=%s AND status='ACTIVE'", (sid,))['n']

    def code(self, tx):
        # Serialize allocation across current and immediately revoked code namespaces.
        tx.lock('join-code-allocation')
        for _ in range(100):
            code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(5))
            if not tx.one('SELECT session_id FROM lecture_sessions WHERE join_code=%s OR previous_join_code=%s', (code, code)):
                return code
        raise APIError(503, 'SERVICE_UNAVAILABLE')

    def version(self, req, lecture):
        if req.headers.get('if-match') != f'"{lecture["version"]}"':
            raise APIError(409, 'VERSION_CONFLICT')

    def create_lecture(self, tx, req, p):
        user_id = self.member(p)
        fields(req.body, ['title', 'description', 'scheduled_at', 'training_consent'], ['title', 'scheduled_at'])
        title = text(req.body['title'], 100)
        description = text(req.body['description'], 500, 0) if req.body.get('description') is not None else None
        scheduled = self.parse_date(req.body['scheduled_at'])
        self.require_service(tx, user=user_id)
        name = tx.one('SELECT display_name FROM users WHERE user_id=%s', (user_id,))['display_name']
        sid, code = uid(), self.code(tx)
        tx.execute("""INSERT INTO lecture_sessions(session_id,host_user_id,host_display_name,title,description,scheduled_at,join_code,course_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (sid, user_id, name, title, description, scheduled, code, 'course-' + sid))
        if 'training_consent' in req.body:
            self.save_consent(tx, 'TRAINING', self.entrance_consent(req.body['training_consent'], 'TRAINING'), user=user_id, sid=sid)
        lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,))
        return Result(201, self.session_public(tx, lecture, p, True), {'ETag': '"1"'})

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

    def patch_lecture(self, tx, req, p, sid):
        s = self.lecture(tx, sid, p, True)
        self.version(req, s)
        b = req.body
        fields(b, ['title', 'description', 'scheduled_at', 'status', 'stt_status'])
        if not b or (b.get('status') == 'ENDED' and 'stt_status' in b):
            raise APIError(422, 'VALIDATION_ERROR')
        updates = {}
        for key, length in [('title', 100), ('description', 500)]:
            if key in b:
                updates[key] = text(b[key], length, 0 if key == 'description' else 1)
        if 'scheduled_at' in b:
            updates['scheduled_at'] = self.parse_date(b['scheduled_at'])
        if updates and s['status'] != 'DRAFT':
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if 'status' in b:
            if b['status'] not in ('LIVE', 'ENDED'):
                raise APIError(422, 'VALIDATION_ERROR')
            if (s['status'], b['status']) not in [('DRAFT', 'LIVE'), ('LIVE', 'ENDED')]:
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            if b['status'] == 'LIVE':
                self.require_service(tx, user=p['user_id'], sid=sid)
                updates.update(status='LIVE', started_at=self.clock())
                self.job(tx, 'AUTO_END', sid, 'auto-end:' + sid, {}, user=str(p['user_id']), sid=sid, when=self.clock() + timedelta(hours=12))
            else:
                self.end(tx, s)
        if 'stt_status' in b:
            if b['stt_status'] not in ('RUNNING', 'PAUSED'):
                raise APIError(422, 'VALIDATION_ERROR')
            if b.get('status', s['status']) != 'LIVE':
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            if b['stt_status'] == 'RUNNING':
                c = self.effective(tx, 'STT', user=p['user_id'], sid=sid)
                if not c or not c['granted']:
                    raise APIError(403, 'CONSENT_REQUIRED')
            updates['stt_status'] = 'CONNECTING' if b['stt_status'] == 'RUNNING' else 'PAUSED'
        if updates:
            tx.execute('UPDATE lecture_sessions SET ' + ','.join(k + '=%s' for k in updates) + ',version=version+1 WHERE session_id=%s', (*updates.values(), sid))
        result = self.session_public(tx, tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,)), p)
        self.event(tx, 'session.updated', {'session_id': sid, 'status': result['status'], 'version': result['version']}, sid)
        return Result(200, result, {'ETag': f'"{result["version"]}"'})

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
            elif '.' in marker:
                candidate, signature = marker.split('.', 1)
                if hmac.compare_digest(signature, credential_hash(self.settings.hmac_key, 'guest-retry', sid, candidate)):
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
            tx.execute("""INSERT INTO participants(participant_id,session_id,user_id,kind,nickname,archived)
                VALUES(%s,%s,%s,%s,%s,%s)""", (pid, sid, user_id, 'MEMBER' if user_id else 'GUEST', nickname, bool(user_id)))
        if not user_id:
            self.save_consent(tx, 'SERVICE', self.entrance_consent(b['service_consent'], 'SERVICE'), participant=pid, sid=sid)
        if not user_id or 'training_consent' in b:
            self.save_consent(tx, 'TRAINING', self.entrance_consent(b.get('training_consent', False), 'TRAINING'), user=user_id, participant=None if user_id else pid, sid=sid)
        headers = {}
        data = {'participant_id': pid, 'session_id': sid, 'kind': 'MEMBER' if user_id else 'GUEST', 'expires_at': self.clock() + timedelta(hours=24)}
        if not user_id:
            # Participant must commit before auth can verify it in a separate transaction.
            # execute() invokes the exchange after the participant transaction commits.
            data['_guest_exchange'] = True
        if not existing:
            self.event(tx, 'session.updated', {'participant_count': self.participant_count(tx, sid)}, sid)
        return Result(200 if existing else 201, data, headers)

    def route(self, tx, req, mode):
        path = req.path.removeprefix('/api/v1')
        method, b = req.method, req.body
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
        if path.startswith('/join-codes/') and method == 'GET':
            s = self.join_lookup(tx, path.split('/')[-1], req.ip)
            p = self.entry_principal(tx, req)
            data = {k: s[k] for k in ['title', 'host_display_name', 'scheduled_at', 'description', 'status']}
            count = self.participant_count(tx, s['session_id'])
            data.update(participant_count=count, capacity=50, is_full=count >= 50, already_joined=bool(p and self.participant(tx, s['session_id'], p)))
            return Result(200, data)
        p = self.entry_principal(tx, req) if path == '/participations' else self.principal(tx, req)
        if method not in ('GET', 'HEAD') and p and path != '/participations':
            self.csrf(req, p)
        if path == '/participations' and method == 'POST':
            return self.join(tx, req, p)
        if path == '/sessions' and method == 'POST':
            return self.create_lecture(tx, req, p)
        if path == '/me/consents' and method == 'POST':
            return self.change_consent(tx, req, p)
        match = re.fullmatch(r'/sessions/([^/]+)(.*)', path)
        if match:
            sid, tail = uuid_value(match[1]), match[2]
            if not tail and method == 'GET':
                s = self.lecture(tx, sid, p)
                return Result(200, self.session_public(tx, s, p), {'ETag': f'"{s["version"]}"'})
            if not tail and method == 'PATCH':
                return self.patch_lecture(tx, req, p, sid)
            if tail == '/join-links' and method == 'POST':
                fields(b, [])
                s = self.lecture(tx, sid, p, True)
                self.version(req, s)
                if s['status'] not in ('DRAFT', 'LIVE'):
                    raise APIError(409, 'SESSION_STATE_CONFLICT')
                code = self.code(tx)
                tx.execute('UPDATE lecture_sessions SET previous_join_code=join_code,join_code=%s,version=version+1 WHERE session_id=%s', (code, sid))
                self.event(tx, 'session.updated', {'version': s['version'] + 1}, sid)
                return Result(201, {'join_code': code, 'join_url': self.settings.origin + '/join?code=' + code, 'version': s['version'] + 1}, {'ETag': f'"{s["version"] + 1}"'})
            if tail == '/consents' and method == 'POST':
                return self.change_consent(tx, req, p, sid)
            if tail == '/documents' and method == 'POST':
                return self.upload(tx, req, p, sid)
            if tail == '/documents' and method == 'GET':
                self.lecture(tx, sid, p, True)
                rows = tx.all('SELECT document_id,file_name,status,stage,page_count,chunk_count,error_code FROM documents WHERE session_id=%s ORDER BY created_at,document_id', (sid,))
                for r in rows:
                    r['retryable'] = self.document_retryable(r)
                return Result(200, {'items': rows, 'next_cursor': None, 'total_count': len(rows)})
            retry = re.fullmatch(r'/documents/([^/]+)/retries', tail)
            if retry and method == 'POST':
                return self.retry_document(tx, req, p, sid, uuid_value(retry[1]))
            if tail == '/terms' and method == 'PUT':
                return self.save_terms(tx, req, p, sid)
        if path == '/sessions' and method == 'GET':
            return self.list_lectures(tx, req, p)
        if path.startswith('/jobs/') and method == 'GET':
            jid = uuid_value(path.split('/')[-1])
            job = tx.one('SELECT * FROM jobs WHERE job_id=%s', (jid,))
            if not job or not p['user_id'] or str(job['requested_by']) != str(p['user_id']):
                raise APIError(404, 'RESOURCE_NOT_FOUND')
            # 7.3.1 Job. result never carries document text or credentials (worker stores counts only).
            return Result(200, {k: job[k] for k in ['job_id', 'job_type', 'status', 'attempts', 'error_code', 'finished_at']}
                          | {'resource_id': job['target_id'], 'result': obj(job['result']) if job.get('result') is not None else None})
        raise APIError(404, 'RESOURCE_NOT_FOUND')

    # ---- API-14 내 강연 · 보관함 강연 목록 -------------------------------------------------
    def list_lectures(self, tx, req, p):
        import base64
        import json as _json
        user_id = self.member(p)
        q = req.query
        if set(q) - {'scope', 'status', 'q', 'cursor', 'limit'} or q.get('scope') not in ('hosted', 'joined'):
            raise APIError(422, 'VALIDATION_ERROR')
        scope = q['scope']
        try:
            limit = int(q.get('limit', 20))
        except ValueError:
            raise APIError(422, 'VALIDATION_ERROR') from None
        if not 1 <= limit <= 100:
            raise APIError(422, 'VALIDATION_ERROR')
        status = q.get('status')
        if status is not None and (scope != 'hosted' or status not in ('DRAFT', 'LIVE', 'ENDED')):
            raise APIError(422, 'VALIDATION_ERROR')
        search = text(q['q'], 50) if 'q' in q else None
        if search is not None and scope != 'joined':
            raise APIError(422, 'VALIDATION_ERROR')
        after = None
        if q.get('cursor'):
            try:
                after = _json.loads(base64.urlsafe_b64decode(q['cursor'].encode()))
                after = (dt(after['t']), uuid_value(after['id']))
            except (ValueError, KeyError, TypeError):
                raise APIError(422, 'VALIDATION_ERROR') from None
        # Newest first by scheduled time; (scheduled_at, session_id) keeps the cursor stable.
        if scope == 'hosted':
            where, args = ['s.host_user_id=%s', 's.host_deleted_at IS NULL', "s.status IN ('DRAFT','LIVE','ENDED')"], [user_id]
            if status:
                where.append('s.status=%s')
                args.append(status)
            base = 'FROM lecture_sessions s WHERE ' + ' AND '.join(where)
        else:
            where = ['a.user_id=%s', "a.kind='MEMBER'", 'a.archived=true', "s.status<>'DELETING'"]
            args = [user_id]
            if search:
                # '!' as LIKE escape: same meaning in PostgreSQL and the SQLite test harness.
                where.append("(lower(s.title) LIKE %s ESCAPE '!' OR lower(s.host_display_name) LIKE %s ESCAPE '!')")
                needle = '%' + search.lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
                args += [needle, needle]
            base = 'FROM lecture_sessions s JOIN participants a ON a.session_id=s.session_id WHERE ' + ' AND '.join(where)
        total = tx.one('SELECT count(*) AS n ' + base, tuple(args))['n']
        page_where, page_args = '', list(args)
        if after:
            page_where = ' AND (s.scheduled_at<%s OR (s.scheduled_at=%s AND s.session_id<%s))'
            page_args += [after[0], after[0], after[1]]
        rows = tx.all('SELECT s.* ' + base + page_where + ' ORDER BY s.scheduled_at DESC, s.session_id DESC LIMIT %s',
                      tuple(page_args + [limit + 1]))
        items = []
        for s in rows[:limit]:
            data = self.session_public(tx, s, p)
            if scope == 'joined':
                data['my_question_count'] = tx.one("""SELECT count(*) AS n FROM questions q JOIN participants a
                    ON a.participant_id=q.participant_id WHERE q.session_id=%s AND a.user_id=%s AND q.deleted_at IS NULL""",
                    (s['session_id'], user_id))['n']
            items.append(data)
        cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            cursor = base64.urlsafe_b64encode(_json.dumps({'t': str(last['scheduled_at']), 'id': str(last['session_id'])}).encode()).decode()
        return Result(200, {'items': items, 'next_cursor': cursor, 'total_count': total})

    # ---- API-25 전문용어 사전 저장 ------------------------------------------------------
    TERMS_MAX, TERM_LENGTH = 100, 40

    def normalize_terms(self, values):
        """Trim, drop case-insensitive duplicates (first spelling wins), keep order."""
        seen, result = set(), []
        for value in values:
            term = ' '.join(value.split())
            if term.casefold() not in seen:
                seen.add(term.casefold())
                result.append(term)
        return result

    def save_terms(self, tx, req, p, sid):
        s = self.lecture(tx, sid, p, True)
        self.version(req, s)
        fields(req.body, ['terms'], ['terms'])
        values = req.body['terms']
        if not isinstance(values, list) or any(not isinstance(v, str) or not 1 <= len(' '.join(v.split())) <= self.TERM_LENGTH for v in values):
            raise APIError(422, 'VALIDATION_ERROR')
        terms = self.normalize_terms(values)
        if len(terms) > self.TERMS_MAX:
            raise APIError(422, 'VALIDATION_ERROR')
        if s['status'] != 'DRAFT':
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        # 6.2.3: a term the host removes is remembered so document extraction never adds it again.
        kept = {t.casefold() for t in terms}
        removed = self.normalize_terms([t for t in (obj(s['removed_terms']) or []) + (obj(s['terms']) or []) if t.casefold() not in kept])
        tx.execute('UPDATE lecture_sessions SET terms=%s,removed_terms=%s,version=version+1 WHERE session_id=%s',
                   (dumps(terms), dumps(removed), sid))
        version = s['version'] + 1
        self.event(tx, 'session.updated', {'session_id': sid, 'status': s['status'], 'version': version}, sid)
        return Result(200, {'terms': terms, 'version': version}, {'ETag': f'"{version}"'})

    # ---- API-23 교안 처리 재시도 ------------------------------------------------------
    PARSE_FAILED = 'DOCUMENT_PARSE_FAILED'

    def document_retryable(self, document):
        return document['status'] == 'FAILED' and document['error_code'] != self.PARSE_FAILED

    def retry_document(self, tx, req, p, sid, did):
        import hashlib
        s = self.lecture(tx, sid, p, True)
        fields(req.body, [])
        key = uuid_value(req.headers.get('idempotency-key'))
        job_key = 'ingest-retry:' + str(p['user_id']) + ':' + did + ':' + key
        fingerprint = hashlib.sha256(('retry:' + sid + ':' + did).encode()).hexdigest()
        tx.lock(job_key)
        prior = tx.one('SELECT * FROM jobs WHERE idempotency_key=%s', (job_key,))
        if prior:
            if obj(prior['payload']).get('fingerprint') != fingerprint:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            return Result(202, {'resource_id': prior['target_id'], 'job_id': prior['job_id'], 'status': prior['status']}, {'Location': '/api/v1/jobs/' + str(prior['job_id'])})
        document = tx.one('SELECT * FROM documents WHERE session_id=%s AND document_id=%s FOR UPDATE', (sid, did))
        if not document or document['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if s['status'] not in ('DRAFT', 'LIVE'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if not self.document_retryable(document):
            raise APIError(409, 'DOCUMENT_NOT_RETRYABLE')
        # Same S3 original, from the first stage: drop earlier chunks/vectors and any job still waiting (e.g. budget HELD).
        tx.execute("UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE job_type='INGEST' AND target_id=%s AND status IN ('QUEUED','HELD')",
                   (self.clock(), did))
        tx.execute("DELETE FROM knowledge_chunks WHERE session_id=%s AND document_id=%s", (sid, did))
        tx.execute("""UPDATE documents SET status='PENDING',stage='UPLOADED',error_code=NULL,page_count=NULL,chunk_count=NULL,
            terms=%s WHERE document_id=%s""", (dumps([]), did))
        jid = self.job(tx, 'INGEST', did, job_key, {'fingerprint': fingerprint, 'retry': True}, user=str(p['user_id']), sid=sid)
        self.event(tx, 'document.updated', {'document_id': did, 'status': 'PENDING', 'stage': 'UPLOADED'}, sid)
        return Result(202, {'resource_id': did, 'job_id': jid, 'status': 'QUEUED'}, {'Location': '/api/v1/jobs/' + jid})

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

    def change_consent(self, tx, req, p, sid=None):
        b = req.body
        fields(b, ['purpose', 'policy_version', 'granted'], ['purpose', 'policy_version', 'granted'])
        purpose = b['purpose']
        if purpose not in (('SERVICE', 'TRAINING', 'STT') if sid else ('SERVICE', 'TRAINING')):
            raise APIError(422, 'VALIDATION_ERROR')
        value = consent({k: b[k] for k in ['policy_version', 'granted']})
        user, participant = p['user_id'], None
        if sid:
            s = self.lecture(tx, sid, p)
            owner = user and str(user) == str(s['host_user_id'])
            if purpose == 'STT' and not owner:
                raise APIError(403, 'FORBIDDEN')
            if not user:
                participant = p['participant_id']
        else:
            user = self.member(p)
            if purpose == 'SERVICE' and not value['granted']:
                raise APIError(422, 'VALIDATION_ERROR', {'action': 'ACCOUNT_DELETION'})
        data = self.save_consent(tx, purpose, value, user=user, participant=participant, sid=sid)
        if not value['granted']:
            if purpose == 'TRAINING':
                target = str(user or participant)
                jid = self.job(tx, 'CLEANUP', target, 'consent:' + data['consent_id'],
                    {'scope': 'TRAINING', 'user_id': str(user) if user else None, 'participant_id': str(participant) if participant else None, 'session_id': sid}, user=str(user) if user else None, sid=sid)
                data['deletion_job_id'] = jid
            if sid and purpose == 'STT':
                tx.execute("UPDATE lecture_sessions SET stt_status='STOPPED',version=version+1 WHERE session_id=%s", (sid,))
                self.event(tx, 'session.updated', {'stt_status': 'STOPPED'}, sid)
            if sid and purpose == 'SERVICE':
                if owner:
                    if s['status'] == 'LIVE':
                        self.end(tx, s)
                    else:
                        raise APIError(409, 'SESSION_STATE_CONFLICT')
                else:
                    g = self.participant(tx, sid, p)
                    tx.execute("UPDATE participants SET status='REVOKED' WHERE participant_id=%s", (g['participant_id'],))
                    if not user:
                        self.job(tx, 'AUTH_REVOKE', str(g['participant_id']), 'consent-revoke:' + data['consent_id'], {'participant_id': str(g['participant_id']), 'reason': 'CONSENT_WITHDRAWN'}, sid=sid)
                self.event(tx, 'session.updated', {'participant_count': self.participant_count(tx, sid)}, sid)
        return Result(201, data)

    def upload(self, tx, req, p, sid):
        from .retrieval import validate_document
        s = self.lecture(tx, sid, p, True)
        if s['status'] not in ('DRAFT', 'LIVE'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if not req.file:
            raise APIError(422, 'VALIDATION_ERROR')
        filename, media, content = req.file
        validate_document(filename, media, content, self.settings)
        key = uuid_value(req.headers.get('idempotency-key'))
        job_key = 'ingest:' + str(p['user_id']) + ':' + sid + ':' + key
        import hashlib
        fingerprint = hashlib.sha256(content + filename.encode() + media.encode()).hexdigest()
        tx.lock(job_key)
        prior = tx.one('SELECT * FROM jobs WHERE idempotency_key=%s', (job_key,))
        if prior:
            if obj(prior['payload'])['fingerprint'] != fingerprint:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            return Result(202, {'resource_id': prior['target_id'], 'job_id': prior['job_id'], 'status': prior['status']}, {'Location': '/api/v1/jobs/' + str(prior['job_id'])})
        did = uid()
        object_key = sid + '/' + did
        req._staged_object = object_key
        self.storage.put(object_key, content, media)
        # Object keys are generated; the user filename never becomes a filesystem path.
        tx.execute("""INSERT INTO documents(document_id,session_id,file_name,object_key,media_type,size_bytes,stage)
            VALUES(%s,%s,%s,%s,%s,%s,'UPLOADED')""", (did, sid, filename, object_key, media, len(content)))
        jid = self.job(tx, 'INGEST', did, job_key, {'fingerprint': fingerprint}, user=str(p['user_id']), sid=sid)
        self.event(tx, 'document.updated', {'document_id': did, 'status': 'PENDING', 'stage': 'UPLOADED'}, sid)
        return Result(202, {'resource_id': did, 'job_id': jid, 'status': 'QUEUED'}, {'Location': '/api/v1/jobs/' + jid})
