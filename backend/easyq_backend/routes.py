"""backend REST routing (after ServiceCore.route has done health, Origin and auth/backend split)."""
import re

from easyq_common.contracts import (APIError, Result, fields,
                                    obj, uuid_value)


class RoutesMixin:
    def backend_route(self, tx, req, path):
        method, b = req.method, req.body
        if path.startswith('/join-codes/') and method == 'GET':
            s = self.join_lookup(tx, path.split('/')[-1], req.ip)
            p = self.entry_principal(tx, req)
            data = {k: s[k] for k in ['title', 'host_display_name', 'scheduled_at', 'description', 'status']}
            count = self.participant_count(tx, s['session_id'])
            data.update(participant_count=count, capacity=50, is_full=count >= 50, already_joined=bool(p and self.participant(tx, s['session_id'], p)))
            return Result(200, data)
        p = self.entry_principal(tx, req) if path == '/participations' else self.principal(tx, req)
        if method not in ('GET', 'HEAD') and p and path != '/participations':
            self.csrf(req, p)
        if path == '/participations' and method == 'POST':
            return self.join(tx, req, p)
        if path == '/sessions' and method == 'POST':
            return self.create_lecture(tx, req, p)
        if path == '/me/consents' and method == 'POST':
            return self.change_consent(tx, req, p)
        match = re.fullmatch(r'/sessions/([^/]+)(.*)', path)
        if match:
            sid, tail = uuid_value(match[1]), match[2]
            if not tail and method == 'GET':
                s = self.lecture(tx, sid, p)
                return Result(200, self.session_public(tx, s, p), {'ETag': f'"{s["version"]}"'})
            if not tail and method == 'PATCH':
                return self.patch_lecture(tx, req, p, sid)
            if tail == '/join-links' and method == 'POST':
                fields(b, [])
                s = self.lecture(tx, sid, p, True)
                self.version(req, s)
                if s['status'] not in ('DRAFT', 'LIVE'):
                    raise APIError(409, 'SESSION_STATE_CONFLICT')
                code = self.code(tx)
                tx.execute('UPDATE lecture_sessions SET previous_join_code=join_code,join_code=%s,version=version+1 WHERE session_id=%s', (code, sid))
                self.event(tx, 'session.updated', {'version': s['version'] + 1}, sid)
                return Result(201, {'join_code': code, 'join_url': self.settings.origin + '/join?code=' + code, 'version': s['version'] + 1}, {'ETag': f'"{s["version"] + 1}"'})
            if tail == '/consents' and method == 'POST':
                return self.change_consent(tx, req, p, sid)
            if tail == '/documents' and method == 'POST':
                return self.upload(tx, req, p, sid)
            if tail == '/documents' and method == 'GET':
                self.lecture(tx, sid, p, True)
                rows = tx.all('SELECT document_id,file_name,status,stage,page_count,chunk_count,error_code FROM documents WHERE session_id=%s ORDER BY created_at,document_id', (sid,))
                for r in rows:
                    r['retryable'] = self.document_retryable(r)
                return Result(200, {'items': rows, 'next_cursor': None, 'total_count': len(rows)})
            retry = re.fullmatch(r'/documents/([^/]+)/retries', tail)
            if retry and method == 'POST':
                return self.retry_document(tx, req, p, sid, uuid_value(retry[1]))
            if tail == '/terms' and method == 'PUT':
                return self.save_terms(tx, req, p, sid)
        if path == '/sessions' and method == 'GET':
            return self.list_lectures(tx, req, p)
        if path.startswith('/jobs/') and method == 'GET':
            jid = uuid_value(path.split('/')[-1])
            job = tx.one('SELECT * FROM jobs WHERE job_id=%s', (jid,))
            if not job or not p['user_id'] or str(job['requested_by']) != str(p['user_id']):
                raise APIError(404, 'RESOURCE_NOT_FOUND')
            # 7.3.1 Job. result never carries document text or credentials (worker stores counts only).
            return Result(200, {k: job[k] for k in ['job_id', 'job_type', 'status', 'attempts', 'error_code', 'finished_at']}
                          | {'resource_id': job['target_id'], 'result': obj(job['result']) if job.get('result') is not None else None})
        raise APIError(404, 'RESOURCE_NOT_FOUND')
