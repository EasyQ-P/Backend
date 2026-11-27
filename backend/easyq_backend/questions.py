"""Questions, votes, answers, ratings, notes (API-12 · 26~36 · 38).

Visibility (7.3.1 · 8.2.13): ESCALATED · COMPLETED questions are public and never carry the author; every other
question (and every answer) is private to its asker and the lecture HOST. Another person's private question is
answered with 404 so its existence stays hidden. Rows whose content was deleted or purged are never served.
Question/answer events carry the question_id column so the WebSocket hub can route them to the asker (7.3.10).
"""
import base64
from datetime import timedelta
import json
import re

from easyq_common.candidates import RETRACTION_LABEL, create_candidate, location_label
from easyq_common.contracts import APIError, Result, dt, dumps, fields, obj, text, uid, uuid_value
from easyq_common.security import credential_hash

PUBLIC = ('ESCALATED', 'COMPLETED')
def bigrams(value):
    value = re.sub(r'\s+', '', (value or '').casefold())
    return {value[i:i + 2] for i in range(len(value) - 1)} or ({value} if value else set())


def similarity(a, b):
    """Character-bigram Dice coefficient: language-agnostic, no external call, nothing stored (API-26)."""
    x, y = bigrams(a), bigrams(b)
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def page(query):
    try:
        limit = int(query.get('limit', 20))
    except ValueError:
        raise APIError(422, 'VALIDATION_ERROR') from None
    if not 1 <= limit <= 100:
        raise APIError(422, 'VALIDATION_ERROR')
    cursor = None
    if query.get('cursor'):
        try:
            cursor = json.loads(base64.urlsafe_b64decode(query['cursor'].encode()))
            cursor = (int(cursor['v']), dt(cursor['t']), uuid_value(cursor['id']))
        except (ValueError, KeyError, TypeError):
            raise APIError(422, 'VALIDATION_ERROR') from None
    return limit, cursor


def next_cursor(rows, limit):
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return base64.urlsafe_b64encode(json.dumps({'v': last['vote_count'], 't': str(last['created_at']), 'id': str(last['question_id'])}).encode()).decode()


def keyset(sort, cursor):
    """ORDER BY and the 'after cursor' condition. votes: vote_count desc, then oldest first (7.3.5 API-28)."""
    if sort == 'votes':
        order = ' ORDER BY q.vote_count DESC, q.created_at, q.question_id'
        cond = ' AND (q.vote_count<%s OR (q.vote_count=%s AND (q.created_at>%s OR (q.created_at=%s AND q.question_id>%s))))'
        args = [cursor[0], cursor[0], cursor[1], cursor[1], cursor[2]] if cursor else []
    else:
        order = ' ORDER BY q.created_at DESC, q.question_id DESC'
        cond = ' AND (q.created_at<%s OR (q.created_at=%s AND q.question_id<%s))'
        args = [cursor[1], cursor[1], cursor[2]] if cursor else []
    return order, (cond if cursor else ''), args


def citation_view(c):
    return {'citation_no': c.get('citation_no'), 'chunk_id': str(c['chunk_id']) if c.get('chunk_id') else None,
            'source_type': c.get('source_type'), 'location_label': c.get('location_label'), 'page_no': c.get('page_no'),
            'start_ms': c.get('start_ms'), 'end_ms': c.get('end_ms'), 'excerpt': c.get('excerpt')}


class QuestionsMixin:
    ROUTES = [
        ('GET', r'/me/questions', 'my_questions'),
        ('POST', r'/sessions/(?P<sid>[^/]+)/similar-question-searches', 'similar_questions'),
        ('POST', r'/sessions/(?P<sid>[^/]+)/questions', 'submit_question'),
        ('GET', r'/sessions/(?P<sid>[^/]+)/questions', 'list_questions'),
        ('GET', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)', 'get_question'),
        ('PATCH', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)', 'complete_question'),
        ('PUT', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/votes/me', 'vote'),
        ('DELETE', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/votes/me', 'unvote'),
        ('POST', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/answer-retractions', 'retract_answer'),
        ('PUT', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/answer-ratings/me', 'rate_answer'),
        ('PUT', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/notes/me', 'save_note'),
        ('DELETE', r'/sessions/(?P<sid>[^/]+)/questions/(?P<qid>[^/]+)/notes/me', 'delete_note'),
        ('GET', r'/sessions/(?P<sid>[^/]+)/sources/(?P<chunk_id>[^/]+)', 'citation_source'),
    ]

    # ---- access and serialization -----------------------------------------------------------
    def question_access(self, tx, sid, p, lock=False):
        """(lecture, owner, participant). Owner = HOST who has not deleted the lecture; participant = ACTIVE
        participant (guests during the lecture, members while the lecture is in their archive). Else 404."""
        s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s' + (' FOR UPDATE' if lock else ''), (sid,))
        if not s or s['status'] == 'DELETING':
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        owner = bool(p['user_id']) and str(p['user_id']) == str(s['host_user_id']) and not s['host_deleted_at']
        part = None if owner else self.participant(tx, sid, p)
        if not owner and not part:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        return s, owner, part

    def question_row(self, tx, sid, qid, lock=False):
        q = tx.one("""SELECT * FROM questions WHERE session_id=%s AND question_id=%s AND deleted_at IS NULL
            AND content_purged_at IS NULL""" + (' FOR UPDATE' if lock else ''), (sid, qid))
        if not q:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        return q

    @staticmethod
    def asked_by(q, part):
        return bool(part) and q['participant_id'] is not None and str(q['participant_id']) == str(part['participant_id'])

    def visible_question(self, tx, sid, qid, owner, part, lock=False):
        """The question if this caller may know it exists (HOST, asker, or public), else 404."""
        q = self.question_row(tx, sid, qid, lock)
        if not (owner or self.asked_by(q, part) or q['status'] in PUBLIC):
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        return q

    @staticmethod
    def public_fields(q):
        """QuestionPublic without voted_by_me · my_note (also the public_question.updated payload)."""
        doc = q['document_id']
        return {'question_id': str(q['question_id']), 'content': q['content'], 'status': q['status'],
                'document_id': str(doc) if doc else None, 'page_no': q['page_no'] if doc else None,
                'vote_count': q['vote_count'], 'version': q['version'], 'created_at': q['created_at']}

    def note_view(self, tx, q, p):
        if not p['user_id']:
            return None
        n = tx.one('SELECT content,question_text,updated_at FROM question_notes WHERE session_id=%s AND question_id=%s AND user_id=%s',
                   (q['session_id'], q['question_id'], p['user_id']))
        return dict(n) if n else None

    def question_public(self, tx, q, p, part):
        voted = bool(part) and bool(tx.one('SELECT 1 AS x FROM votes WHERE session_id=%s AND question_id=%s AND participant_id=%s',
                                           (q['session_id'], q['question_id'], part['participant_id'])))
        return self.public_fields(q) | {'voted_by_me': voted, 'my_note': self.note_view(tx, q, p)}

    def answer_view(self, tx, q, rater=None):
        a = tx.one('SELECT * FROM answers WHERE session_id=%s AND question_id=%s', (q['session_id'], q['question_id']))
        if not a:
            return None
        active = a['status'] == 'ACTIVE'
        citations = tx.all('SELECT * FROM answer_citations WHERE session_id=%s AND answer_id=%s ORDER BY citation_no',
                           (a['session_id'], a['answer_id'])) if active else []
        rating = None
        if rater:
            r = tx.one('SELECT rating FROM answer_feedback WHERE session_id=%s AND answer_id=%s AND participant_id=%s',
                       (a['session_id'], a['answer_id'], rater['participant_id']))
            rating = r['rating'] if r else None
        # 7.3.1 Answer: a retracted answer shows only its status (content = null, citations = []).
        return {'answer_id': str(a['answer_id']), 'status': a['status'], 'content': a['content'] if active else None,
                'citations': [citation_view(c) for c in citations], 'my_rating': rating}

    def question_private(self, tx, q, p, part, mine):
        """QuestionPrivate for the asker (mine) or the HOST. my_rating is the asker's own rating only."""
        failed = tx.one("SELECT error_code FROM jobs WHERE idempotency_key=%s AND status='FAILED'", ('qa:' + str(q['question_id']),))
        return self.question_public(tx, q, p, part) | {
            'escalation_reason': q['escalation_reason'], 'answer': self.answer_view(tx, q, part if mine else None),
            'processing_error': failed['error_code'] if failed else None}

    def question_event(self, tx, kind, payload, sid, qid):
        """ws_events row with question_id (8.2.29) so the hub can route asker-only events (7.3.10)."""
        row = tx.one("""INSERT INTO ws_events(event_type,payload,session_id,question_id,expires_at)
            VALUES(%s,%s,%s,%s,%s) RETURNING event_id""", (kind, dumps(payload), sid, qid, self.clock() + timedelta(hours=24)))
        tx.notify(row['event_id'])

    def public_event(self, tx, sid, qid):
        q = tx.one('SELECT * FROM questions WHERE session_id=%s AND question_id=%s', (sid, qid))
        self.question_event(tx, 'public_question.updated', self.public_fields(q), sid, qid)

    # ---- API-12 내 질문 기록 ----------------------------------------------------------------
    def my_questions(self, tx, req, p):
        user_id = self.member(p)
        q = req.query
        if set(q) - {'session_id', 'cursor', 'limit'}:
            raise APIError(422, 'VALIDATION_ERROR')
        limit, cursor = page(q)
        where = ['a.user_id=%s', "a.kind='MEMBER'", 'a.archived=true', "s.status<>'DELETING'", 'q.deleted_at IS NULL', 'q.content_purged_at IS NULL']
        args = [user_id]
        if q.get('session_id'):
            where.append('q.session_id=%s')
            args.append(uuid_value(q['session_id']))
        base = """FROM questions q JOIN participants a ON a.session_id=q.session_id AND a.participant_id=q.participant_id
            JOIN lecture_sessions s ON s.session_id=q.session_id WHERE """ + ' AND '.join(where)
        total = tx.one('SELECT count(*) AS n ' + base, tuple(args))['n']
        order, cond, extra = keyset('latest', cursor)
        rows = tx.all('SELECT q.*, s.title AS session_title ' + base + cond + order + ' LIMIT %s', tuple(args + extra + [limit + 1]))
        items = []
        for r in rows[:limit]:
            part = {'participant_id': r['participant_id']}
            items.append(self.question_private(tx, r, p, part, True) | {'session_id': str(r['session_id']), 'session_title': r['session_title']})
        return Result(200, {'items': items, 'next_cursor': next_cursor(rows, limit), 'total_count': total})

    # ---- API-26 유사 공개 질문 검색 -----------------------------------------------------------
    SIMILAR_MIN, SIMILAR_MAX = 0.3, 3

    def similar_questions(self, tx, req, p, sid):
        fields(req.body, ['content', 'document_id', 'page_no'], ['content'])
        content = text(req.body['content'], 200)
        _, owner, part = self.question_access(tx, sid, p)
        self.question_target(tx, sid, req.body)
        # Only ESCALATED questions can be voted on; the typed sentence is neither stored nor logged.
        rows = tx.all("""SELECT * FROM questions WHERE session_id=%s AND status='ESCALATED' AND deleted_at IS NULL
            AND content_purged_at IS NULL""", (sid,))
        scored = sorted(((similarity(content, r['content']), r) for r in rows), key=lambda x: (-x[0], -x[1]['vote_count'], str(x[1]['created_at'])))
        items = [{'question': self.question_public(tx, r, p, part), 'similarity': round(score, 4)}
                 for score, r in scored[:self.SIMILAR_MAX] if score >= self.SIMILAR_MIN]
        return Result(200, {'items': items})

    def question_target(self, tx, sid, body):
        """Optional document_id · page_no of a question; page_no needs a document of this lecture."""
        doc_id, page_no = body.get('document_id'), body.get('page_no')
        if page_no is not None and (type(page_no) is not int or page_no < 1 or doc_id is None):
            raise APIError(422, 'VALIDATION_ERROR')
        if doc_id is None:
            return None, None
        doc = tx.one('SELECT document_id,page_count FROM documents WHERE session_id=%s AND document_id=%s', (sid, uuid_value(doc_id)))
        if not doc or (page_no and doc['page_count'] and page_no > doc['page_count']):
            raise APIError(422, 'VALIDATION_ERROR')
        return str(doc['document_id']), page_no

    # ---- API-27 질문 등록 --------------------------------------------------------------------
    def submit_question(self, tx, req, p, sid):
        fields(req.body, ['content', 'document_id', 'page_no'], ['content'])
        content = text(req.body['content'], 200)
        try:
            key = uuid_value(req.headers.get('idempotency-key'))   # questions.idempotency_key is a UUID (8.2.13)
        except APIError:
            raise APIError(422, 'VALIDATION_ERROR', {'field': 'Idempotency-Key'}) from None
        s, owner, part = self.question_access(tx, sid, p, lock=True)
        if not part:
            raise APIError(403, 'FORBIDDEN')   # the HOST is not a participant of their own lecture
        doc_id, page_no = self.question_target(tx, sid, req.body)
        pid = str(part['participant_id'])
        tx.lock('question:' + pid + ':' + key)
        prior = tx.one('SELECT * FROM questions WHERE participant_id=%s AND idempotency_key=%s', (pid, key))
        if prior:
            # 7.1.2: same participant · key · body is the same 202; a different body is 409.
            same = (prior['content'], str(prior['document_id']) if prior['document_id'] else None, prior['page_no']) == (content, doc_id, page_no)
            if not same or str(prior['session_id']) != sid:
                raise APIError(409, 'IDEMPOTENCY_CONFLICT')
            job = tx.one('SELECT job_id,status FROM jobs WHERE idempotency_key=%s', ('qa:' + str(prior['question_id']),))
            return self.accepted(prior['question_id'], job['job_id'], job['status'])
        user_id = str(p['user_id']) if p['user_id'] else None
        self.require_service(tx, user=user_id, participant=None if user_id else pid, sid=sid)
        if s['status'] != 'LIVE':
            raise APIError(409, 'SESSION_NOT_LIVE')
        qid = uid()
        tx.execute("""INSERT INTO questions(question_id,session_id,participant_id,document_id,page_no,content,status,
            idempotency_key,created_at) VALUES(%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s)""",
            (qid, sid, pid, doc_id, page_no, content, key, self.clock()))
        jid = self.job(tx, 'QA', qid, 'qa:' + qid, {}, user=user_id, sid=sid, participant=pid)
        self.question_event(tx, 'question.updated', {'question_id': qid, 'status': 'QUEUED'}, sid, qid)
        return self.accepted(qid, jid)

    # ---- API-28 질문 목록 --------------------------------------------------------------------
    def list_questions(self, tx, req, p, sid):
        q = req.query
        if set(q) - {'scope', 'document_id', 'page_no', 'sort', 'cursor', 'limit'} or q.get('scope') not in ('public', 'mine', 'review') \
                or q.get('sort', 'latest') not in ('votes', 'latest'):
            raise APIError(422, 'VALIDATION_ERROR')
        scope, sort = q['scope'], q.get('sort', 'latest')
        limit, cursor = page(q)
        _, owner, part = self.question_access(tx, sid, p)
        if (scope == 'review' and not owner) or (scope == 'mine' and not part):
            raise APIError(403, 'FORBIDDEN')
        where, args = ['q.session_id=%s', 'q.deleted_at IS NULL', 'q.content_purged_at IS NULL'], [sid]
        if scope == 'public':
            where.append("q.status IN ('ESCALATED','COMPLETED')")
        elif scope == 'mine':
            where.append('q.participant_id=%s')
            args.append(part['participant_id'])
        else:   # HOST review: escalated questions and the AI answers to check (6.2.8)
            where.append("q.status IN ('AI_ANSWERED','ESCALATED','COMPLETED')")
        if q.get('document_id'):
            where.append('q.document_id=%s')
            args.append(uuid_value(q['document_id']))
        if q.get('page_no'):
            try:
                number = int(q['page_no'])
            except ValueError:
                raise APIError(422, 'VALIDATION_ERROR') from None
            where.append('q.document_id IS NOT NULL AND q.page_no=%s')   # a page without its document is hidden
            args.append(number)
        base = 'FROM questions q WHERE ' + ' AND '.join(where)
        total = tx.one('SELECT count(*) AS n ' + base, tuple(args))['n']
        order, cond, extra = keyset(sort, cursor)
        rows = tx.all('SELECT q.* ' + base + cond + order + ' LIMIT %s', tuple(args + extra + [limit + 1]))
        if scope == 'public':
            items = [self.question_public(tx, r, p, part) for r in rows[:limit]]
        else:
            items = [self.question_private(tx, r, p, part, scope == 'mine') for r in rows[:limit]]
        return Result(200, {'items': items, 'next_cursor': next_cursor(rows, limit), 'total_count': total})

    # ---- API-29 질문 · 답변 조회 ----------------------------------------------------------------
    def get_question(self, tx, req, p, sid, qid):
        _, owner, part = self.question_access(tx, sid, p)
        q = self.visible_question(tx, sid, qid, owner, part)
        mine = self.asked_by(q, part)
        data = self.question_private(tx, q, p, part, mine) if owner or mine else self.question_public(tx, q, p, part)
        return Result(200, data, {'ETag': f'"{q["version"]}"'})

    # ---- API-30 · 31 공감 · 공감 취소 ------------------------------------------------------------
    def vote_target(self, tx, req, p, sid, qid):
        fields(req.body, [])
        s, owner, part = self.question_access(tx, sid, p, lock=True)
        if not part:
            raise APIError(403, 'FORBIDDEN')
        q = self.visible_question(tx, sid, qid, owner, part, lock=True)
        if s['status'] != 'LIVE':
            raise APIError(409, 'SESSION_NOT_LIVE')
        return q, part

    def vote(self, tx, req, p, sid, qid):
        q, part = self.vote_target(tx, req, p, sid, qid)
        if q['status'] != 'ESCALATED':
            raise APIError(409, 'QUESTION_NOT_PUBLIC')
        key = (sid, qid, part['participant_id'])
        if not tx.one('SELECT 1 AS x FROM votes WHERE session_id=%s AND question_id=%s AND participant_id=%s', key):
            tx.execute('INSERT INTO votes(session_id,question_id,participant_id,created_at) VALUES(%s,%s,%s,%s)', key + (self.clock(),))
            tx.execute('UPDATE questions SET vote_count=vote_count+1 WHERE session_id=%s AND question_id=%s', (sid, qid))
            self.public_event(tx, sid, qid)
        count = tx.one('SELECT vote_count FROM questions WHERE session_id=%s AND question_id=%s', (sid, qid))['vote_count']
        return Result(200, {'question_id': qid, 'voted': True, 'vote_count': count})

    def unvote(self, tx, req, p, sid, qid):
        # API-31: LIVE only, own vote only, idempotent (P-7: any status, as long as the vote exists).
        _, part = self.vote_target(tx, req, p, sid, qid)
        key = (sid, qid, part['participant_id'])
        if tx.one('SELECT 1 AS x FROM votes WHERE session_id=%s AND question_id=%s AND participant_id=%s', key):
            tx.execute('DELETE FROM votes WHERE session_id=%s AND question_id=%s AND participant_id=%s', key)
            tx.execute("""UPDATE questions SET vote_count=CASE WHEN vote_count>0 THEN vote_count-1 ELSE 0 END
                WHERE session_id=%s AND question_id=%s""", (sid, qid))
            self.public_event(tx, sid, qid)
        return Result(204)

    # ---- API-32 이관 질문 답변 완료 · 완료 취소 ---------------------------------------------------
    def host_question(self, tx, req, p, sid, qid):
        s, owner, _ = self.question_access(tx, sid, p, lock=True)
        if not owner:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        q = self.question_row(tx, sid, qid, lock=True)
        if s['status'] != 'LIVE':
            raise APIError(409, 'SESSION_NOT_LIVE')
        self.version(req, q)
        return q

    def complete_question(self, tx, req, p, sid, qid):
        fields(req.body, ['status'], ['status'])
        target = req.body['status']
        if target not in PUBLIC:
            raise APIError(422, 'VALIDATION_ERROR')
        q = self.host_question(tx, req, p, sid, qid)
        if (q['status'], target) not in [('ESCALATED', 'COMPLETED'), ('COMPLETED', 'ESCALATED')]:
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        tx.execute('UPDATE questions SET status=%s,completed_at=%s,version=version+1 WHERE session_id=%s AND question_id=%s',
                   (target, self.clock() if target == 'COMPLETED' else None, sid, qid))
        self.public_event(tx, sid, qid)
        q = self.question_row(tx, sid, qid)
        return Result(200, self.question_private(tx, q, p, None, False), {'ETag': f'"{q["version"]}"'})

    # ---- API-33 AI 답변 철회 --------------------------------------------------------------------

    def retract_answer(self, tx, req, p, sid, qid):
        b = req.body
        fields(b, ['reason_code', 'reason_detail'], ['reason_code'])
        if b['reason_code'] not in RETRACTION_LABEL:
            raise APIError(422, 'VALIDATION_ERROR')
        detail = text(b['reason_detail'], 500, 0) or None if b.get('reason_detail') is not None else None
        q = self.host_question(tx, req, p, sid, qid)
        a = tx.one('SELECT * FROM answers WHERE session_id=%s AND question_id=%s FOR UPDATE', (sid, qid))
        if not a or a['status'] != 'ACTIVE':
            raise APIError(409, 'ANSWER_NOT_ACTIVE')
        now = self.clock()
        tx.execute("""UPDATE answers SET status='RETRACTED',retraction_reason=%s,retraction_detail=%s,retracted_at=%s
            WHERE session_id=%s AND answer_id=%s""", (b['reason_code'], detail, now, sid, a['answer_id']))
        tx.execute("""UPDATE questions SET status='ESCALATED',escalation_reason='RETRACTED',version=version+1
            WHERE session_id=%s AND question_id=%s""", (sid, qid))
        request_id = req.headers.get('x-request-id', '')
        try:
            request_id = uuid_value(request_id)
        except APIError:
            request_id = uid()
        # Audit (8.2.30): pseudonymous target, allowed fields only, never the answer/question text or free-text detail.
        tx.execute("""INSERT INTO audit_events(audit_event_id,actor_user_id,action,target_type,target_ref,changes,reason,request_id,created_at)
            VALUES(%s,%s,'ANSWER_RETRACTED','ANSWER',%s,%s,%s,%s,%s)""",
            (uid(), p['user_id'], credential_hash(self.settings.hmac_key, 'audit', 'ANSWER', str(a['answer_id']))[:64],
             dumps({'answer_status': ['ACTIVE', 'RETRACTED'], 'question_status': [q['status'], 'ESCALATED']}), b['reason_code'], request_id, now))
        self.question_event(tx, 'answer.retracted', {'question_id': qid, 'answer_id': str(a['answer_id'])}, sid, qid)
        self.public_event(tx, sid, qid)
        create_candidate(self, tx, 'RETRACTED', qid, sid, str(a['answer_id']), actor_user_id=str(p['user_id']), reason_code=b['reason_code'])
        version = q['version'] + 1
        return Result(201, {'question_id': qid, 'answer_status': 'RETRACTED', 'question_status': 'ESCALATED',
                            'reason_code': b['reason_code'], 'question_version': version}, {'ETag': f'"{version}"'})

    # ---- API-34 AI 답변 평가 --------------------------------------------------------------------
    def rate_answer(self, tx, req, p, sid, qid):
        b = req.body
        fields(b, ['answer_id', 'rating'], ['answer_id', 'rating'])
        answer_id = uuid_value(b['answer_id'])
        if b['rating'] not in ('HELPFUL', 'UNHELPFUL'):
            raise APIError(422, 'VALIDATION_ERROR')
        _, owner, part = self.question_access(tx, sid, p)
        q = self.question_row(tx, sid, qid)
        if not self.asked_by(q, part):
            raise APIError(404, 'RESOURCE_NOT_FOUND')   # only the asker rates; others must not learn it exists
        a = tx.one('SELECT * FROM answers WHERE session_id=%s AND question_id=%s', (sid, qid))
        if not a or str(a['answer_id']) != answer_id:
            raise APIError(404, 'RESOURCE_NOT_FOUND')
        if a['status'] != 'ACTIVE':
            raise APIError(409, 'ANSWER_NOT_ACTIVE')
        pid = str(part['participant_id'])
        tx.lock('rating:' + answer_id + ':' + pid)
        prior = tx.one('SELECT * FROM answer_feedback WHERE session_id=%s AND answer_id=%s AND participant_id=%s', (sid, answer_id, pid))
        if prior and prior['rating'] == b['rating']:
            return Result(200, {'answer_id': answer_id, 'rating': prior['rating'], 'version': prior['version']})
        if prior:
            version = prior['version'] + 1
            tx.execute('UPDATE answer_feedback SET rating=%s,version=%s,updated_at=%s WHERE session_id=%s AND answer_id=%s AND participant_id=%s',
                       (b['rating'], version, self.clock(), sid, answer_id, pid))
        else:
            version = 1
            tx.execute('INSERT INTO answer_feedback(answer_id,participant_id,rating,version,updated_at,session_id) VALUES(%s,%s,%s,1,%s,%s)',
                       (answer_id, pid, b['rating'], self.clock(), sid))
        create_candidate(self, tx, 'AUDIENCE_' + b['rating'], qid, sid, answer_id, actor_participant_id=pid, source_revision=version)
        return Result(200, {'answer_id': answer_id, 'rating': b['rating'], 'version': version})

    # ---- API-35 · 36 필기 ---------------------------------------------------------------------
    def note_owner(self, tx, p, sid):
        _, owner, part = self.question_access(tx, sid, p)
        if not p['user_id'] or not part:
            raise APIError(403, 'FORBIDDEN')   # guests and the HOST do not write notes
        return part

    def save_note(self, tx, req, p, sid, qid):
        fields(req.body, ['content'], ['content'])
        content = text(req.body['content'], 1000)
        part = self.note_owner(tx, p, sid)
        q = self.visible_question(tx, sid, qid, False, part)
        if q['status'] not in PUBLIC:
            raise APIError(409, 'QUESTION_NOT_PUBLIC')
        now = self.clock()
        key = (sid, qid, p['user_id'])
        if tx.one('SELECT 1 AS x FROM question_notes WHERE session_id=%s AND question_id=%s AND user_id=%s', key):
            tx.execute('UPDATE question_notes SET content=%s,updated_at=%s WHERE session_id=%s AND question_id=%s AND user_id=%s', (content, now) + key)
        else:
            # The question sentence is copied once, on the first save (8.2.17).
            tx.execute("""INSERT INTO question_notes(session_id,question_id,user_id,question_text,content,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", key + (q['content'], content, now, now))
        return Result(200, self.note_view(tx, q, p))

    def delete_note(self, tx, req, p, sid, qid):
        fields(req.body, [])
        self.note_owner(tx, p, sid)
        tx.execute('DELETE FROM question_notes WHERE session_id=%s AND question_id=%s AND user_id=%s', (sid, qid, p['user_id']))
        return Result(204)

    # ---- API-38 인용 근거 원문 -------------------------------------------------------------------
    def citation_source(self, tx, req, p, sid, chunk_id):
        _, owner, part = self.question_access(tx, sid, p)
        chunk = tx.one('SELECT * FROM knowledge_chunks WHERE session_id=%s AND chunk_id=%s', (sid, chunk_id))
        if not chunk:
            raise APIError(404, 'RESOURCE_NOT_FOUND')   # raw data purged or deleted: the screen keeps label · excerpt
        cite = None
        if owner:
            cite = tx.one('SELECT * FROM answer_citations WHERE session_id=%s AND chunk_id=%s ORDER BY citation_no LIMIT 1', (sid, chunk_id))
        else:
            cite = tx.one("""SELECT c.* FROM answer_citations c JOIN answers a ON a.session_id=c.session_id AND a.answer_id=c.answer_id
                JOIN questions q ON q.session_id=a.session_id AND q.question_id=a.question_id
                WHERE c.session_id=%s AND c.chunk_id=%s AND a.status='ACTIVE' AND q.participant_id=%s
                AND q.deleted_at IS NULL AND q.content_purged_at IS NULL ORDER BY c.citation_no LIMIT 1""",
                (sid, chunk_id, part['participant_id']))
            if not cite and p['user_id']:
                summary = tx.one("SELECT source_refs FROM summary_notes WHERE session_id=%s AND status='READY'", (sid,))
                refs = obj(summary['source_refs']) if summary else []
                cite = next((dict(r, citation_no=r.get('citation_no', i + 1)) for i, r in enumerate(refs or [])
                             if str(r.get('chunk_id')) == chunk_id), None)
            if not cite:
                raise APIError(403, 'FORBIDDEN')
        base = {'citation_no': None, 'chunk_id': chunk_id, 'source_type': chunk['source_type'], 'location_label': location_label(chunk),
                'page_no': chunk['page_no'], 'start_ms': chunk['start_ms'], 'end_ms': chunk['end_ms'], 'excerpt': chunk['content'][:200]}
        if cite:
            base.update({k: v for k, v in citation_view(cite).items() if v is not None})
        base.update(chunk_id=chunk_id, source_type=chunk['source_type'], content=chunk['content'])
        return Result(200, base)


OPENAPI = [
    ('GET', '/me/questions', 'API-12', '내 질문 기록'),
    ('POST', '/sessions/{sid}/similar-question-searches', 'API-26', '유사 공개 질문 검색'),
    ('POST', '/sessions/{sid}/questions', 'API-27', '질문 등록'),
    ('GET', '/sessions/{sid}/questions', 'API-28', '질문 목록(public · mine · review)'),
    ('GET', '/sessions/{sid}/questions/{qid}', 'API-29', '질문 · 답변 조회'),
    ('PATCH', '/sessions/{sid}/questions/{qid}', 'API-32', '이관 질문 답변 완료 · 완료 취소'),
    ('PUT', '/sessions/{sid}/questions/{qid}/votes/me', 'API-30', '공감'),
    ('DELETE', '/sessions/{sid}/questions/{qid}/votes/me', 'API-31', '공감 취소'),
    ('POST', '/sessions/{sid}/questions/{qid}/answer-retractions', 'API-33', 'AI 답변 철회'),
    ('PUT', '/sessions/{sid}/questions/{qid}/answer-ratings/me', 'API-34', 'AI 답변 평가'),
    ('PUT', '/sessions/{sid}/questions/{qid}/notes/me', 'API-35', '필기 저장 · 수정'),
    ('DELETE', '/sessions/{sid}/questions/{qid}/notes/me', 'API-36', '필기 삭제'),
    ('GET', '/sessions/{sid}/sources/{chunk_id}', 'API-38', '인용 근거 원문'),
]
