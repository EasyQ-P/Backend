"""Fixed training-server adapter (design 7.4) shared by backend and worker.

Only the APIs listed for the service in 7.4 (table 7.16 "호출하는 쪽" ① ②) are exposed as methods; there is no generic
request method, so no free URL, command or file path can be sent. Path parameters are checked against the version-name
patterns of 7.4 (ds-vN, judge-vN, train-NNNN, eval-NNNN, ...) before they are put in a URL.

Responses use the common envelope {result_code, result_msg, data}. Several 7.4 examples show the bare data object
(review A-15), so a 2xx body without result_code is accepted as data. 6xx codes become TrainerError with the
service code of 8.4.3. Network failures and timeouts become TrainerUnavailable. The X-API-Key value is never logged
or put in an exception message.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# 8.4.3 training-server code -> 8.4.2 service code.
SERVICE_CODES = {
    600: 'INTERNAL_ERROR', 601: 'DATASET_NOT_READY', 602: 'INTERNAL_ERROR', 603: 'VALIDATION_ERROR',
    604: 'TRAINING_FAILED', 605: 'DEPLOYMENT_FAILED', 606: 'TRAINING_FAILED', 607: 'TRAINING_FAILED',
    608: 'TRAINING_ALREADY_FINISHED', 609: 'TRAINER_BUSY', 610: 'MODEL_NOT_DEPLOYABLE', 611: 'MODEL_UNAVAILABLE',
    612: 'INTERNAL_ERROR', 613: 'RESOURCE_NOT_FOUND', 614: 'DEPLOY_CRITERIA_NOT_MET', 615: 'TRAINING_HELD',
}

# 7.4 version names. Anything else is refused before a URL is built.
NAME_PATTERNS = {
    'dataset': r'ds-v[0-9]{1,6}',
    'model': r'judge-v[0-9]{1,6}',
    'train_job': r'train-[0-9]{1,8}',
    'evaluation': r'eval-[0-9]{1,8}',
    'job': r'(train|eval|export|collect)-[0-9]{1,8}',
}


def service_code(code):
    """Map an 8.4.3 code (int or numeric string) to the 8.4.2 service code; unknown values give None."""
    try:
        return SERVICE_CODES.get(int(code))
    except (TypeError, ValueError):
        return None


class TrainerError(Exception):
    """The training server answered with a 6xx result_code."""

    def __init__(self, result_code, http_status=None):
        super().__init__('training server result_code %s' % result_code)
        self.result_code = result_code
        self.http_status = http_status
        self.code = service_code(result_code) or 'INTERNAL_ERROR'


class TrainerUnavailable(Exception):
    """No usable answer (not configured, tunnel down, timeout, malformed body). Whether a write was applied is unknown."""


def checked_name(kind, value):
    if not isinstance(value, str) or not re.fullmatch(NAME_PATTERNS[kind], value):
        raise ValueError('invalid %s name' % kind)
    return urllib.parse.quote(value, safe='')


class TrainerClient:
    """7.4 adapter. transport(method, url, headers, body_bytes, timeout) -> (http_status, body_bytes) can be injected."""

    TIMEOUT = 10
    KEEPALIVE_TIMEOUT = 3

    def __init__(self, settings, transport=None):
        self.base = (settings.training_api_base_url or '').rstrip('/')
        self._key = settings.training_api_key
        self.transport = transport or self._urllib

    def __repr__(self):
        return '<TrainerClient>'   # never show the key

    @staticmethod
    def _urllib(method, url, headers, body, timeout):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:   # 4xx/5xx still carry the envelope
            return exc.code, exc.read()

    def _call(self, method, path, body=None, query=None, timeout=None):
        if not self.base:
            raise TrainerUnavailable('training server not configured')
        url = self.base + path
        if query:
            url += '?' + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {'X-API-Key': self._key, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        try:
            status, raw = self.transport(method, url, headers, data, timeout or self.TIMEOUT)
        except (OSError, ValueError) as exc:   # URLError, timeout, connection reset
            raise TrainerUnavailable(type(exc).__name__) from None
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            raise TrainerUnavailable('malformed body') from None
        if not isinstance(payload, dict):
            raise TrainerUnavailable('malformed body')
        if 'result_code' in payload:
            code = payload.get('result_code')
            if code != 200:
                if service_code(code) is None:
                    raise TrainerUnavailable('unknown result_code')
                raise TrainerError(int(code), status)
            data = payload.get('data')
            return data if isinstance(data, dict) else {}
        if 200 <= status < 300:
            return payload   # bare data object (A-15)
        raise TrainerUnavailable('HTTP %s' % status)

    # ---- worker judge adapter (6.4.14 / 7.4.1) ---------------------------------------------------------------------
    def health(self):
        return self._call('GET', '/health', timeout=self.KEEPALIVE_TIMEOUT)

    def keepalive(self):
        return self._call('POST', '/judge-model/keepalive', {}, timeout=self.KEEPALIVE_TIMEOUT)

    # ---- datasets (7.4.3 · 7.4.4 · 7.4.5) ---------------------------------------------------------------------------
    def list_datasets(self, page=1, size=100):
        return self._call('GET', '/datasets', query={'page': int(page), 'size': int(size)})

    def dataset(self, version):
        return self._call('GET', '/datasets/' + checked_name('dataset', version))

    def merge_labels(self, base_version, request_id, items, description=None):
        body = {'request_id': str(request_id), 'items': items}
        if description:
            body['description'] = description
        return self._call('POST', '/datasets/' + checked_name('dataset', base_version) + '/reviewed-labels', body)

    # ---- training (7.4.7 · 7.4.8 · 7.4.9) ----------------------------------------------------------------------------
    def start_training(self, dataset_version, request_id, config_override=None, memo=None):
        checked_name('dataset', dataset_version)
        body = {'dataset_version': dataset_version, 'request_id': str(request_id), 'auto_pipeline': True, 'config_override': dict(config_override or {})}
        if memo:
            body['memo'] = memo
        return self._call('POST', '/train-jobs', body)

    def training_status(self, job_id):
        return self._call('GET', '/train-jobs/' + checked_name('train_job', job_id))

    def stop_training(self, job_id):
        return self._call('DELETE', '/train-jobs/' + checked_name('train_job', job_id))

    # ---- evaluations · models (7.4.13 · 7.4.17 · 7.4.18) -------------------------------------------------------------
    def evaluation(self, evaluation_id):
        return self._call('GET', '/evaluations/' + checked_name('evaluation', evaluation_id))

    def list_models(self, status=None):
        if status not in (None, 'REGISTERED', 'DELETED'):
            raise ValueError('invalid model status')
        return self._call('GET', '/models', query={'status': status})

    def model(self, version):
        return self._call('GET', '/models/' + checked_name('model', version))

    # ---- serving (7.4.21 · 7.4.22 · 7.4.23) · jobs (7.4.24 · 7.4.25) ---------------------------------------------
    def serving(self):
        return self._call('GET', '/deployment')

    def switch(self, model_version):
        checked_name('model', model_version)
        return self._call('PUT', '/deployment', {'model_version': model_version})

    def rollback(self, target, reason):
        # A concrete version name (or LLM_ONLY) instead of "previous" keeps a resend idempotent (API-62).
        if target != 'LLM_ONLY':
            checked_name('model', target)
        return self._call('POST', '/rollbacks', {'target': target, 'reason': str(reason)[:500]})

    def list_jobs(self, job_type=None, status=None, page=1, size=50):
        if job_type not in (None, 'collect', 'train', 'eval', 'export'):
            raise ValueError('invalid job type')
        if status not in (None, 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'HELD'):
            raise ValueError('invalid job status')
        return self._call('GET', '/jobs', query={'type': job_type, 'status': status, 'page': int(page), 'size': int(size)})

    def job(self, job_id):
        return self._call('GET', '/jobs/' + checked_name('job', job_id))


def trainer_for(owner):
    """The adapter of a service or worker object: an injected owner.training_client, else one built from settings."""
    client = getattr(owner, 'training_client', None)
    if client is None:
        client = TrainerClient(owner.settings)
        owner.training_client = client
    return client


def active_serving(tx):
    """Current serving configuration copy for the judge adapter (QA worker).

    Source of truth is 7.4.21; the service copy is the single ACTIVE deployments row (8.2.27):
    serving_snapshot = {mode, serving_version, previous_version, threshold, llm_only_threshold, llm_model_id,
    llm_prompt_version, deployed_at}. model_version_id is the model_versions row with serving = true.
    Returns None before the first deployment request (review A-4); callers then read 7.4.21 / 7.4.1 directly."""
    row = tx.one("SELECT deployment_id,serving_snapshot,synced_at FROM deployments WHERE status='ACTIVE'")
    if not row:
        return None
    snapshot = row['serving_snapshot']
    snapshot = json.loads(snapshot) if isinstance(snapshot, str) else dict(snapshot or {})
    model = tx.one('SELECT model_version_id,version_name FROM model_versions WHERE serving=true')
    return {**snapshot, 'deployment_id': str(row['deployment_id']), 'synced_at': row['synced_at'],
            'model_version_id': str(model['model_version_id']) if model and snapshot.get('mode') == 'CUSTOM'
            and model['version_name'] == snapshot.get('serving_version') else None}


# ---- shared by backend (API-53~58) and worker (DATASET · SYNC) ----------------------------------------------------
SYNC_ALL_TARGET = '6f1f3b5e-5a8e-5d1c-9c55-0e5f1a2b3c4d'   # fixed jobs.target_id for the periodic SYNC (no natural target)
TERMINAL_RUN_STATES = ('SUCCEEDED', 'FAILED', 'HELD', 'CANCELLED')


def candidate_session(service, tx, candidate_id):
    """Lecture title when the candidate may be shown/reviewed/merged now, else None (6.3.2-1, 8.2.20, 8.2.22).

    Re-checked every time: questioner and host both currently grant TRAINING for that lecture (account withdrawal
    wins, ServiceCore.effective) and the lecture is not an evaluation lecture. Provenance IDs never leave this function."""
    prov = tx.one('SELECT question_id,session_id FROM candidate_provenance WHERE candidate_id=%s', (candidate_id,))
    if not prov:
        return None
    sid = prov['session_id']
    s = tx.one('SELECT title,host_user_id,is_eval_lecture,status FROM lecture_sessions WHERE session_id=%s', (sid,))
    if not s or s['is_eval_lecture'] or not s['host_user_id'] or s['status'] in ('DELETING', 'PURGED'):
        return None
    q = tx.one('SELECT participant_id FROM questions WHERE session_id=%s AND question_id=%s', (sid, prov['question_id']))
    part = tx.one('SELECT participant_id,user_id FROM participants WHERE session_id=%s AND participant_id=%s',
                  (sid, q['participant_id'])) if q and q['participant_id'] else None
    if not part:
        return None
    if part['user_id']:
        asker = service.effective(tx, 'TRAINING', user=part['user_id'], sid=sid)
    else:
        asker = service.effective(tx, 'TRAINING', participant=part['participant_id'], sid=sid)
    host = service.effective(tx, 'TRAINING', user=s['host_user_id'], sid=sid)
    if not asker or not asker['granted'] or not host or not host['granted']:
        return None
    return s['title']


def queue_sync(service, tx, run_id=None, when=None, user=None, force=False):
    """Enqueue a SYNC job (8.2.25: on training-run polling, on admin screen views, hourly) unless one is already
    running or due by `when`. run_id limits it to one training run (7.4.8)."""
    when = when or service.clock()
    target = str(run_id) if run_id else SYNC_ALL_TARGET
    tx.lock('sync:' + target)
    if not force:
        pending = tx.one("""SELECT job_id FROM jobs WHERE job_type='SYNC' AND target_id=%s
            AND (status='RUNNING' OR (status='QUEUED' AND available_at<=%s)) LIMIT 1""", (target, when))
        if pending:
            return str(pending['job_id'])
    from .contracts import uid
    payload = {'scope': 'TRAINING_RUN', 'training_run_id': str(run_id)} if run_id else {'scope': 'ALL'}
    return service.job(tx, 'SYNC', target, f'sync:{target}:{uid()}', payload, user=user, when=when)
