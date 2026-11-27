"""Summary note and lecture statistics (API-37 · 39). Both are lecture records readable after the lecture ends."""
from easyq_common.contracts import APIError, Result, obj

from .questions import citation_view


class ReportsMixin:
    ROUTES = [
        ('GET', r'/sessions/(?P<sid>[^/]+)/summary', 'get_summary'),
        ('GET', r'/sessions/(?P<sid>[^/]+)/statistics', 'get_statistics'),
    ]

    # ---- API-37 요약 노트 ---------------------------------------------------------------------
    def get_summary(self, tx, req, p, sid):
        s, owner, part = self.question_access(tx, sid, p)
        if not owner and not p['user_id']:
            raise APIError(403, 'FORBIDDEN')   # guests have no summary (7.3.5)
        if s['status'] not in ('ENDED', 'PURGED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        row = tx.one('SELECT * FROM summary_notes WHERE session_id=%s', (sid,))
        if not row:
            # FINALIZE has not created the row yet: report "being generated" without inventing an ID.
            return Result(200, {'summary_id': None, 'status': 'QUEUED', 'content': None, 'sources': [], 'generated_at': None})
        ready = row['status'] == 'READY'
        refs = obj(row['source_refs']) or [] if ready else []
        sources = [citation_view(dict(r, citation_no=r.get('citation_no', i + 1))) for i, r in enumerate(refs)]
        return Result(200, {'summary_id': str(row['summary_id']), 'status': row['status'],
                            'content': obj(row['content']) if ready else None, 'sources': sources,
                            'generated_at': row['generated_at']})

    # ---- API-39 강연 통계 ---------------------------------------------------------------------
    def get_statistics(self, tx, req, p, sid):
        s, owner, _ = self.question_access(tx, sid, p)
        if not owner:
            raise APIError(403, 'FORBIDDEN')
        if s['status'] not in ('ENDED', 'PURGED'):
            raise APIError(409, 'SESSION_STATE_CONFLICT')
        # The value FINALIZE stored (6.2.9); until it runs, compute the same figures without storing them.
        stored = obj(s['statistics'])
        return Result(200, stored if stored else self.lecture_statistics(tx, sid))


OPENAPI = [
    ('GET', '/sessions/{sid}/summary', 'API-37', '요약 노트'),
    ('GET', '/sessions/{sid}/statistics', 'API-39', '강연 통계'),
]
