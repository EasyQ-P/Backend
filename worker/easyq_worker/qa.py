"""QA job (6.1.4 · 6.1.5 · 6.2.7): hybrid retrieval, judgment, answer generation, citation check.

QUEUED → PROCESSING (question.progress SEARCHING) → retrieval in the same lecture's READY chunks (vector 20 + Kiwi BM25
20 → RRF top 20) → judgment (CUSTOM: training server 7.4.26 within 800 ms, else this question by LLM_ONLY; admin-fixed
LLM_ONLY: always Sonnet) → answer (question.progress WRITING, answer.delta temporary events) → citation check in code →
AI_ANSWERED, or ESCALATED with a reason. Every write runs in a transaction that first re-checks the job lease, so a QA job
that FINALIZE cancelled (8.2.28: CANCELLED + new lease_token) cannot overwrite the escalation.
The QA job always ends the question (8.2.28): provider failures escalate instead of failing the job.
"""
from datetime import datetime, timezone
import json
import re
import sys
import time

from easyq_common.contracts import APIError, dt, dumps, obj, uid
from easyq_common.trainer import active_serving

from .budget import reserve, settle
from easyq_common.candidates import create_candidate, location_label
from .judge_model import JudgeUnavailable, TrainerClient
from .llm import AnthropicLLM, call_llm
from .retrieval import check_vectors

JUDGE_TIMEOUT_MS = 800
TOP_K = 5
MAX_SENTENCES = 4
DELTA_FLUSH_CHARS = 24

JUDGE_SYSTEM = ('You judge whether lecture evidence answers a student question. Reply with JSON only: '
                '{"score": <integer 0-100, confidence that the numbered evidence answers the question>, '
                '"evidence": [<up to 5 evidence numbers that support the answer, best first>]}. '
                'Opinion questions and questions the evidence does not cover get a low score.')
ANSWER_SYSTEM = ('You answer a student question during a live lecture using ONLY the numbered evidence. '
                 'First line: exactly ANSWER if the evidence is sufficient, otherwise exactly INSUFFICIENT and nothing else. '
                 'After ANSWER, on the next line write the answer in Korean in at most 4 sentences, citing evidence numbers like [1]. '
                 'Do not cite numbers that are not listed. Do not add facts that are not in the evidence.')


def rrf(vector_ids, keyword_ids, top_n=20):
    """Shared rc-v1 fusion (retrieval.search.rrf_merge, k = 60)."""
    from retrieval.search import rrf_merge
    return rrf_merge(vector_ids, keyword_ids, top_n)


def bm25_config():
    from retrieval.config import DEFAULT_CONFIG
    return DEFAULT_CONFIG


class PostgresSearch:
    """6.1.5 on PostgreSQL. Vector: exact cosine distance inside the lecture (the "+ 0" keeps the planner off the global
    HNSW index, which would filter after the top-k and could return fewer than 20 — review D-8). Keyword: GIN pre-filter
    with the question's tokens, then BM25 (k1, b from rc-v1) over the same lecture's READY chunks, term frequencies read
    back from search_vector positions."""

    def vector_ids(self, tx, sid, vector, limit=20):
        rows = tx.all("""SELECT chunk_id FROM knowledge_chunks WHERE session_id=%s AND index_status='READY' AND embedding IS NOT NULL
            ORDER BY (embedding <=> %s::vector) + 0, chunk_id LIMIT %s""", (sid, dumps(vector), limit))
        return [str(r['chunk_id']) for r in rows]

    def keyword_ids(self, tx, sid, tokens, limit=20):
        if not tokens:
            return []
        terms = []
        for r in tx.all("SELECT lexeme, coalesce(array_length(positions,1),1) AS n FROM unnest(to_tsvector('simple',%s))", (' '.join(tokens),)):
            terms.extend([r['lexeme']] * r['n'])
        lexemes = sorted(set(terms))
        if not lexemes:
            return []
        query = ' | '.join("'" + t.replace('\\', '\\\\').replace("'", "''") + "'" for t in lexemes)
        stats = tx.one("""SELECT count(*) AS n, coalesce(avg(dl),0) AS avgdl FROM (
            SELECT (SELECT coalesce(sum(coalesce(array_length(u.positions,1),1)),0) FROM unnest(k.search_vector) u) AS dl
            FROM knowledge_chunks k WHERE k.session_id=%s AND k.index_status='READY') t""", (sid,))
        rows = tx.all("""SELECT k.chunk_id, u.lexeme, coalesce(array_length(u.positions,1),1) AS tf, d.dl
            FROM knowledge_chunks k
            CROSS JOIN LATERAL unnest(k.search_vector) u
            CROSS JOIN LATERAL (SELECT coalesce(sum(coalesce(array_length(x.positions,1),1)),0) AS dl FROM unnest(k.search_vector) x) d
            WHERE k.session_id=%s AND k.index_status='READY' AND k.search_vector @@ %s::tsquery AND u.lexeme = ANY(%s)""",
            (sid, query, lexemes))
        docs, df = {}, {}
        for r in rows:
            doc = docs.setdefault(str(r['chunk_id']), {'dl': int(r['dl']), 'tf': {}})
            doc['tf'][r['lexeme']] = int(r['tf'])
            df[r['lexeme']] = df.get(r['lexeme'], 0) + 1
        return bm25_rank(docs, df, int(stats['n']), float(stats['avgdl']), terms, limit)


def bm25_rank(docs, df, count, average, query_terms, limit):
    """Okapi BM25 exactly as retrieval.keyword.BM25Index.search (idf = ln(1 + (N - df + 0.5) / (df + 0.5)),
    repeated query tokens add again, score > 0 only, ties by id), fed with statistics read from the database."""
    import math
    cfg = bm25_config()
    scores = []
    for doc_id, doc in docs.items():
        ratio = doc['dl'] / average if average else 0.0
        score = 0.0
        for term in query_terms:
            tf = doc['tf'].get(term, 0)
            if tf:
                idf = math.log(1.0 + (count - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * (cfg.bm25_k1 + 1.0) / (tf + cfg.bm25_k1 * (1.0 - cfg.bm25_b + cfg.bm25_b * ratio))
        if score > 0.0:
            scores.append((doc_id, score))
    scores.sort(key=lambda item: (-item[1], item[0]))
    return [doc_id for doc_id, _ in scores[:limit]]


def log_line(level, event, code, message, meta):
    """8.3 common fields; never question / answer / evidence text."""
    line = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': level, 'service': 'worker', 'event': event,
            'request_id': None, 'trace_id': None, 'code': code, 'message': message, 'meta': meta}
    print(dumps(line), file=sys.stderr, flush=True)


def split_sentences(text):
    parts = re.split(r'(?<=[.!?。])\s+', text.strip())
    return [p for p in parts if p.strip()]


class Escalate(Exception):
    def __init__(self, reason, candidate=None, discard=None):
        super().__init__(reason)
        self.reason, self.candidate, self.discard = reason, candidate, discard


class QAJob:
    JOB_HANDLERS = {'QA': 'qa'}

    llm = None        # AnthropicLLM, created on first use (tests inject a fake)
    searcher = None   # PostgresSearch by default (SQLite tests inject an in-memory equivalent)

    # ------------------------------------------------------------------ helpers ----
    def question_event(self, tx, kind, payload, sid, qid, generation_id=None, sequence=None):
        """ws_events row with question_id (and generation_id · sequence for answer.delta) so backend can pick the asker
        (question.progress · answer.*) or everyone (public_question.updated) and re-read the latest state (7.3.10)."""
        row = tx.one("""INSERT INTO ws_events(event_type,payload,session_id,question_id,generation_id,sequence,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING event_id""",
            (kind, dumps(payload), sid, qid, generation_id, sequence, self.service.clock() + self._event_ttl()))
        tx.notify(row['event_id'])

    @staticmethod
    def _event_ttl():
        from datetime import timedelta
        return timedelta(hours=24)

    def public_payload(self, q):
        return {'question_id': str(q['question_id']), 'content': q['content'], 'status': q['status'],
                'document_id': str(q['document_id']) if q['document_id'] else None, 'page_no': q['page_no'] if q['document_id'] else None,
                'vote_count': q['vote_count'], 'version': q['version'], 'created_at': q['created_at']}

    def elapsed_ms(self, since):
        return max(0, int((self.service.clock() - dt(since)).total_seconds() * 1000))

    def serving(self, tx):
        """Current serving settings: ACTIVE deployment snapshot (copy of 7.4.21, written by SYNC / API-62 · 63). Before the
        first deployment the mode is CUSTOM and the LLM_ONLY values come from configuration (review A-4)."""
        snap = active_serving(tx) or {}   # one reader shared with the admin side (easyq_common.trainer)
        s = self.settings
        threshold = snap.get('llm_only_threshold')
        if isinstance(threshold, (int, float)) and 0 < threshold <= 1:
            threshold = threshold * 100   # 7.4.21 examples give τ_LLM as 0~1; the LLM score here is 0~100
        return {'mode': snap.get('mode') or 'CUSTOM',
                'llm_only_threshold': float(threshold) if isinstance(threshold, (int, float)) else float(s.llm_only_threshold),
                'llm_model_id': snap.get('llm_model_id') or s.llm_model,
                'llm_prompt_version': snap.get('llm_prompt_version') or s.llm_prompt_version}

    # ------------------------------------------------------------------ job ----
    def qa(self, job):
        started = self.qa_start(job)
        if started is None:
            return
        question, snapshot = started
        sid, qid = str(job['session_id'] or question['session_id']), str(question['question_id'])
        state = {'generation_id': None, 'sequence': 0, 'first_token': False}
        try:
            self.qa_pipeline(job, question, snapshot, state)
        except Escalate as exc:
            self.qa_escalate(job, qid, sid, exc.reason, snapshot, state, exc.candidate, exc.discard)
        except APIError as exc:
            if exc.code == 'LEASE_LOST':
                raise
            reason = 'BUDGET_EXCEEDED' if exc.code == 'BUDGET_EXCEEDED' else 'PROVIDER_FAILURE'
            if reason == 'PROVIDER_FAILURE':
                log_line('WARNING', 'question.generation_failed', 'GENERATION_FAILED', 'provider failure; question escalated',
                         {'job_id': str(job['job_id']), 'cause': exc.code})
            self.qa_escalate(job, qid, sid, reason, snapshot, state, None, 'PROVIDER_FAILURE')
        except Exception as exc:
            log_line('WARNING', 'question.generation_failed', 'PROVIDER_FAILURE', 'unexpected failure; question escalated',
                     {'job_id': str(job['job_id']), 'exception': type(exc).__name__})
            self.qa_escalate(job, qid, sid, 'PROVIDER_FAILURE', snapshot, state, None, 'PROVIDER_FAILURE')

    def qa_start(self, job):
        with self.db.transaction() as tx:
            self.active(tx, job)
            q = tx.one('SELECT * FROM questions WHERE question_id=%s FOR UPDATE', (job['target_id'],))
            lecture = tx.one('SELECT status FROM lecture_sessions WHERE session_id=%s', (q['session_id'],)) if q else None
            if (not q or q['deleted_at'] or q['content'] is None or q['status'] not in ('QUEUED', 'PROCESSING')
                    or not lecture or lecture['status'] not in ('LIVE', 'ENDED')):
                self.finish(tx, job, {'skipped': True})
                return None
            sid, qid = str(q['session_id']), str(q['question_id'])
            snapshot = {'retrieval_config_version': self.settings.retrieval_version, 'second_stage_escalated': False,
                        'progress_ms': self.elapsed_ms(q['created_at'])}
            if q['status'] == 'QUEUED':
                tx.execute("UPDATE questions SET status='PROCESSING',version=version+1 WHERE question_id=%s", (qid,))
                q.update(status='PROCESSING', version=q['version'] + 1)
                self.question_event(tx, 'question.updated', {'question_id': qid, 'status': 'PROCESSING', 'version': q['version']}, sid, qid)
            self.question_event(tx, 'question.progress', {'question_id': qid, 'stage': 'SEARCHING'}, sid, qid)
            return q, snapshot

    # ------------------------------------------------------------------ pipeline ----
    def qa_pipeline(self, job, question, snapshot, state):
        sid, qid, text = str(question['session_id']), str(question['question_id']), question['content']
        candidates = self.hybrid_candidates(job, sid, text)
        if not candidates:
            raise Escalate('INSUFFICIENT_EVIDENCE')
        snapshot['candidate_scores'] = [{'evidence_id': c['chunk_id'], 'score': None, 'rank': i + 1} for i, c in enumerate(candidates)]
        with self.db.transaction() as tx:
            serving = self.serving(tx)
        state['answer_model'] = serving['llm_model_id']   # answers use the serving model ID (7.4.21)
        if serving['mode'] == 'LLM_ONLY':
            snapshot.update(route='FIXED_LLM_ONLY', fallback_reason=None)
            selected = self.judge_llm_only(job, question, candidates, serving, snapshot)
        else:
            selected = self.judge_custom(job, question, candidates, snapshot)
            if selected is None:
                selected = self.judge_llm_only(job, question, candidates, serving, snapshot)
        self.generate_answer(job, question, selected, snapshot, state)

    def hybrid_candidates(self, job, sid, text):
        from retrieval.keyword import tokenize_keywords
        usage_id = reserve(self.service, job)   # query embedding: EMBEDDING row, same hard cap as indexing
        began = time.monotonic()
        try:
            embed = getattr(self.embedder, 'embed_query', None) or self.embedder.embed
            vector = check_vectors(embed([text]), 1)[0]
        except Exception:
            settle(self.service, usage_id, False, int((time.monotonic() - began) * 1000))
            raise APIError(503, 'PROVIDER_FAILURE') from None
        settle(self.service, usage_id, True, int((time.monotonic() - began) * 1000))
        searcher = self.searcher or PostgresSearch()
        tokens = tokenize_keywords(text)
        with self.db.transaction() as tx:
            self.active(tx, job)
            cfg = bm25_config()
            vector_ids = searcher.vector_ids(tx, sid, vector, cfg.vector_top_k)
            keyword_ids = searcher.keyword_ids(tx, sid, tokens, cfg.keyword_top_k)
            ids = rrf(vector_ids, keyword_ids, cfg.candidates)
            rows = []
            for cid in ids:
                row = tx.one("""SELECT chunk_id,document_id,source_type,source_key,page_no,start_ms,end_ms,content FROM knowledge_chunks
                    WHERE session_id=%s AND chunk_id=%s AND index_status='READY'""", (sid, cid))
                if row:
                    row['chunk_id'] = str(row['chunk_id'])
                    rows.append(row)
        return rows

    def judge_custom(self, job, question, candidates, snapshot):
        """7.4.26. Returns the selected chunks (≤ 5) to answer with, raises Escalate(LOW_CONFIDENCE), or returns None
        to fall back to LLM_ONLY for this question only (timeout > 800 ms, 611, any error, not configured)."""
        reason = None
        began = time.monotonic()
        data = None
        try:
            if not self.settings.training_api_base_url:
                raise JudgeUnavailable('NOT_CONFIGURED')
            if self.trainer is None:
                self.trainer = TrainerClient(self.settings)
            data = self.trainer.judge({'request_id': str(question['question_id']), 'question': question['content'],
                'candidates': [{'evidence_id': c['chunk_id'], 'evidence_type': c['source_type'], 'text': c['content']} for c in candidates],
                'top_k': TOP_K}, timeout=JUDGE_TIMEOUT_MS / 1000)
        except JudgeUnavailable as exc:
            reason = 'NOT_CONFIGURED' if exc.result_code == 'NOT_CONFIGURED' else ('MODEL_UNAVAILABLE' if exc.result_code == 611 else 'ERROR')
        except Exception as exc:
            reason = 'TIMEOUT' if 'timeout' in (type(exc).__name__ + str(exc)).lower() else 'ERROR'
        judge_ms = int((time.monotonic() - began) * 1000)
        snapshot['judge_ms'] = judge_ms
        by_id = {c['chunk_id']: c for c in candidates}
        if data is not None and judge_ms > JUDGE_TIMEOUT_MS:
            reason, data = 'TIMEOUT', None
        if data is not None:
            try:
                ranked = [{'evidence_id': str(r['evidence_id']), 'score': float(r['score'])} for r in data.get('ranked') or []]
                max_score, threshold = float(data['max_score']), float(data['threshold'])
                selected = [str(i) for i in data.get('selected') or [] if str(i) in by_id][:TOP_K]
                if data.get('decision') not in ('ANSWER', 'ESCALATE') or not all(r['evidence_id'] in by_id for r in ranked):
                    raise ValueError()
                if data['decision'] == 'ANSWER' and not selected:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                reason, data = 'ERROR', None
        if data is None:
            log_line('WARNING', 'question.judge_fallback', 'MODEL_UNAVAILABLE', 'judge inference unavailable; this question uses LLM_ONLY',
                     {'job_id': str(job['job_id']), 'reason': reason, 'judge_ms': judge_ms})
            snapshot.update(route='FALLBACK_LLM_ONLY', fallback_reason=reason)
            return None
        ranked.sort(key=lambda r: -r['score'])
        snapshot.update(mode='CUSTOM', route='CUSTOM', fallback_reason=None, model_version=data.get('model_version'),
                        threshold=threshold, max_score=max_score,
                        candidate_scores=[{**r, 'rank': i + 1} for i, r in enumerate(ranked)],
                        top_evidence_id=ranked[0]['evidence_id'] if ranked else None, selected_evidence_ids=selected)
        if data['decision'] != 'ANSWER':
            raise Escalate('LOW_CONFIDENCE', candidate='AUTO_ESCALATED')
        return [by_id[i] for i in selected]

    def judge_llm_only(self, job, question, candidates, serving, snapshot):
        """Sonnet judgment: 0~100 score and up to 5 evidence; τ_LLM or above answers with that evidence (6.1.3)."""
        snapshot.update(mode='LLM_ONLY', model_version=None, threshold=None, llm_model_id=serving['llm_model_id'],
                        llm_prompt_version=serving['llm_prompt_version'], llm_only_threshold=serving['llm_only_threshold'])
        prompt = 'Question: ' + question['content'] + '\n\nEvidence:\n' + '\n'.join(
            f'[{i + 1}] ({c["source_type"]}) {c["content"]}' for i, c in enumerate(candidates))
        began = time.monotonic()
        result = self.llm_call('JUDGE_LLM', serving['llm_model_id'], JUDGE_SYSTEM, prompt, 300, job, question)
        snapshot['judge_ms'] = snapshot.get('judge_ms', 0) + int((time.monotonic() - began) * 1000)
        try:
            match = re.search(r'\{.*\}', result['text'], re.S)
            parsed = json.loads(match.group(0))
            score = float(parsed['score'])
            numbers = [int(n) for n in parsed.get('evidence') or []]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise APIError(503, 'PROVIDER_FAILURE') from None
        chosen = list(dict.fromkeys(candidates[n - 1]['chunk_id'] for n in numbers if 1 <= n <= len(candidates)))[:TOP_K]
        snapshot.update(llm_score=score, selected_evidence_ids=chosen, top_evidence_id=chosen[0] if chosen else None)
        if score < serving['llm_only_threshold'] or not chosen:
            raise Escalate('LOW_CONFIDENCE')   # LLM_ONLY escalations never become candidates (3.3.4)
        by_id = {c['chunk_id']: c for c in candidates}
        return [by_id[i] for i in chosen]

    def llm_call(self, operation, model, system, prompt, max_tokens, job, question, on_text=None):
        if self.llm is None:
            self.llm = AnthropicLLM(self.settings)
        return call_llm(self.service, self.llm, operation, model, system, prompt, max_tokens, str(question['session_id']),
                        str(question['question_id']), str(job['job_id']), on_text)

    def context(self, tx, sid, chunk):
        """Neighbouring chunks (previous / next of the same document or utterance stream) for the answer prompt."""
        if chunk['source_type'] == 'DOCUMENT':
            rows = tx.all("""SELECT chunk_id,content FROM knowledge_chunks WHERE session_id=%s AND document_id=%s AND index_status='READY'
                ORDER BY page_no,source_key""", (sid, chunk['document_id']))
        else:
            rows = tx.all("""SELECT chunk_id,content FROM knowledge_chunks WHERE session_id=%s AND source_type='TRANSCRIPT'
                AND index_status='READY' ORDER BY start_ms""", (sid,))
        ids = [str(r['chunk_id']) for r in rows]
        if chunk['chunk_id'] not in ids:
            return '', ''
        i = ids.index(chunk['chunk_id'])
        before = rows[i - 1]['content'][-300:] if i > 0 else ''
        after = rows[i + 1]['content'][:300] if i + 1 < len(rows) else ''
        return before, after

    def generate_answer(self, job, question, selected, snapshot, state):
        sid, qid = str(question['session_id']), str(question['question_id'])
        with self.db.transaction() as tx:
            self.active(tx, job)
            parts = []
            for i, c in enumerate(selected):
                before, after = self.context(tx, sid, c)
                parts.append(f'[{i + 1}] ({location_label(c)})\n' + (f'(앞 문맥) {before}\n' if before else '') + c['content']
                             + (f'\n(뒤 문맥) {after}' if after else ''))
            state['generation_id'] = uid()
            self.question_event(tx, 'question.progress', {'question_id': qid, 'stage': 'WRITING'}, sid, qid)
        prompt = 'Question: ' + question['content'] + '\n\nEvidence:\n' + '\n\n'.join(parts)
        model = state.get('answer_model') or self.settings.llm_model
        stream = {'head': '', 'verdict': None, 'pending': ''}

        def flush():
            if not stream['pending']:
                return
            with self.db.transaction() as tx:
                self.active(tx, job)
                if not state['first_token']:
                    state['first_token'] = True
                    snapshot['first_token_ms'] = self.elapsed_ms(question['created_at'])
                    tx.execute('UPDATE questions SET first_token_at=%s WHERE question_id=%s AND first_token_at IS NULL',
                               (self.service.clock(), qid))
                self.question_event(tx, 'answer.delta', {'question_id': qid, 'generation_id': state['generation_id'],
                    'sequence': state['sequence'], 'text': stream['pending'], 'provisional': True}, sid, qid,
                    state['generation_id'], state['sequence'])
                state['sequence'] += 1
            stream['pending'] = ''

        def on_text(chunk):
            if stream['verdict'] is None:
                stream['head'] += chunk
                if '\n' not in stream['head']:
                    return True
                first, rest = stream['head'].split('\n', 1)
                stream['verdict'] = 'INSUFFICIENT' if first.strip().upper().startswith('INSUFFICIENT') else 'ANSWER'
                if stream['verdict'] == 'INSUFFICIENT':
                    return False   # stop the stream: first output says the evidence is insufficient
                chunk = rest if first.strip().upper().startswith('ANSWER') else stream['head']
            stream['pending'] += chunk
            if len(stream['pending']) >= DELTA_FLUSH_CHARS:
                flush()
            return True

        result = self.llm_call('ANSWER', model, ANSWER_SYSTEM, prompt, 1024, job, question, on_text)
        if stream['verdict'] is None:
            head = stream['head'].strip()
            stream['verdict'] = 'INSUFFICIENT' if head.upper().startswith('INSUFFICIENT') else 'ANSWER'
            stream['pending'] += head[len('ANSWER'):] if head.upper().startswith('ANSWER') else head
        if stream['verdict'] == 'INSUFFICIENT':
            snapshot['second_stage_escalated'] = True
            raise Escalate('SECOND_STAGE_INSUFFICIENT', candidate='SECOND_STAGE_INSUFFICIENT', discard='INSUFFICIENT_EVIDENCE')
        flush()
        body = result['text'].split('\n', 1)[1] if result['text'].lstrip().upper().startswith('ANSWER') and '\n' in result['text'] else result['text']
        sentences = split_sentences(body)[:MAX_SENTENCES]
        answer_text = ' '.join(sentences).strip()
        cited = sorted({int(n) for n in re.findall(r'\[(\d+)\]', answer_text)})
        valid = [n for n in cited if 1 <= n <= len(selected)]
        # Numbers outside the given list are removed from the text; at least one valid citation is required (6.1.4).
        answer_text = re.sub(r'\[(\d+)\]', lambda m: m.group(0) if int(m.group(1)) in valid else '', answer_text).strip()
        if not answer_text or not valid:
            log_line('WARNING', 'question.citation_invalid', 'GENERATION_FAILED', 'citation check failed; question escalated',
                     {'job_id': str(job['job_id']), 'cited': len(cited), 'valid': len(valid)})
            raise Escalate('PROVIDER_FAILURE', discard='CITATION_INVALID')
        self.qa_confirm(job, question, selected, valid, answer_text, result.get('model') or model, snapshot, state)

    # ------------------------------------------------------------------ results ----
    def clear_deltas(self, tx, sid, qid):
        tx.execute("DELETE FROM ws_events WHERE session_id=%s AND question_id=%s AND event_type='answer.delta'", (sid, qid))

    def qa_confirm(self, job, question, selected, citation_numbers, text, model, snapshot, state):
        sid, qid = str(question['session_id']), str(question['question_id'])
        with self.db.transaction() as tx:
            self.active(tx, job)
            q = tx.one('SELECT * FROM questions WHERE question_id=%s FOR UPDATE', (qid,))
            if not q or q['status'] != 'PROCESSING' or q['deleted_at']:
                self.clear_deltas(tx, sid, qid)
                self.finish(tx, job, {'skipped': True})
                return
            now = self.service.clock()
            snapshot['final_ms'] = self.elapsed_ms(q['created_at'])
            aid = uid()
            tx.execute("""INSERT INTO answers(answer_id,question_id,content,provider_model,status,created_at,session_id)
                VALUES(%s,%s,%s,%s,'ACTIVE',%s,%s)""", (aid, qid, text, model[:100], now, sid))
            for n in citation_numbers:
                c = selected[n - 1]
                tx.execute("""INSERT INTO answer_citations(answer_id,citation_no,chunk_id,source_type,location_label,page_no,start_ms,end_ms,
                    excerpt,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (aid, n, c['chunk_id'], c['source_type'], location_label(c), c['page_no'], c['start_ms'], c['end_ms'],
                     c['content'][:500], sid))
            version_id = None
            if snapshot.get('route') == 'CUSTOM' and snapshot.get('model_version'):
                mv = tx.one('SELECT model_version_id FROM model_versions WHERE version_name=%s', (snapshot['model_version'],))
                version_id = mv['model_version_id'] if mv else None
            score = snapshot.get('max_score') if snapshot.get('route') == 'CUSTOM' else None
            tx.execute("""UPDATE questions SET status='AI_ANSWERED',escalation_reason=NULL,finalized_at=%s,decision_snapshot=%s,
                decision_score=%s,model_version_id=%s,version=version+1 WHERE question_id=%s""",
                (now, dumps(snapshot), min(1.0, max(0.0, score)) if score is not None else None, version_id, qid))
            self.clear_deltas(tx, sid, qid)
            self.question_event(tx, 'answer.completed', {'question_id': qid, 'answer_id': aid}, sid, qid)
            self.question_event(tx, 'question.updated', {'question_id': qid, 'status': 'AI_ANSWERED', 'version': q['version'] + 1}, sid, qid)
            self.finish(tx, job, {'status': 'AI_ANSWERED', 'route': snapshot.get('route')})

    def qa_escalate(self, job, qid, sid, reason, snapshot, state, candidate=None, discard=None):
        with self.db.transaction() as tx:
            self.active(tx, job)
            q = tx.one('SELECT * FROM questions WHERE question_id=%s FOR UPDATE', (qid,))
            if not q or q['status'] != 'PROCESSING' or q['deleted_at']:
                self.clear_deltas(tx, sid, qid)
                self.finish(tx, job, {'skipped': True})
                return
            now = self.service.clock()
            snapshot['final_ms'] = self.elapsed_ms(q['created_at'])
            score = snapshot.get('max_score') if snapshot.get('route') == 'CUSTOM' else None
            version_id = None
            if snapshot.get('route') == 'CUSTOM' and snapshot.get('model_version'):
                mv = tx.one('SELECT model_version_id FROM model_versions WHERE version_name=%s', (snapshot['model_version'],))
                version_id = mv['model_version_id'] if mv else None
            tx.execute("""UPDATE questions SET status='ESCALATED',escalation_reason=%s,finalized_at=%s,decision_snapshot=%s,
                decision_score=%s,model_version_id=%s,version=version+1 WHERE question_id=%s""",
                (reason, now, dumps(snapshot), min(1.0, max(0.0, score)) if score is not None else None, version_id, qid))
            self.clear_deltas(tx, sid, qid)
            if state.get('generation_id') and discard:
                self.question_event(tx, 'answer.discarded', {'question_id': qid, 'generation_id': state['generation_id'],
                                                             'reason': discard}, sid, qid)
            q.update(status='ESCALATED', version=q['version'] + 1)
            self.question_event(tx, 'question.updated', {'question_id': qid, 'status': 'ESCALATED', 'version': q['version']}, sid, qid)
            self.question_event(tx, 'public_question.updated', self.public_payload(q), sid, qid)
            if candidate:
                create_candidate(self.service, tx, candidate, qid, sid)
            self.finish(tx, job, {'status': 'ESCALATED', 'escalation_reason': reason, 'route': snapshot.get('route')})
