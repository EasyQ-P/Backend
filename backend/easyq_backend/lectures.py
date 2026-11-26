"""Lectures (API-13~17, 25): create, state, join links, my-lecture list, term dictionary."""
from datetime import timedelta
import secrets

from easyq_common.contracts import (APIError, Result, dt, dumps, fields,
                                    obj, text, uid, uuid_value)


class LecturesMixin:
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

    def code(self, tx):
        # Serialize allocation across current and immediately revoked code namespaces.
        tx.lock('join-code-allocation')
        for _ in range(100):
            code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(5))
            if not tx.one('SELECT session_id FROM lecture_sessions WHERE join_code=%s OR previous_join_code=%s', (code, code)):
                return code
        raise APIError(503, 'SERVICE_UNAVAILABLE')

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
