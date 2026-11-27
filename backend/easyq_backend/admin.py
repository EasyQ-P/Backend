"""Admin users, lectures, metrics, budget (API-45~48 · 64 · 65).

Every admin API checks the ADMIN role of the current account (require_admin). State changes write audit_events in the
same transaction (7.3.7, 8.2.30); a failed audit insert rolls the change back. Responses never include password or
session data. The backend DB role may only update users.status/suspension_reason/updated_at, so all users reads list
their columns explicitly (no SELECT *).
"""
import base64
from datetime import timedelta, timezone
from decimal import Decimal
import json

from easyq_common.contracts import APIError, Result, dt, dumps, fields, obj, text, uid, uuid_value
from easyq_common.security import credential_hash

SEOUL = timezone(timedelta(hours=9))   # Asia/Seoul has no DST; API-64 counts the budget month from the 1st 00:00 KST
USER_COLUMNS = 'user_id,email,display_name,role,status,email_verified_at,suspension_reason,created_at'


# ---- shared helpers (also imported by training.py) --------------------------------------------------------------
def num(value):
    """NUMERIC columns come back as Decimal from PostgreSQL; JSON responses use numbers."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def flag(value):
    return None if value is None else bool(value)


def jsonb(value, default=None):
    value = obj(value) if value is not None else None
    return default if value is None else value


def limit_of(q):
    try:
        limit = int(q.get('limit', 20))
    except (TypeError, ValueError):
        raise APIError(422, 'VALIDATION_ERROR', {'field': 'limit'}) from None
    if not 1 <= limit <= 100:
        raise APIError(422, 'VALIDATION_ERROR', {'field': 'limit'})
    return limit


def encode_cursor(value):
    return base64.urlsafe_b64encode(dumps(value).encode()).decode()


def decode_cursor(q):
    if not q.get('cursor'):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(q['cursor'].encode()))
    except (ValueError, TypeError):
        raise APIError(422, 'VALIDATION_ERROR', {'field': 'cursor'}) from None


def query_keys(q, allowed):
    if set(q) - set(allowed):
        raise APIError(422, 'VALIDATION_ERROR', {'field': sorted(set(q) - set(allowed))[0]})


def keyset(q, column, id_column):
    """(sql, args) for a 'created_at DESC, id DESC' cursor."""
    after = decode_cursor(q)
    if not after:
        return '', ()
    try:
        moment, ident = dt(after['t']), uuid_value(after['id'])
    except (KeyError, TypeError, ValueError, APIError):
        raise APIError(422, 'VALIDATION_ERROR', {'field': 'cursor'}) from None
    return f' AND ({column}<%s OR ({column}=%s AND {id_column}<%s))', (moment, moment, ident)


def page_of(rows, limit, column, id_column):
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = encode_cursor({'t': str(dt(rows[-1][column]).isoformat()), 'id': str(rows[-1][id_column])}) if more else None
    return rows, cursor


def like(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def percentile(values, share):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    rank = max(1, -(-len(values) * share // 1))   # nearest rank
    return values[int(rank) - 1]


def ms_between(start, end):
    if start is None or end is None:
        return None
    return int((dt(end) - dt(start)).total_seconds() * 1000)


class AdminMixin:
    ROUTES = [
        ('GET', r'/admin/users', 'admin_users'),
        ('PATCH', r'/admin/users/(?P<user_id>[^/]+)', 'admin_patch_user'),
        ('GET', r'/admin/sessions', 'admin_sessions'),
        ('PATCH', r'/admin/sessions/(?P<sid>[^/]+)', 'admin_patch_session'),
        ('GET', r'/admin/metrics', 'admin_metrics'),
        ('PUT', r'/admin/budget-settings', 'admin_budget_settings'),
    ]

    # ---- audit (8.2.30) ------------------------------------------------------------------------------------------
    def audit(self, tx, req, actor, action, target_type, target_id, changes, reason=None):
        """target_ref is pseudonymized for people and lectures (HMAC); version names and service IDs stay readable."""
        ref = str(target_id)
        if target_type in ('USER', 'SESSION'):
            ref = target_type.lower() + ':' + credential_hash(self.settings.hmac_key, 'audit', target_type, ref)[:32]
        if not getattr(req, '_audit_request_id', None):
            req._audit_request_id = uid()   # one trace ID for all audit rows of one request
        tx.execute("""INSERT INTO audit_events(audit_event_id,actor_user_id,action,target_type,target_ref,changes,reason,request_id,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uid(), actor, action, target_type, ref[:100], dumps(changes), reason, req._audit_request_id, self.clock()))

    # ---- API-45 · 46 users ---------------------------------------------------------------------------------------
    def admin_user_view(self, tx, u):
        data = {k: u[k] for k in USER_COLUMNS.split(',')}
        data['user_id'] = str(u['user_id'])
        data['consents'] = {}
        for purpose in ('SERVICE', 'TRAINING'):
            c = self.effective(tx, purpose, user=u['user_id'])
            data['consents'][purpose] = {'policy_version': c['policy_version'], 'granted': bool(c['granted'])} if c else None
        uid_ = u['user_id']
        data['joined_count'] = tx.one('SELECT count(DISTINCT session_id) AS n FROM participants WHERE user_id=%s', (uid_,))['n']
        data['hosted_count'] = tx.one('SELECT count(*) AS n FROM lecture_sessions WHERE host_user_id=%s', (uid_,))['n']
        data['question_count'] = tx.one("""SELECT count(*) AS n FROM questions q JOIN participants p ON p.participant_id=q.participant_id
            WHERE p.user_id=%s AND q.deleted_at IS NULL""", (uid_,))['n']
        return data

    def admin_users(self, tx, req, p):
        self.require_admin(p)
        q = req.query
        query_keys(q, ['status', 'q', 'cursor', 'limit'])
        limit = limit_of(q)
        where, args = ' WHERE 1=1', []
        if 'status' in q:
            if q['status'] not in ('PENDING_VERIFICATION', 'ACTIVE', 'SUSPENDED', 'DELETING'):
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'status'})
            where += ' AND status=%s'
            args.append(q['status'])
        if 'q' in q:
            pattern = like(text(q['q'], 50).lower())
            where += " AND (lower(display_name) LIKE %s ESCAPE '\\' OR email LIKE %s ESCAPE '\\')"
            args += [pattern, pattern]
        total = tx.one('SELECT count(*) AS n FROM users' + where, tuple(args))['n']
        cond, cargs = keyset(q, 'created_at', 'user_id')
        rows = tx.all(f'SELECT {USER_COLUMNS} FROM users' + where + cond + ' ORDER BY created_at DESC,user_id DESC LIMIT %s',
                      (*args, *cargs, limit + 1))
        rows, cursor = page_of(rows, limit, 'created_at', 'user_id')
        return Result(200, {'items': [self.admin_user_view(tx, r) for r in rows], 'next_cursor': cursor, 'total_count': total})

    def admin_patch_user(self, tx, req, p, user_id):
        admin = self.require_admin(p)
        fields(req.body, ['status', 'reason'], ['status', 'reason'])
        status = req.body['status']
        if status not in ('ACTIVE', 'SUSPENDED'):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'status'})
        reason = text(req.body['reason'], 500)
        tx.lock('admin-user-status')   # serializes the last-active-admin check
        u = tx.one(f'SELECT {USER_COLUMNS} FROM users WHERE user_id=%s FOR UPDATE', (user_id,))
        if not u:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if status == 'SUSPENDED':
            self.suspend_user(tx, req, admin, u, reason)
        elif u['status'] == 'SUSPENDED':
            self.restore_user(tx, req, admin, u, reason)
        elif u['status'] != 'ACTIVE':
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        u = tx.one(f'SELECT {USER_COLUMNS} FROM users WHERE user_id=%s', (user_id,))
        return Result(200, self.admin_user_view(tx, u))

    def suspend_user(self, tx, req, admin, u, reason):
        # Only ACTIVE (or already SUSPENDED, reason update) accounts: a PENDING_VERIFICATION account could not be
        # restored without breaking the users CHECK (review D-6), DELETING is a withdrawal in progress.
        if u['status'] not in ('ACTIVE', 'SUSPENDED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        if u['role'] == 'ADMIN' and u['status'] == 'ACTIVE':
            others = tx.one("SELECT count(*) AS n FROM users WHERE role='ADMIN' AND status='ACTIVE' AND user_id<>%s", (u['user_id'],))['n']
            if not others:
                raise APIError(409, 'SESSION_STATE_CONFLICT', {'reason': 'LAST_ADMIN'})
        target = str(u['user_id'])
        tx.execute("UPDATE users SET status='SUSPENDED',suspension_reason=%s,updated_at=%s WHERE user_id=%s", (reason, self.clock(), target))
        self.audit(tx, req, admin, 'USER_SUSPENDED', 'USER', target, {'status': [u['status'], 'SUSPENDED']}, reason)
        # The status change blocks access at once (principal()); the worker revokes the auth sessions with INT-02,
        # which also closes WebSockets through session.revoked (6.3.1, 7.3.11).
        self.job(tx, 'AUTH_REVOKE', target, f'suspend-revoke:{target}:{uid()}', {'user_id': target, 'reason': 'ACCOUNT_SUSPENDED'}, user=admin)
        for s in tx.all("SELECT * FROM lecture_sessions WHERE host_user_id=%s AND status='LIVE' FOR UPDATE", (target,)):
            self.force_end(tx, req, admin, s, reason)

    def restore_user(self, tx, req, admin, u, reason):
        target = str(u['user_id'])
        # 7.3.11: revoke every remaining session first, then ACTIVE (a re-login is required). If auth is down the
        # account stays SUSPENDED and the admin retries (review P-9).
        try:
            self.guest_client.revoke({'user_id': target, 'reason': 'ACCOUNT_RESTORED', 'request_id': uid()})
        except Exception:
            raise APIError(503, 'AUTH_SERVICE_UNAVAILABLE') from None
        # Revocation jobs left from the suspension must not hit sessions created after the restore.
        tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s,lease_until=NULL WHERE job_type='AUTH_REVOKE'
            AND status IN ('QUEUED','HELD') AND idempotency_key LIKE %s""", (self.clock(), f'suspend-revoke:{target}:%'))
        tx.execute("UPDATE users SET status='ACTIVE',suspension_reason=NULL,updated_at=%s WHERE user_id=%s", (self.clock(), target))
        self.audit(tx, req, admin, 'USER_RESTORED', 'USER', target, {'status': ['SUSPENDED', 'ACTIVE']}, reason)

    def force_end(self, tx, req, admin, s, reason):
        """API-48 / API-46: the 6.2.9 end flow with end_reason = ADMIN."""
        sid = str(s['session_id'])
        self.end(tx, s, 'ADMIN')
        self.event(tx, 'session.updated', {'session_id': sid, 'status': 'ENDED', 'end_reason': 'ADMIN', 'version': s['version'] + 1}, sid)
        self.audit(tx, req, admin, 'SESSION_FORCE_ENDED', 'SESSION', sid, {'status': ['LIVE', 'ENDED'], 'end_reason': 'ADMIN'}, reason)

    # ---- API-47 · 48 lectures ------------------------------------------------------------------------------------
    def admin_sessions(self, tx, req, p):
        self.require_admin(p)
        q = req.query
        query_keys(q, ['status', 'from', 'to', 'cursor', 'limit'])
        limit = limit_of(q)
        where, args = ' WHERE 1=1', []
        if 'status' in q:
            if q['status'] not in ('DRAFT', 'LIVE', 'ENDED', 'PURGED', 'DELETING'):
                raise APIError(422, 'VALIDATION_ERROR', {'field': 'status'})
            where += ' AND status=%s'
            args.append(q['status'])
        # The shown date is scheduled_at for DRAFT and started_at otherwise; from is inclusive, to exclusive.
        if 'from' in q:
            where += ' AND COALESCE(started_at,scheduled_at)>=%s'
            args.append(self.parse_date(q['from']))
        if 'to' in q:
            where += ' AND COALESCE(started_at,scheduled_at)<%s'
            args.append(self.parse_date(q['to']))
        total = tx.one('SELECT count(*) AS n FROM lecture_sessions' + where, tuple(args))['n']
        cond, cargs = keyset(q, 'created_at', 'session_id')
        rows = tx.all('SELECT * FROM lecture_sessions' + where + cond + ' ORDER BY created_at DESC,session_id DESC LIMIT %s',
                      (*args, *cargs, limit + 1))
        rows, cursor = page_of(rows, limit, 'created_at', 'session_id')
        items = []
        for s in rows:
            # API-47 lists host_user_id (the admin acts on it with API-46) and join_code; no question text (6.3.1).
            items.append({'session_id': str(s['session_id']), 'host_user_id': str(s['host_user_id']) if s['host_user_id'] else None,
                          'host_display_name': s['host_display_name'], 'title': s['title'], 'join_code': s['join_code'],
                          'status': s['status'], 'end_reason': s['end_reason'], 'is_eval_lecture': bool(s['is_eval_lecture']),
                          'scheduled_at': s['scheduled_at'], 'started_at': s['started_at'], 'ended_at': s['ended_at'],
                          'participant_count': self.participant_count(tx, s['session_id']),
                          'question_count': tx.one('SELECT count(*) AS n FROM questions WHERE session_id=%s', (s['session_id'],))['n'],
                          'created_at': s['created_at'], 'version': s['version']})
        return Result(200, {'items': items, 'next_cursor': cursor, 'total_count': total})

    def admin_patch_session(self, tx, req, p, sid):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['status', 'is_eval_lecture', 'reason'], ['reason'])
        if 'status' not in b and 'is_eval_lecture' not in b:
            raise APIError(422, 'VALIDATION_ERROR')
        if 'status' in b and b['status'] != 'ENDED':
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'status'})
        if 'is_eval_lecture' in b and b['is_eval_lecture'] is not True:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'is_eval_lecture'})   # never back to false
        reason = text(b['reason'], 500)
        s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
        if not s or s['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        self.version(req, s)
        if 'is_eval_lecture' in b and not s['is_eval_lecture']:
            if s['status'] != 'DRAFT' or 'status' in b:
                raise APIError(409, 'SESSION_STATE_CONFLICT')
            tx.execute('UPDATE lecture_sessions SET is_eval_lecture=true,version=version+1 WHERE session_id=%s', (sid,))
            self.audit(tx, req, admin, 'EVAL_LECTURE_MARKED', 'SESSION', sid, {'is_eval_lecture': [False, True]}, reason)
            self.event(tx, 'session.updated', {'session_id': sid, 'status': s['status'], 'version': s['version'] + 1}, sid)
        if 'status' in b:
            if s['status'] != 'LIVE':
                raise APIError(409, 'SESSION_STATE_CONFLICT')   # DRAFT is not force-ended (6.3.1)
            self.force_end(tx, req, admin, s, reason)
        s = tx.one('SELECT session_id,status,is_eval_lecture,version FROM lecture_sessions WHERE session_id=%s', (sid,))
        data = {'session_id': str(s['session_id']), 'status': s['status'], 'is_eval_lecture': bool(s['is_eval_lecture']), 'version': s['version']}
        return Result(200, data, {'ETag': f'"{s["version"]}"'})

    # ---- API-64 metrics (7.3.8) ----------------------------------------------------------------------------------
    def admin_metrics(self, tx, req, p):
        self.require_admin(p)
        q = req.query
        query_keys(q, ['from', 'to', 'group_by', 'session_id'])
        if 'from' not in q or 'to' not in q or q.get('group_by') not in ('day', 'session'):
            raise APIError(422, 'VALIDATION_ERROR')
        start, end = self.parse_date(q['from']), self.parse_date(q['to'])
        if end <= start or end - start > timedelta(days=90):
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'to'})
        sid = uuid_value(q['session_id']) if q.get('session_id') else None
        by_day = q['group_by'] == 'day'
        extra, extra_args = (' AND q.session_id=%s', (sid,)) if sid else ('', ())

        def key_of(moment, session_id):
            if by_day:
                return dt(moment).astimezone(SEOUL).date().isoformat()
            return str(session_id) if session_id else None

        groups = {}

        def group(key):
            if key not in groups:
                groups[key] = {'questions': [], 'retracted': 0, 'retraction_reasons': {}, 'ratings': {'HELPFUL': 0, 'UNHELPFUL': 0},
                               'ingest': [], 'failed_jobs': 0, 'external_calls': 0, 'cost': {}}
            return groups[key]

        questions = tx.all(f"""SELECT q.question_id,q.session_id,q.status,q.decision_snapshot,q.created_at,q.first_token_at,q.finalized_at,
            (SELECT count(*) FROM answers a WHERE a.question_id=q.question_id AND a.status='ACTIVE') AS active_answers
            FROM questions q WHERE q.created_at>=%s AND q.created_at<%s{extra}""", (start, end, *extra_args))
        for row in questions:
            group(key_of(row['created_at'], row['session_id']))['questions'].append(row)
        for row in tx.all(f"""SELECT a.status,a.retraction_reason,q.created_at,q.session_id FROM answers a JOIN questions q ON q.question_id=a.question_id
                WHERE q.created_at>=%s AND q.created_at<%s AND a.status='RETRACTED'{extra}""", (start, end, *extra_args)):
            g = group(key_of(row['created_at'], row['session_id']))
            g['retracted'] += 1
            reason = row['retraction_reason'] or 'UNKNOWN'
            g['retraction_reasons'][reason] = g['retraction_reasons'].get(reason, 0) + 1
        for row in tx.all(f"""SELECT f.rating,q.created_at,q.session_id FROM answer_feedback f JOIN answers a ON a.answer_id=f.answer_id
                JOIN questions q ON q.question_id=a.question_id WHERE q.created_at>=%s AND q.created_at<%s{extra}""", (start, end, *extra_args)):
            g = group(key_of(row['created_at'], row['session_id']))
            if row['rating'] in g['ratings']:
                g['ratings'][row['rating']] += 1
        sess = ' AND session_id=%s' if sid else ''
        for row in tx.all(f"""SELECT session_id,captured_at,indexed_at FROM knowledge_chunks WHERE source_type='TRANSCRIPT' AND index_status='READY'
                AND captured_at>=%s AND captured_at<%s{sess}""", (start, end, *extra_args)):
            group(key_of(row['captured_at'], row['session_id']))['ingest'].append(ms_between(row['captured_at'], row['indexed_at']))
        for row in tx.all(f"SELECT session_id,finished_at FROM jobs WHERE status='FAILED' AND finished_at>=%s AND finished_at<%s{sess}",
                          (start, end, *extra_args)):
            group(key_of(row['finished_at'], row['session_id']))['failed_jobs'] += 1
        for row in tx.all(f"""SELECT session_id,created_at,currency,estimated_cost,outcome FROM api_usage WHERE created_at>=%s AND created_at<%s{sess}""",
                          (start, end, *extra_args)):
            g = group(key_of(row['created_at'], row['session_id']))
            g['external_calls'] += 1
            if row['estimated_cost'] is not None:
                g['cost'][row['currency']] = g['cost'].get(row['currency'], Decimal(0)) + Decimal(str(row['estimated_cost']))

        result = []
        for key in sorted(groups, key=lambda k: (k is None, k or '')):
            g = groups[key]
            qs = g['questions']
            snaps = [jsonb(r['decision_snapshot'], {}) for r in qs]
            count = len(qs)
            ai = sum(1 for r in qs if r['status'] == 'AI_ANSWERED' and r['active_answers'])
            esc = sum(1 for r in qs if r['status'] in ('ESCALATED', 'COMPLETED'))
            progress = [s.get('progress_ms') for s in snaps if isinstance(s.get('progress_ms'), (int, float))]
            first = [ms_between(r['created_at'], r['first_token_at']) for r in qs if r['status'] == 'AI_ANSWERED']
            final = [ms_between(r['created_at'], r['finalized_at']) for r in qs]
            item = {'date' if by_day else 'session_id': key,
                    'question_count': count, 'ai_answer_count': ai, 'escalated_count': esc,
                    'ai_answer_share': ai / count if count else None, 'escalation_rate': esc / count if count else None,
                    'progress_max_ms': max(progress) if progress else None, 'progress_p95_ms': percentile(progress, 0.95),
                    'first_token_p50_ms': percentile(first, 0.5),
                    'final_p50_ms': percentile(final, 0.5), 'final_p95_ms': percentile(final, 0.95),
                    'stt_ingest_p95_ms': percentile(g['ingest'], 0.95),
                    'fallback_count': sum(1 for s in snaps if s.get('route') == 'FALLBACK_LLM_ONLY'),
                    'second_stage_escalated_count': sum(1 for s in snaps if s.get('second_stage_escalated') is True),
                    'retracted_count': g['retracted'],
                    'feedback_counts': {'retraction_reasons': g['retraction_reasons'], 'audience': g['ratings']},
                    'failed_jobs': g['failed_jobs'], 'external_calls': g['external_calls'],
                    'estimated_cost_by_currency': {c: float(v) for c, v in g['cost'].items()}}
            result.append(item)
        return Result(200, {'period': {'from': start, 'to': end}, 'group_by': q['group_by'], 'groups': result,
                            'budget': self.budget_status(tx), 'data_as_of': self.clock()})

    def budget_status(self, tx):
        """This month's (Asia/Seoul) settled cost and reservations of the capped operations, in KRW (8.2.34)."""
        setting = tx.one("SELECT value,hard_cap_enabled FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET'")
        if not setting:
            return None
        value = jsonb(setting['value'], {})
        try:
            budget, rate = Decimal(str(value['monthly_budget_krw'])), Decimal(str(value['usd_krw_rate']))
        except (KeyError, ArithmeticError, ValueError):
            return None
        local = self.clock().astimezone(SEOUL)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        used = reserved = Decimal(0)
        # Same operations as the worker's hard cap (STT, mail and judge inference are not capped).
        for row in tx.all("""SELECT currency,estimated_cost,outcome FROM api_usage WHERE created_at>=%s AND created_at<%s
                AND operation IN ('EMBEDDING','JUDGE_LLM','ANSWER','SUMMARY') AND estimated_cost IS NOT NULL""",
                          (start.astimezone(timezone.utc), end.astimezone(timezone.utc))):
            if row['currency'] not in ('USD', 'KRW'):
                continue
            krw = Decimal(str(row['estimated_cost'])) * (rate if row['currency'] == 'USD' else 1)
            if row['outcome'] == 'RESERVED':
                reserved += krw
            else:
                used += krw
        ratio = float((used + reserved) / budget) if budget > 0 else None
        cap = bool(setting['hard_cap_enabled'])
        return {'month': start.strftime('%Y-%m'), 'monthly_budget_krw': int(budget), 'month_to_date_krw': float(used),
                'reserved_krw': float(reserved), 'usage_ratio': ratio, 'warning': ratio is not None and ratio >= 0.8,
                'hard_cap_enabled': cap, 'cap_reached': cap and ratio is not None and ratio >= 1}

    # ---- API-65 budget settings ----------------------------------------------------------------------------------
    def admin_budget_settings(self, tx, req, p):
        admin = self.require_admin(p)
        b = req.body
        fields(b, ['monthly_budget_krw', 'usd_krw_rate', 'hard_cap_enabled', 'reason'],
               ['monthly_budget_krw', 'usd_krw_rate', 'hard_cap_enabled', 'reason'])
        budget, rate, cap = b['monthly_budget_krw'], b['usd_krw_rate'], b['hard_cap_enabled']
        if type(budget) is not int or not 1 <= budget <= 10_000_000:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'monthly_budget_krw'})
        if type(rate) not in (int, float) or not 0 < rate <= 100_000:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'usd_krw_rate'})
        if type(cap) is not bool:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'hard_cap_enabled'})
        reason = text(b['reason'], 500)
        moment = self.clock()
        old = tx.one("SELECT value,hard_cap_enabled FROM app_settings WHERE setting_key='MONTHLY_API_BUDGET' FOR UPDATE")
        value = {'monthly_budget_krw': budget, 'usd_krw_rate': rate}
        if old:
            tx.execute("""UPDATE app_settings SET value=%s,hard_cap_enabled=%s,updated_by=%s,updated_at=%s
                WHERE setting_key='MONTHLY_API_BUDGET'""", (dumps(value), cap, admin, moment))
        else:
            tx.execute("""INSERT INTO app_settings(setting_key,value,hard_cap_enabled,updated_by,updated_at)
                VALUES('MONTHLY_API_BUDGET',%s,%s,%s,%s)""", (dumps(value), cap, admin, moment))
        before = {**jsonb(old['value'], {}), 'hard_cap_enabled': bool(old['hard_cap_enabled'])} if old else None
        self.audit(tx, req, admin, 'BUDGET_UPDATED', 'SETTING', 'MONTHLY_API_BUDGET',
                   {'before': before, 'after': {**value, 'hard_cap_enabled': cap}}, reason)
        return Result(200, {**value, 'hard_cap_enabled': cap, 'updated_at': moment})


OPENAPI = [
    ('GET', '/admin/users', 'API-45', '회원 목록'),
    ('PATCH', '/admin/users/{user_id}', 'API-46', '계정 정지 · 복구'),
    ('GET', '/admin/sessions', 'API-47', '강연 목록(관리자)'),
    ('PATCH', '/admin/sessions/{sid}', 'API-48', '평가용 강의 표시 · 강제 종료'),
    ('GET', '/admin/metrics', 'API-64', '운영 지표 · 예산 사용률'),
    ('PUT', '/admin/budget-settings', 'API-65', '월 API 예산 설정'),
]
