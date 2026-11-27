"""Judge-model lease (6.4.14, agreed with the AI team 2026-11-27).

While any lecture is LIVE, or a DRAFT lecture already has audience waiting (just before it starts), the worker calls
POST /judge-model/keepalive about once a minute. The training server loads the fp16 judge model if needed and unloads it
5 minutes after the last call, so overlapping lectures and missed calls are both safe. There is no separate unload call.
While the model is not loaded, 7.4.26 answers 611 at once and the question is judged by LLM_ONLY (judge adapter, v0.5).
"""
from datetime import datetime, timezone
import json
import sys
import urllib.request

from easyq_common.contracts import dumps


class TrainerClient:
    """Calls only the fixed training-server API (no free URL/command/path input). The API key is never logged."""

    def __init__(self, settings):
        self.base = settings.training_api_base_url.rstrip('/')
        self.key = settings.training_api_key

    def keepalive(self):
        request = urllib.request.Request(self.base + '/judge-model/keepalive', data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json', 'X-API-Key': self.key})
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.loads(response.read() or b'{}')
        if body.get('result_code') != 200:
            raise RuntimeError('keepalive result_code %s' % body.get('result_code'))
        return body.get('data') or {}

    def judge(self, payload, timeout=0.8):
        """7.4.26 POST /judgments. Returns data on result_code 200; any other code (611: not loaded / LLM_ONLY mode)
        raises JudgeUnavailable with that code so the caller can judge this question by LLM_ONLY."""
        request = urllib.request.Request(self.base + '/judgments', data=dumps(payload).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', 'X-API-Key': self.key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read() or b'{}')
        if body.get('result_code') != 200:
            raise JudgeUnavailable(body.get('result_code'))
        return body.get('data') or {}


class JudgeUnavailable(Exception):
    def __init__(self, result_code):
        super().__init__('judge result_code %s' % result_code)
        self.result_code = result_code


class JudgeModelKeepalive:
    trainer = None

    def judge_model_needed(self):
        with self.db.transaction() as tx:
            row = tx.one("""SELECT 1 AS needed FROM lecture_sessions s WHERE s.status='LIVE'
                OR (s.status='DRAFT' AND EXISTS (SELECT 1 FROM participants p WHERE p.session_id=s.session_id AND p.status='ACTIVE'))
                LIMIT 1""")
        return bool(row)

    def keep_judge_model_warm(self):
        """Return True when a keepalive was sent. Failures are logged and swallowed: questions fall back to LLM_ONLY."""
        if not self.settings.training_api_base_url or not self.judge_model_needed():
            return False
        if self.trainer is None:
            self.trainer = TrainerClient(self.settings)
        try:
            self.trainer.keepalive()
            return True
        except Exception as exc:
            line = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': 'WARNING', 'service': 'worker',
                    'event': 'judge_model.keepalive_failed', 'request_id': None, 'trace_id': None, 'code': 'MODEL_UNAVAILABLE',
                    'message': 'judge model keepalive failed; questions use LLM_ONLY until the model is loaded',
                    'meta': {'exception': type(exc).__name__}}
            print(dumps(line), file=sys.stderr, flush=True)
            return False
