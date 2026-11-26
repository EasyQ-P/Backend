"""CLEANUP (TRAINING consent withdrawal): remove unmerged label candidates."""

from easyq_common.contracts import APIError, obj



class CleanupJob:
    def cleanup(self, tx, job):
        payload = obj(job['payload'])
        if payload.get('scope') != 'TRAINING':
            raise APIError(422, 'JOB_UNSUPPORTED')
        # Delete only unmigrated candidates. MERGING requires AI-side reconciliation in a later milestone.
        sql = "SELECT c.candidate_id,c.review_status FROM feedback_candidates c JOIN candidate_provenance p ON p.candidate_id=c.candidate_id WHERE "
        if payload.get('user_id'):
            sql += """(p.actor_user_id=%s OR p.session_id IN (SELECT session_id FROM lecture_sessions WHERE host_user_id=%s)
                OR p.question_id IN (SELECT q.question_id FROM questions q JOIN participants a ON a.participant_id=q.participant_id WHERE a.user_id=%s))"""
            args = [payload['user_id']] * 3
        else:
            sql += 'p.actor_participant_id=%s'
            args = [payload['participant_id']]
        if payload.get('session_id'):
            sql += ' AND p.session_id=%s'
            args.append(payload['session_id'])
        candidates = tx.all(sql, tuple(args))
        if any(c['review_status'] == 'MERGING' for c in candidates):
            raise APIError(409, 'MERGE_RECONCILIATION_REQUIRED')
        for c in candidates:
            tx.execute('DELETE FROM feedback_candidates WHERE candidate_id=%s', (c['candidate_id'],))
