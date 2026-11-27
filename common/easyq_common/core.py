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
            retry_cookie = self.join_retry_cookie(pid, result.data['session_id'], result.data.pop('_retry_expires'))
            try:
                exchange = self.guest_client.issue(pid, uid())
            except Exception:
                raise APIError(503, 'AUTH_SERVICE_UNAVAILABLE', headers={'Set-Cookie': retry_cookie}) from None
            result.data.update(csrf_token=exchange['csrf_token'], expires_at=exchange['expires_at'])
            result.headers['Set-Cookie'] = [exchange['set_cookie'], retry_cookie]
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

    def join_retry_cookie(self, pid, sid, expires_at):
        """7.1.1 easyq_join_retry: participant_id · session_id · expiry (joined_at + 24 h, the guest auth period) signed
        with the server key. Only finds the same browser's guest participation again; never authenticates, never stored."""
        expires = int(dt(expires_at).timestamp())
        max_age = max(0, expires - int(self.clock().timestamp()))
        marker = f'{pid}.{expires}.' + credential_hash(self.settings.hmac_key, 'guest-retry', sid, f'{pid}.{expires}')
        return ('easyq_join_retry=' + marker + f'; Path=/api/v1/participations; HttpOnly; SameSite=Lax; Max-Age={max_age}'
                + ('; Secure' if self.settings.cookie_secure else ''))

    def join_retry_participant(self, marker, sid):
        """The participant_id in a valid, unexpired easyq_join_retry marker for this lecture, else None."""
        try:
            pid, expires, signature = marker.split('.', 2)
            expected = credential_hash(self.settings.hmac_key, 'guest-retry', sid, f'{pid}.{expires}')
            if hmac.compare_digest(signature, expected) and int(expires) > self.clock().timestamp():
                return pid
        except ValueError:
            pass
        return None

    def event(self, tx, event_type, payload, sid=None):
        # The number comes from the sequence first: easyq_auth has INSERT but no SELECT on ws_events (v1.4 grants), and
        # INSERT ... RETURNING event_id would need SELECT (review D-2). All three roles have USAGE on the sequence.
        event_id = tx.one("SELECT nextval('ws_events_event_id_seq') AS event_id")['event_id']
        tx.execute("""INSERT INTO ws_events(event_id,event_type,payload,session_id,expires_at) VALUES(%s,%s,%s,%s,%s)""",
                   (event_id, event_type, dumps(payload), sid, self.clock() + timedelta(hours=24)))
        tx.notify(event_id)

    def job(self, tx, kind, target, key, payload, user=None, sid=None, participant=None, when=None, client=None):
        """client = (client_request_key, request_hash) from idempotent() for async APIs (8.2.28), else None."""
        jid = uid()
        client_key, request_hash = client or (None, None)
        tx.execute("""INSERT INTO jobs(job_id,job_type,target_id,idempotency_key,client_request_key,request_hash,payload,
            requested_by,session_id,participant_id,available_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (jid, kind, target, key, client_key, request_hash, dumps(payload), user, sid, participant, when or self.clock()))
        return jid

    def idempotent(self, tx, req, subject, job_type, body):
        """7.1.2 async POST. Returns (client, prior): client = (client_request_key, request_hash) to pass to job();
        prior = the 202 Result of an earlier identical request (same subject · job type · key · body), else None.
        The same key with a different body is 409 IDEMPOTENCY_CONFLICT."""
        key = req.headers.get('idempotency-key') or ''
        if not key or len(key) > 200:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'Idempotency-Key'})
        client_key = credential_hash(self.settings.hmac_key, 'idempotency', f'{subject}:{job_type}', key)
        request_hash = digest(body if isinstance(body, str) else dumps(body))
        tx.lock('idem:' + client_key)
        prior = tx.one('SELECT * FROM jobs WHERE client_request_key=%s', (client_key,))
        if prior:
            if prior['request_hash'] != request_hash:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            return (client_key, request_hash), self.accepted(prior['target_id'], prior['job_id'], prior['status'])
        return (client_key, request_hash), None

    @staticmethod
    def accepted(resource_id, job_id, status='QUEUED'):
        return Result(202, {'resource_id': str(resource_id), 'job_id': str(job_id), 'status': status},
                      {'Location': '/api/v1/jobs/' + str(job_id)})

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
        if not lecture or lecture['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        # host_deleted_at hides the lecture from its host only; archive members keep the lecture record (6.2.11-3).
        owner = principal['user_id'] and str(lecture['host_user_id']) == str(principal['user_id']) and not lecture['host_deleted_at']
        participant = self.participant(tx, sid, principal)
        if (host and not owner) or (not host and not owner and not participant):
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        return lecture

    def participant(self, tx, sid, principal):
        column, value = ('user_id', principal['user_id']) if principal['user_id'] else ('participant_id', principal['participant_id'])
        # archived = the lecture is in the member's archive (8.2.8: default true, false after archive delete; guests false).
        return tx.one(f"SELECT * FROM participants WHERE session_id=%s AND {column}=%s AND status='ACTIVE' AND (kind='GUEST' OR archived=true)", (sid, value))

    def participant_count(self, tx, sid):
        """API-19 capacity (v1.4): ACTIVE members, plus ACTIVE guests that still hold an unexpired, unrevoked guest
        session or joined less than 10 minutes ago (the session exchange may still be in flight). Host excluded."""
        moment = self.clock()
        return tx.one("""SELECT count(*) AS n FROM participants p WHERE p.session_id=%s AND p.status='ACTIVE' AND
            (p.kind='MEMBER' OR p.joined_at>%s OR EXISTS (SELECT 1 FROM auth_session_view a WHERE a.participant_id=p.participant_id
             AND a.revoked_at IS NULL AND a.expires_at>%s))""", (sid, moment - timedelta(minutes=10), moment))['n']

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

    def lecture_statistics(self, tx, sid):
        """7.3.1 Statistics for one lecture (API-39). FINALIZE stores it once in lecture_sessions.statistics (6.2.9);
        counts every question row (deleted or purged ones too) so later clean-up never changes the numbers.
        ai_answer_count = ACTIVE answers, retracted_count = RETRACTED answers; escalated / completed = current status."""
        q = tx.one("""SELECT count(*) AS total, sum(CASE WHEN status='ESCALATED' THEN 1 ELSE 0 END) AS escalated,
            sum(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed, sum(vote_count) AS votes,
            sum(CASE WHEN document_id IS NULL OR page_no IS NULL THEN 1 ELSE 0 END) AS general
            FROM questions WHERE session_id=%s""", (sid,))
        a = tx.one("""SELECT sum(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) AS active,
            sum(CASE WHEN status='RETRACTED' THEN 1 ELSE 0 END) AS retracted FROM answers WHERE session_id=%s""", (sid,))
        kinds = {r['kind']: r['n'] for r in tx.all('SELECT kind,count(*) AS n FROM participants WHERE session_id=%s GROUP BY kind', (sid,))}
        pages = tx.all("""SELECT q.document_id,d.file_name,q.page_no,count(*) AS n FROM questions q
            JOIN documents d ON d.session_id=q.session_id AND d.document_id=q.document_id
            WHERE q.session_id=%s AND q.page_no IS NOT NULL GROUP BY q.document_id,d.file_name,q.page_no
            ORDER BY d.file_name,q.page_no""", (sid,))
        total, active = int(q['total'] or 0), int(a['active'] or 0)
        return {'question_count': total, 'ai_answer_count': active, 'escalated_count': int(q['escalated'] or 0),
                'completed_count': int(q['completed'] or 0), 'retracted_count': int(a['retracted'] or 0),
                'vote_total': int(q['votes'] or 0),
                'participant_count': {'member': int(kinds.get('MEMBER', 0)), 'guest': int(kinds.get('GUEST', 0))},
                'ai_answer_share': round(active / total, 4) if total else None,
                'questions_by_page': [{'document_id': str(r['document_id']), 'file_name': r['file_name'], 'page_no': r['page_no'],
                                       'count': int(r['n'])} for r in pages],
                'general_question_count': int(q['general'] or 0), 'computed_at': self.clock()}

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
