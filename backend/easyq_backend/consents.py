"""Consent changes (API-11 · 20) and their side effects."""

from easyq_common.contracts import (APIError, Result, consent, fields)


class ConsentsMixin:
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
                    # v1.4: a LIVE lecture ends (end_reason HOST, no If-Match); a DRAFT stays, and API-16 refuses to
                    # start it with 403 CONSENT_REQUIRED until SERVICE is granted again.
                    if s['status'] == 'LIVE':
                        self.end(tx, s, 'HOST')
                else:
                    g = self.participant(tx, sid, p)
                    tx.execute("UPDATE participants SET status='REVOKED' WHERE participant_id=%s", (g['participant_id'],))
                    if not user:
                        self.job(tx, 'AUTH_REVOKE', str(g['participant_id']), 'consent-revoke:' + data['consent_id'], {'participant_id': str(g['participant_id']), 'reason': 'CONSENT_WITHDRAWN'}, sid=sid)
                self.event(tx, 'session.updated', {'participant_count': self.participant_count(tx, sid)}, sid)
        return Result(201, data)
