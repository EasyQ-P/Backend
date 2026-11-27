"""AUTO_END, FINALIZE and SUMMARY jobs (6.2.9 · 6.2.11 · 6.2.14 · 8.2.28).

FINALIZE (created by ServiceCore.end): give in-flight questions and the last utterance a short grace period, then
escalate what is left (FINALIZE_TIMEOUT) and cancel its QA job (CANCELLED + new lease_token, so a late answer cannot
overwrite the escalation), freeze vote counts, store the lecture statistics once, create deferred label candidates,
remove guest nicknames and the content of guest private questions that cannot become label candidates, make sure every
guest has a session revocation (AUTH_REVOKE), and create the one SUMMARY job.
"""
from datetime import timedelta
import json
import re

from easyq_common.contracts import APIError, dt, dumps, obj, uid

from easyq_common.candidates import eligible, location_label, sweep_candidates
from .qa import AnthropicLLM, log_line

FINALIZE_GRACE = timedelta(seconds=60)   # S-6: the design gives no number; QA normally finishes within seconds
SUMMARY_MIN_CHARS = 1000
SUMMARY_TRANSCRIPT_CHARS = 150_000
SUMMARY_DOCUMENT_CHARS = 50_000

SUMMARY_SYSTEM = ('You write a Korean study summary of a lecture from numbered sources (transcript and slides). Reply with JSON only: '
                  '{"one_line": string, "key_concepts": [{"term": string, "description": string}], "emphases": [string], '
                  '"keywords": [string], "sources": [<numbers of the sources you used>]}. Use only the sources.')


class FinalizeJob:
    JOB_HANDLERS = {'AUTO_END': 'auto_end', 'FINALIZE': 'finalize', 'SUMMARY': 'summary'}

    # ------------------------------------------------------------------ AUTO_END ----
    def auto_end(self, job):
        """12 hours after LIVE started (available_at): end with AUTO_TIMEOUT; no-op if the lecture already ended."""
        with self.db.transaction() as tx:
            self.active(tx, job)
            s = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (job['session_id'],))
            if s and s['status'] == 'LIVE':
                self.service.end(tx, s, 'AUTO_TIMEOUT')
                self.service.event(tx, 'session.updated', {'session_id': str(s['session_id']), 'status': 'ENDED', 'end_reason': 'AUTO_TIMEOUT',
                    'stt_status': 'STOPPED', 'version': s['version'] + 1}, str(job['session_id']))
            self.finish(tx, job, {'ended': bool(s and s['status'] == 'LIVE')})

    # ------------------------------------------------------------------ FINALIZE ----
    def finalize(self, job):
        sid = str(job['target_id'])
        with self.db.transaction() as tx:
            self.active(tx, job)
            lecture = tx.one('SELECT * FROM lecture_sessions WHERE session_id=%s FOR UPDATE', (sid,))
            if not lecture or lecture['status'] != 'ENDED':
                self.finish(tx, job, {'skipped': True})
                return
            now = self.service.clock()
            deadline = dt(lecture['ended_at'] or now) + FINALIZE_GRACE
            busy = tx.one("""SELECT count(*) AS n FROM jobs WHERE session_id=%s AND job_type IN ('QA','TRANSCRIPT_INDEX')
                AND status IN ('QUEUED','RUNNING')""", (sid,))['n']
            if busy and now < deadline:
                # Wait inside the grace period: put this job back without spending an attempt.
                tx.execute("""UPDATE jobs SET status='QUEUED',available_at=%s,lease_until=NULL,attempts=attempts-1
                    WHERE job_id=%s AND lease_token=%s""", (min(deadline, now + timedelta(seconds=5)), job['job_id'], job['lease_token']))
                return
            escalated = self.finalize_questions(tx, sid, now)
            self.freeze_votes(tx, sid)
            if lecture['statistics'] is None:
                tx.execute('UPDATE lecture_sessions SET statistics=%s WHERE session_id=%s', (dumps(self.statistics(tx, sid, now)), sid))
            created = sweep_candidates(self.service, tx, sid)
            purged = self.purge_guest_content(tx, sid, lecture, now)
            tx.execute("UPDATE participants SET nickname=NULL WHERE session_id=%s AND kind='GUEST'", (sid,))
            for g in tx.all("SELECT participant_id FROM participants WHERE session_id=%s AND kind='GUEST'", (sid,)):
                pid = str(g['participant_id'])
                if not tx.one('SELECT 1 AS found FROM jobs WHERE idempotency_key=%s', ('end-revoke:' + pid,)):
                    self.service.job(tx, 'AUTH_REVOKE', pid, 'end-revoke:' + pid, {'participant_id': pid, 'reason': 'LECTURE_ENDED'}, sid=sid)
            summary = tx.one('SELECT summary_id FROM summary_notes WHERE session_id=%s', (sid,))
            if not summary:
                summary_id = uid()
                tx.execute("INSERT INTO summary_notes(summary_id,session_id,status,source_refs) VALUES(%s,%s,'QUEUED','[]')", (summary_id, sid))
                self.service.job(tx, 'SUMMARY', summary_id, 'summary:' + sid, {}, user=str(lecture['host_user_id']) if lecture['host_user_id'] else None, sid=sid)
                self.service.event(tx, 'summary.updated', {'summary_id': summary_id, 'status': 'QUEUED'}, sid)
            self.finish(tx, job, {'escalated': escalated, 'candidates': created, 'guest_questions_purged': purged})

    def finalize_questions(self, tx, sid, now):
        count = 0
        for q in tx.all("""SELECT * FROM questions WHERE session_id=%s AND status IN ('QUEUED','PROCESSING') AND deleted_at IS NULL""", (sid,)):
            qid = str(q['question_id'])
            # 8.2.28: cancel and re-token the QA job so its late result fails the lease check.
            tx.execute("""UPDATE jobs SET status='CANCELLED',lease_token=%s,lease_until=NULL,finished_at=%s
                WHERE job_type='QA' AND target_id=%s AND status IN ('QUEUED','RUNNING','HELD')""", (uid(), now, qid))
            snapshot = obj(q['decision_snapshot'] or '{}')
            snapshot['final_ms'] = max(0, int((now - dt(q['created_at'])).total_seconds() * 1000))
            tx.execute("""UPDATE questions SET status='ESCALATED',escalation_reason='FINALIZE_TIMEOUT',finalized_at=%s,decision_snapshot=%s,
                version=version+1 WHERE question_id=%s""", (now, dumps(snapshot), qid))
            delta = tx.one("""SELECT generation_id FROM ws_events WHERE session_id=%s AND question_id=%s AND event_type='answer.delta'
                ORDER BY event_id DESC LIMIT 1""", (sid, qid))
            tx.execute("DELETE FROM ws_events WHERE session_id=%s AND question_id=%s AND event_type='answer.delta'", (sid, qid))
            if delta and delta['generation_id']:
                self.question_event(tx, 'answer.discarded', {'question_id': qid, 'generation_id': str(delta['generation_id']),
                                                             'reason': 'FINALIZE_TIMEOUT'}, sid, qid)
            q.update(status='ESCALATED', version=q['version'] + 1)
            self.question_event(tx, 'question.updated', {'question_id': qid, 'status': 'ESCALATED', 'version': q['version']}, sid, qid)
            self.question_event(tx, 'public_question.updated', self.public_payload(q), sid, qid)
            count += 1
        return count

    def freeze_votes(self, tx, sid):
        """vote_count becomes part of the lecture record (8.2.16): settle it once from the vote rows."""
        tx.execute("""UPDATE questions SET vote_count=(SELECT count(*) FROM votes v WHERE v.session_id=questions.session_id
            AND v.question_id=questions.question_id) WHERE session_id=%s AND status IN ('ESCALATED','COMPLETED')""", (sid,))

    def statistics(self, tx, sid, now):
        """7.3.1 Statistics, counted once before guest content is removed (6.2.9-3)."""
        count = lambda sql: tx.one(sql, (sid,))['n']
        questions = count('SELECT count(*) AS n FROM questions WHERE session_id=%s AND deleted_at IS NULL')
        answers = count('SELECT count(*) AS n FROM answers WHERE session_id=%s')
        pages = tx.all("""SELECT q.document_id,d.file_name,q.page_no,count(*) AS n FROM questions q
            JOIN documents d ON d.session_id=q.session_id AND d.document_id=q.document_id
            WHERE q.session_id=%s AND q.deleted_at IS NULL AND q.page_no IS NOT NULL
            GROUP BY q.document_id,d.file_name,q.page_no ORDER BY d.file_name,q.page_no""", (sid,))
        with_page = sum(r['n'] for r in pages)
        return {'question_count': questions, 'ai_answer_count': answers,
                'escalated_count': count("SELECT count(*) AS n FROM questions WHERE session_id=%s AND deleted_at IS NULL AND status IN ('ESCALATED','COMPLETED')"),
                'completed_count': count("SELECT count(*) AS n FROM questions WHERE session_id=%s AND deleted_at IS NULL AND status='COMPLETED'"),
                'retracted_count': count("SELECT count(*) AS n FROM answers WHERE session_id=%s AND status='RETRACTED'"),
                'vote_total': int(count('SELECT coalesce(sum(vote_count),0) AS n FROM questions WHERE session_id=%s AND deleted_at IS NULL')),
                'participant_count': {'member': count("SELECT count(*) AS n FROM participants WHERE session_id=%s AND kind='MEMBER'"),
                                      'guest': count("SELECT count(*) AS n FROM participants WHERE session_id=%s AND kind='GUEST'")},
                'ai_answer_share': answers / questions if questions else None,
                'questions_by_page': [{'document_id': str(r['document_id']), 'file_name': r['file_name'], 'page_no': r['page_no'],
                                       'count': r['n']} for r in pages],
                'general_question_count': questions - with_page, 'computed_at': now}

    def purge_guest_content(self, tx, sid, lecture, now):
        """6.2.11-1 / 8.2.37: guest private questions that cannot become label candidates lose question text, answer text and
        citations; status and timing rows stay. While the evaluation check is PENDING the content is kept for now."""
        purged = 0
        rows = tx.all("""SELECT q.* FROM questions q JOIN participants p ON p.session_id=q.session_id AND p.participant_id=q.participant_id
            WHERE q.session_id=%s AND p.kind='GUEST' AND q.status='AI_ANSWERED' AND q.content IS NOT NULL""", (sid,))
        for q in rows:
            state, _ = eligible(self.service, tx, q, lecture)
            if state != 'NO':
                continue
            tx.execute('UPDATE questions SET content=NULL,content_purged_at=%s WHERE question_id=%s', (now, q['question_id']))
            for a in tx.all('SELECT answer_id FROM answers WHERE session_id=%s AND question_id=%s', (sid, q['question_id'])):
                tx.execute('DELETE FROM answer_citations WHERE session_id=%s AND answer_id=%s', (sid, a['answer_id']))
                tx.execute('UPDATE answers SET content=NULL WHERE answer_id=%s', (a['answer_id'],))
            purged += 1
        return purged

    # ------------------------------------------------------------------ SUMMARY ----
    def summary(self, job):
        with self.db.transaction() as tx:
            self.active(tx, job)
            note = tx.one('SELECT * FROM summary_notes WHERE summary_id=%s FOR UPDATE', (job['target_id'],))
            if not note or note['status'] in ('READY', 'SKIPPED'):
                self.finish(tx, job, {'skipped': True})
                return
            sid = str(note['session_id'])
            transcript = tx.all("""SELECT chunk_id,source_type,page_no,start_ms,end_ms,content FROM knowledge_chunks
                WHERE session_id=%s AND source_type='TRANSCRIPT' ORDER BY start_ms""", (sid,))
            if sum(len(c['content']) for c in transcript) < SUMMARY_MIN_CHARS:
                tx.execute("UPDATE summary_notes SET status='SKIPPED',content=NULL,generated_at=%s WHERE summary_id=%s",
                           (self.service.clock(), note['summary_id']))
                self.service.event(tx, 'summary.updated', {'summary_id': str(note['summary_id']), 'status': 'SKIPPED'}, sid)
                self.finish(tx, job, {'status': 'SKIPPED'})
                return
            documents = tx.all("""SELECT chunk_id,source_type,page_no,start_ms,end_ms,content FROM knowledge_chunks
                WHERE session_id=%s AND source_type='DOCUMENT' AND index_status='READY' ORDER BY document_id,page_no,source_key""", (sid,))
            model = self.serving(tx)['llm_model_id']
            self.summary_status(tx, note, 'RUNNING')
        sources, used = [], 0
        for limit, rows in ((SUMMARY_TRANSCRIPT_CHARS, transcript), (SUMMARY_DOCUMENT_CHARS, documents)):
            used = 0
            for c in rows:
                if used + len(c['content']) > limit:
                    break
                used += len(c['content'])
                sources.append(c)
        prompt = '\n\n'.join(f'[{i + 1}] ({location_label(c)}) {c["content"]}' for i, c in enumerate(sources))
        try:
            if self.llm is None:
                self.llm = AnthropicLLM(self.settings)
            from .llm import call_llm
            result = call_llm(self.service, self.llm, 'SUMMARY', model, SUMMARY_SYSTEM, prompt, 4096, sid, None, str(job['job_id']))
            content, refs = self.parse_summary(result['text'], sources)
        except APIError as exc:
            if exc.code == 'LEASE_LOST':
                raise
            self.summary_failed(job, note, exc.code)
            raise
        except Exception:
            self.summary_failed(job, note, 'PROVIDER_FAILURE')
            raise
        with self.db.transaction() as tx:
            self.active(tx, job)
            tx.execute("""UPDATE summary_notes SET status='READY',content=%s,source_refs=%s,provider_model=%s,generated_at=%s
                WHERE summary_id=%s AND status='RUNNING'""",
                (dumps(content), dumps(refs), (result.get('model') or model)[:100], self.service.clock(), note['summary_id']))
            self.service.event(tx, 'summary.updated', {'summary_id': str(note['summary_id']), 'status': 'READY'}, sid)
            self.finish(tx, job, {'status': 'READY', 'source_count': len(refs)})

    def summary_status(self, tx, note, status):
        tx.execute('UPDATE summary_notes SET status=%s WHERE summary_id=%s', (status, note['summary_id']))
        self.service.event(tx, 'summary.updated', {'summary_id': str(note['summary_id']), 'status': status}, str(note['session_id']))

    def summary_failed(self, job, note, code):
        """Budget hold → QUEUED (6.3.5-2); last attempt → FAILED; otherwise QUEUED for the runner's retry."""
        final = code != 'BUDGET_EXCEEDED' and job['attempts'] >= self.MAX_ATTEMPTS
        with self.db.transaction() as tx:
            try:
                self.active(tx, job)
            except APIError:
                return
            self.summary_status(tx, note, 'FAILED' if final else 'QUEUED')
        if code != 'BUDGET_EXCEEDED':
            log_line('WARNING', 'summary.generation_failed', 'GENERATION_FAILED', 'summary generation failed',
                     {'job_id': str(job['job_id']), 'final': final, 'cause': code})

    @staticmethod
    def parse_summary(text, sources):
        """8.2.19 content shape; cited source numbers are checked in code and at least one must be valid."""
        try:
            data = json.loads(re.search(r'\{.*\}', text, re.S).group(0))
            content = {'one_line': str(data['one_line']).strip(),
                       'key_concepts': [{'term': str(k['term']).strip(), 'description': str(k['description']).strip()} for k in data.get('key_concepts') or []],
                       'emphases': [str(e).strip() for e in data.get('emphases') or []],
                       'keywords': [str(k).strip() for k in data.get('keywords') or []]}
            numbers = sorted({int(n) for n in data.get('sources') or []})
        except (AttributeError, KeyError, TypeError, ValueError):
            raise APIError(503, 'PROVIDER_FAILURE') from None
        valid = [n for n in numbers if 1 <= n <= len(sources)]
        if not content['one_line'] or not valid:
            raise APIError(503, 'PROVIDER_FAILURE')
        refs = []
        for n in valid:
            c = sources[n - 1]
            refs.append({'citation_no': n, 'chunk_id': str(c['chunk_id']), 'source_type': c['source_type'], 'location_label': location_label(c),
                         'page_no': c['page_no'], 'start_ms': c['start_ms'], 'end_ms': c['end_ms'], 'excerpt': c['content'][:300]})
        return content, refs
