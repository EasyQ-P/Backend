"""Calls to auth internal APIs (7.3.11 INT-01 · 02 · 03) from backend and worker."""
import json
import urllib.request

from easyq_common.contracts import dumps


class AuthClient:
    def __init__(self, settings, worker=False):
        self.settings = settings
        self.key = settings.worker_internal_key if worker else settings.internal_key

    def call(self, path, body):
        request = urllib.request.Request(self.settings.auth_url.rstrip('/') + '/internal/v1/' + path,
            data=dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Service-Key': self.key}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)['data']

    def issue(self, pid, request_id):
        return self.call('guest-sessions', {'participant_id': pid, 'request_id': request_id})

    def revoke(self, body):
        return self.call('session-revocations', body)

    def renew(self, auth_session_id, session_id, request_id):
        return self.call('host-session-renewals', {'auth_session_id': auth_session_id, 'session_id': session_id, 'request_id': request_id})
