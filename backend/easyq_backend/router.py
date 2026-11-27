"""Route table shared by the backend mixins.

Each mixin declares ROUTES = [(method, pattern, handler_name[, access]), ...]. pattern is a regex over the path
after /api/v1 with named groups (sid, qid, did, chunk_id, job_id, user_id, notice_id, inquiry_id, cid, run_id);
the handler is called as handler(tx, req, p, **groups) with UUID groups validated. p is the current principal.
access 'auth' (default): login required (401 otherwise) and X-CSRF-Token checked for non-GET methods.
access 'public': p may be None and CSRF is not checked (the handler decides).
"""
import re

from easyq_common.contracts import uuid_value

UUID_GROUPS = {'sid', 'qid', 'did', 'chunk_id', 'job_id', 'user_id', 'notice_id', 'inquiry_id', 'cid', 'run_id'}


def collect_routes(cls):
    table = []
    for klass in reversed(cls.__mro__):
        for method, pattern, handler, *rest in klass.__dict__.get('ROUTES', ()):
            table.append((method, re.compile(pattern), handler, rest[0] if rest else 'auth'))
    return table


def dispatch(service, tx, req, path):
    """Return a Result, or None when no table route matches (the caller then answers 404)."""
    table = service.__class__.__dict__.get('_route_table')
    if table is None:
        table = collect_routes(service.__class__)
        setattr(service.__class__, '_route_table', table)
    for method, pattern, handler, access in table:
        if method != req.method:
            continue
        match = pattern.fullmatch(path)
        if not match:
            continue
        groups = {k: (uuid_value(v) if k in UUID_GROUPS else v) for k, v in match.groupdict().items()}
        p = service.principal(tx, req, optional=(access == 'public'))
        if req.method not in ('GET', 'HEAD') and p and access != 'public':
            service.csrf(req, p)
        return getattr(service, handler)(tx, req, p, **groups)
    return None  # the v0.3 routes in routes.py, then 404
