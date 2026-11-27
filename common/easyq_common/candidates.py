"""Label candidates (3.3.4 표 3.12 · 6.1.6 · 7.1.3 · 8.2.20 · 8.2.22).

create_candidate() is shared: the worker calls it when QA escalates (AUTO_ESCALATED, SECOND_STAGE_INSUFFICIENT) and the
backend calls it inside the retraction (API-33) / rating (API-34) transaction. sweep_candidates() re-scans a lecture's
events (FINALIZE, and after a finished evaluation-lecture check) so a deferred or missed event still gets its candidate;
event_key makes every path idempotent.

Rules: only while the asker AND the lecture host currently grant TRAINING and the lecture is not an evaluation lecture;
while the evaluation check is PENDING the event is deferred (no candidate yet). LLM_ONLY auto-escalations never become
candidates. Evidence: retraction / rating → one candidate per cited chunk (CUSTOM); second-stage insufficiency and
auto-escalation → the top judge-score chunk; any LLM_ONLY event → the first evidence Sonnet selected.

event_key (8.2.22) is the one formula for every path: SHA-256 of event · question · answer · evidence · actor · revision,
where an audience rating counts as one event family 'AUDIENCE' without revision, so one answer has at most one rating
candidate per rater and evidence. A changed rating (API-34) re-types that candidate in place while nobody has started
reviewing it (no DELETE needed, so the backend role can do it too); a candidate already under review keeps its type.
"""
import hashlib
import re

from easyq_common.contracts import dumps, obj, uid

# 1st masking pass (6.4.3 ①: phone · e-mail · student-number-like digit runs → [MASK]); the training server re-masks.
_MASKS = (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'),
          re.compile(r'(?<!\d)(?:\+?82[-\s.]?)?0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)'),
          re.compile(r'(?<!\d)\d{7,10}(?!\d)'))

DEFAULT_LABEL = {'AUTO_ESCALATED': None, 'SECOND_STAGE_INSUFFICIENT': 'INSUFFICIENT_EVIDENCE',
                 'AUDIENCE_HELPFUL': 'ANSWERABLE', 'AUDIENCE_UNHELPFUL': None}
RETRACTION_LABEL = {'WRONG_OR_INSUFFICIENT': 'INSUFFICIENT_EVIDENCE', 'EXPLAIN_AGAIN': 'ANSWERABLE'}


AUDIENCE = ('AUDIENCE_HELPFUL', 'AUDIENCE_UNHELPFUL')


def event_key(event_type, question_id, answer_id, evidence_id, actor, source_revision):
    family, revision = ('AUDIENCE', '') if event_type in AUDIENCE else (event_type, str(source_revision))
    return hashlib.sha256('|'.join([family, str(question_id), str(answer_id or ''), str(evidence_id), str(actor or ''),
                                    revision]).encode()).hexdigest()


def sanitize(value):
    value = value or ''
    for pattern in _MASKS:
        value = pattern.sub('[MASK]', value)
    return value


def location_label(chunk):
    """Server-built location text (8.2.15): "교안 p.9" / "발화 21:05" (offset from the start of the lecture audio)."""
    if chunk['source_type'] == 'DOCUMENT':
        return f'교안 p.{chunk["page_no"]}'
    seconds = int(chunk['start_ms'] or 0) // 1000
    hours, rest = divmod(seconds, 3600)
    return f'발화 {hours}:{rest // 60:02d}:{rest % 60:02d}' if hours else f'발화 {rest // 60}:{rest % 60:02d}'


def _chunks(tx, sid, ids):
    found = {}
    for cid in dict.fromkeys(str(i) for i in ids if i):
        row = tx.one('SELECT chunk_id,source_type,page_no,start_ms,end_ms,content FROM knowledge_chunks WHERE session_id=%s AND chunk_id=%s',
                     (sid, cid))
        if row:
            found[cid] = row
    return found


def _consent(service, tx, purpose, user=None, participant=None, sid=None):
    row = service.effective(tx, purpose, str(user) if user else None, str(participant) if participant else None, sid)
    return row if row and row['granted'] else None


def eligible(service, tx, question, lecture):
    """Return (state, consent_snapshot): state 'OK', 'DEFERRED' (evaluation check pending) or 'NO'."""
    if not question or question['content'] is None or question['deleted_at'] or not lecture:
        return 'NO', None
    if lecture['is_eval_lecture'] or lecture['status'] in ('DELETING', 'PURGED'):
        return 'NO', None
    sid = str(lecture['session_id'])
    if not question['participant_id'] or not lecture['host_user_id']:
        return 'NO', None
    asker = tx.one('SELECT participant_id,user_id FROM participants WHERE session_id=%s AND participant_id=%s',
                   (sid, question['participant_id']))
    if not asker:
        return 'NO', None
    asked = _consent(service, tx, 'TRAINING', user=asker['user_id'], participant=None if asker['user_id'] else asker['participant_id'], sid=sid)
    hosted = _consent(service, tx, 'TRAINING', user=lecture['host_user_id'], sid=sid)
    if not asked or not hosted:
        return 'NO', None
    if obj(lecture['eval_detection'] or '{}').get('check_status') == 'PENDING':
        return 'DEFERRED', None
    snapshot = {'asker': {'consent_id': str(asked['consent_id']), 'policy_version': asked['policy_version']},
                'host': {'consent_id': str(hosted['consent_id']), 'policy_version': hosted['policy_version']}}
    return 'OK', snapshot


def _evidence_ids(tx, event_type, sid, answer_id, snapshot):
    llm_only = snapshot.get('mode') == 'LLM_ONLY'
    if llm_only:
        selected = snapshot.get('selected_evidence_ids') or []
        return selected[:1]
    if event_type in ('AUTO_ESCALATED', 'SECOND_STAGE_INSUFFICIENT'):
        return [snapshot['top_evidence_id']] if snapshot.get('top_evidence_id') else []
    if not answer_id:
        return []
    rows = tx.all('SELECT chunk_id FROM answer_citations WHERE session_id=%s AND answer_id=%s AND chunk_id IS NOT NULL ORDER BY citation_no',
                  (sid, answer_id))
    return [str(r['chunk_id']) for r in rows]


def create_candidate(service, tx, event_type, question_id, session_id, answer_id=None, actor_user_id=None,
                     actor_participant_id=None, source_revision=1, reason_code=None, replace_stale=False):
    """Create the label candidates for one event inside the caller's transaction; returns the new candidate IDs
    ([] when not eligible, deferred, without evidence, or already created). replace_stale is kept for callers but no
    longer deletes anything: an earlier rating's candidate shares the event_key and is re-typed in place."""
    sid = str(session_id)
    question = tx.one('SELECT * FROM questions WHERE session_id=%s AND question_id=%s', (sid, question_id))
    lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s', (sid,))
    state, consents = eligible(service, tx, question, lecture)
    if state != 'OK':
        return []
    snapshot = obj(question['decision_snapshot'] or '{}')
    if event_type == 'AUTO_ESCALATED' and snapshot.get('route') != 'CUSTOM':
        return []   # 3.3.4: LLM_ONLY escalations have no judge score to order reviews by
    ranked = snapshot.get('candidate_scores') or []
    if not ranked:
        return []
    evidence = _chunks(tx, sid, _evidence_ids(tx, event_type, sid, answer_id, snapshot))
    if not evidence:
        return []
    pool = _chunks(tx, sid, [r.get('evidence_id') for r in ranked])
    retrieved = []
    for r in ranked[:20]:
        c = pool.get(str(r.get('evidence_id')))
        if c:
            retrieved.append({'evidence_id': str(c['chunk_id']), 'evidence_type': c['source_type'], 'location_label': location_label(c),
                              'page_no': c['page_no'], 'start_ms': c['start_ms'], 'end_ms': c['end_ms'],
                              'text': sanitize(c['content']), 'rank': r.get('rank'), 'score': r.get('score')})
    llm_only = snapshot.get('mode') == 'LLM_ONLY'
    score = snapshot.get('llm_score') if llm_only else snapshot.get('max_score')
    threshold = snapshot.get('llm_only_threshold') if llm_only else snapshot.get('threshold')
    judgment = {'mode': snapshot.get('mode'), 'model_version': snapshot.get('model_version'), 'score': score, 'threshold': threshold,
                'margin': (score - threshold) if isinstance(score, (int, float)) and isinstance(threshold, (int, float)) else None,
                'selected_evidence_ids': snapshot.get('selected_evidence_ids') or []}
    default = RETRACTION_LABEL.get(reason_code) if event_type == 'RETRACTED' else DEFAULT_LABEL.get(event_type)
    actor = str(actor_user_id or actor_participant_id or '')
    created = []
    for evidence_id, chunk in evidence.items():
        key = event_key(event_type, question_id, answer_id, evidence_id, actor, source_revision)
        existing = tx.one('SELECT candidate_id FROM candidate_provenance WHERE event_key=%s', (key,))
        if existing:
            if event_type in AUDIENCE:
                tx.execute("""UPDATE feedback_candidates SET event_type=%s,default_label=%s WHERE candidate_id=%s AND event_type<>%s
                    AND review_status='PENDING' AND review_started_at IS NULL AND merge_request_id IS NULL""",
                    (event_type, default, existing['candidate_id'], event_type))
            continue
        cid = uid()
        pair = {'question_text': sanitize(question['content']), 'evidence_id': evidence_id, 'evidence_type': chunk['source_type'],
                'evidence_text': sanitize(chunk['content']), 'page_no': chunk['page_no'], 'start_ms': chunk['start_ms'],
                'end_ms': chunk['end_ms'], 'course_id': lecture['course_id']}
        tx.execute("""INSERT INTO feedback_candidates(candidate_id,event_type,default_label,reason_code,sanitized_pair,
            retrieved_evidence,judgment_snapshot,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, event_type, default, reason_code if event_type == 'RETRACTED' else None, dumps(pair), dumps(retrieved),
             dumps(judgment), service.clock()))
        tx.execute("""INSERT INTO candidate_provenance(candidate_id,question_id,answer_id,actor_user_id,actor_participant_id,
            source_revision,consent_snapshot,event_key,created_at,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, str(question_id), str(answer_id) if answer_id else None, str(actor_user_id) if actor_user_id else None,
             str(actor_participant_id) if actor_participant_id else None, source_revision, dumps(consents), key,
             service.clock(), sid))
        created.append(cid)
    return created


def sweep_candidates(service, tx, session_id):
    """Re-scan the lecture's candidate events (6.1.6-5); safe to repeat. Returns the number of candidates created."""
    sid = str(session_id)
    lecture = tx.one('SELECT host_user_id FROM lecture_sessions WHERE session_id=%s', (sid,))
    if not lecture:
        return 0
    count = 0
    for q in tx.all("""SELECT question_id,escalation_reason FROM questions WHERE session_id=%s
            AND escalation_reason IN ('LOW_CONFIDENCE','SECOND_STAGE_INSUFFICIENT') AND content IS NOT NULL AND deleted_at IS NULL""", (sid,)):
        kind = 'AUTO_ESCALATED' if q['escalation_reason'] == 'LOW_CONFIDENCE' else 'SECOND_STAGE_INSUFFICIENT'
        count += len(create_candidate(service, tx, kind, q['question_id'], sid))
    for a in tx.all("""SELECT answer_id,question_id,retraction_reason FROM answers WHERE session_id=%s AND status='RETRACTED'
            AND retraction_reason IS NOT NULL""", (sid,)):
        count += len(create_candidate(service, tx, 'RETRACTED', a['question_id'], sid, a['answer_id'], actor_user_id=lecture['host_user_id'],
                                      reason_code=a['retraction_reason']))
    for f in tx.all("""SELECT f.answer_id,f.participant_id,f.rating,f.version,a.question_id FROM answer_feedback f
            JOIN answers a ON a.answer_id=f.answer_id AND a.session_id=f.session_id WHERE f.session_id=%s""", (sid,)):
        kind = 'AUDIENCE_HELPFUL' if f['rating'] == 'HELPFUL' else 'AUDIENCE_UNHELPFUL'
        count += len(create_candidate(service, tx, kind, f['question_id'], sid, f['answer_id'], actor_participant_id=f['participant_id'],
                                      source_revision=f['version'], replace_stale=True))
    return count
