"""Training-server MOCK for local tests (design 7.4.1 /health, 7.4.26 /judgments, 6.4.14 /judge-model/keepalive). Not the real model.

Scores candidates by character-bigram overlap with the question. Env:
  TRAINER_MODE=CUSTOM|LLM_ONLY  (LLM_ONLY -> /judgments returns 611, health mode LLM_ONLY)
  TRAINER_THRESHOLD=0.35        TRAINER_API_KEY=...   TRAINER_DELAY_MS=0 (set >800 to test the timeout fallback)
  TRAINER_LEASE_SECONDS=300     TRAINER_LOAD_MS=0 (load time after the first keepalive; until then /judgments returns 611)
Judge-model lease (agreed 2026-11-27): POST /judge-model/keepalive loads the model if needed and keeps it for the lease;
/health reports model_loaded and status stays 'ok'; /judgments answers 611 at once while the model is not loaded.
"""
import os, time
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

app = FastAPI(title='EasyQ trainer mock')
KEY = os.environ.get('TRAINER_API_KEY', 'dev-only-trainer-key')
VERSION = 'judge-mock'
LEASE = {'loading_since': None, 'expires': 0.0}


def lease_state():
    now = time.monotonic()
    if LEASE['expires'] <= now:
        LEASE['loading_since'] = None
        return False, False
    loaded = now - LEASE['loading_since'] >= int(os.environ.get('TRAINER_LOAD_MS', '0')) / 1000
    return loaded, not loaded


def cfg():
    return os.environ.get('TRAINER_MODE', 'CUSTOM').upper(), float(os.environ.get('TRAINER_THRESHOLD', '0.35')), int(os.environ.get('TRAINER_DELAY_MS', '0'))


def reply(code, msg='', data=None, http=200):
    return JSONResponse({'result_code': code, 'result_msg': msg, 'data': data or {}}, status_code=http)


def bigrams(text):
    t = ''.join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


@app.get('/api/v1/health')
def health(x_api_key: str = Header(None)):
    if x_api_key != KEY:
        return reply(612, 'invalid api key', http=401)
    mode, tau, _ = cfg()
    loaded, _ = lease_state()
    return reply(200, '', {'status': 'ok', 'mode': mode, 'serving_version': None if mode == 'LLM_ONLY' else VERSION, 'threshold': tau,
                           'gpu_busy': False, 'model_loaded': loaded})


@app.post('/api/v1/judge-model/keepalive')
def keepalive(x_api_key: str = Header(None)):
    if x_api_key != KEY:
        return reply(612, 'invalid api key', http=401)
    now = time.monotonic()
    if LEASE['expires'] <= now or LEASE['loading_since'] is None:
        LEASE['loading_since'] = now
    LEASE['expires'] = now + int(os.environ.get('TRAINER_LEASE_SECONDS', '300'))
    loaded, loading = lease_state()
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=LEASE['expires'] - now)).isoformat()
    return reply(200, '', {'loaded': loaded, 'loading': loading, 'expires_at': expires_at})


@app.post('/api/v1/judgments')
def judge(body: dict, x_api_key: str = Header(None)):
    if x_api_key != KEY:
        return reply(612, 'invalid api key', http=401)
    mode, tau, delay = cfg()
    if mode == 'LLM_ONLY':
        return reply(611, 'serving mode is LLM_ONLY', http=503)
    if not lease_state()[0]:
        return reply(611, 'judge model is not loaded', http=503)
    start = time.perf_counter()
    time.sleep(delay / 1000)
    q = bigrams(body.get('question', ''))
    ranked = sorted(({'evidence_id': c['evidence_id'], 'score': round(len(q & bigrams(c.get('text', ''))) / max(len(q), 1), 4)}
                     for c in body.get('candidates', [])[:20]), key=lambda r: -r['score'])
    top_k = int(body.get('top_k') or 5)
    max_score = ranked[0]['score'] if ranked else 0.0
    return reply(200, '', {'model_version': VERSION, 'threshold': tau, 'decision': 'ANSWER' if max_score >= tau else 'ESCALATE',
                           'max_score': max_score, 'ranked': ranked,
                           'selected': [r['evidence_id'] for r in ranked if r['score'] >= tau][:top_k],
                           'latency_ms': int((time.perf_counter() - start) * 1000)})


# ---- admin-side training registry mock (7.4.3~7.4.5, 7.4.7~7.4.9, 7.4.13, 7.4.17 · 7.4.18, 7.4.21~7.4.25) -------------
# In-memory only; enough for the admin console flows (API-55~63) with the worker SYNC/TRAIN/DATASET/DEPLOY jobs.
# A training job finishes after TRAINER_TRAIN_SECONDS (default 5) and registers the next judge-vN as deployable.
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
_STATS = {'TRAIN': {'questions': 3000, 'pairs': 4400}, 'VALIDATION': {'questions': 480}, 'TEST': {'questions': 620}}
REG = {
    'datasets': {'ds-v1': {'dataset_version': 'ds-v1', 'parent_version': None, 'status': 'ACTIVE', 'contains_aihub': True,
                           'statistics': _STATS, 'script_version': 'build-dataset@1.2.0', 'retrieval_config_version': 'rc-v1',
                           'validation_set_version': 'val-v1', 'test_set_version': 'test-v1', 'eval_slide_version': 'es-v1',
                           'test_use_count': 0, 'change_note': 'mock initial dataset', 'created_at': '2026-11-01T09:00:00+09:00'}},
    'merges': {}, 'jobs': {}, 'models': {}, 'evaluations': {},
    'deployment': {'mode': 'LLM_ONLY', 'serving_version': None, 'previous_version': None, 'threshold': None,
                   'llm_only_threshold': 70, 'llm_model_id': 'claude-sonnet-5-5', 'llm_prompt_version': 'llm-only-v1',
                   'deployed_at': '2026-11-01T09:00:00+09:00'},
}


def _now():
    return datetime.now(KST).isoformat()


def _auth(key):
    return key == KEY


def _advance(job):
    seconds = float(os.environ.get('TRAINER_TRAIN_SECONDS', '5'))
    if job['status'] in ('QUEUED', 'RUNNING') and time.monotonic() - job['_started'] >= seconds:
        n = len(REG['models']) + 1
        version, eval_id = f'judge-v{n}', 'eval-%04d' % (len(REG['evaluations']) + 1)
        REG['evaluations'][eval_id] = {
            'evaluation_id': eval_id, 'status': 'SUCCEEDED', 'dataset_version': job['dataset_version'],
            'validation_set_version': 'val-v1', 'test_set_version': 'test-v1', 'retrieval_config_version': 'rc-v1', 'threshold': 0.62,
            'valid_auc_fp32': 0.884, 'valid_auc_fp16': 0.882, 'precision_auc_drop': 0.002, 'latency_p95_ms': 180,
            'roc_auc': {'value': 0.88, 'ci_low': 0.85, 'ci_high': 0.91}, 'pair_roc_auc': 0.91,
            'service_metrics': {'retrieval_success_rate': 0.88},
            'baselines': [{'evaluator': 'SONNET_ONLY', 'roc_auc': 0.83, 'threshold': 70}],
            'llm_only': {'threshold': 70, 'llm_model_id': 'claude-sonnet-5-5', 'llm_prompt_version': 'llm-only-v1'}, 'test_use_count': 1}
        REG['models'][version] = {
            'model_version': version, 'dataset_version': job['dataset_version'], 'test_set_version': 'test-v1',
            'roc_auc': 0.88, 'false_answer_rate': 0.042, 'tpr': 0.61, 'threshold': 0.62, 'precision_auc_drop': 0.002,
            'latency_p95_ms': 180, 'status': 'REGISTERED', 'serving': False, 'served_before': False, 'deployable': True,
            'compared_with': REG['deployment']['serving_version'], 'memo': job.get('memo'), 'created_at': _now(), 'evaluation_id': eval_id}
        job.update(status='SUCCEEDED', stage='register', progress=1.0, model_version=version, deployable=True, evaluation_id=eval_id,
                   metrics={'roc_auc': 0.88, 'threshold': 0.62}, finished_at=_now())
    elif job['status'] == 'QUEUED':
        job.update(status='RUNNING', stage='train', progress=0.1)
    return job


def _public(job):
    return {k: v for k, v in job.items() if not k.startswith('_')}


@app.get('/api/v1/datasets')
def list_datasets(page: int = 1, size: int = 20, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    items = sorted(REG['datasets'].values(), key=lambda d: d['created_at'])
    keys = ('dataset_version', 'parent_version', 'status', 'contains_aihub', 'statistics', 'script_version', 'retrieval_config_version',
            'test_set_version', 'test_use_count', 'change_note', 'created_at')
    return reply(200, '', {'datasets': [{k: d[k] for k in keys} for d in items[(page - 1) * size: page * size]], 'total_count': len(items)})


@app.get('/api/v1/datasets/{version}')
def get_dataset(version: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    if version not in REG['datasets']:
        return reply(613, 'no dataset', http=404)
    return reply(200, '', REG['datasets'][version])


@app.post('/api/v1/datasets/{version}/reviewed-labels')
def merge_labels(version: str, body: dict, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    if version not in REG['datasets']:
        return reply(613, 'no dataset', http=404)
    if body.get('request_id') in REG['merges']:
        return reply(200, '', REG['merges'][body['request_id']])
    new = 'ds-v%d' % (len(REG['datasets']) + 1)
    REG['datasets'][new] = {**REG['datasets'][version], 'dataset_version': new, 'parent_version': version,
                            'change_note': body.get('description') or 'feedback merge', 'created_at': _now()}
    result = {'dataset_version': new, 'added': len(body.get('items') or []), 'excluded_overlap': 0, 'masked': 0, 'skipped': 0}
    REG['merges'][body['request_id']] = result
    return reply(200, '', result)


@app.post('/api/v1/train-jobs')
def start_training(body: dict, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    if body.get('dataset_version') not in REG['datasets']:
        return reply(613, 'no dataset', http=404)
    for job in REG['jobs'].values():
        if job.get('_request_id') == body.get('request_id'):
            return reply(200, '', {'job_id': job['job_id'], 'status': job['status']})
    if any(_advance(j)['status'] in ('QUEUED', 'RUNNING') for j in REG['jobs'].values()):
        return reply(609, 'gpu busy', http=409)
    job_id = 'train-%04d' % (len(REG['jobs']) + 1)
    REG['jobs'][job_id] = {
        'job_id': job_id, 'type': 'train', 'status': 'QUEUED', 'stage': None, 'hold_reason': None, 'progress': 0.0,
        'curve': [{'epoch': 1, 'train_loss': 0.41, 'valid_loss': 0.36, 'valid_auc': 0.86}], 'config': body.get('config_override') or {},
        'dataset_version': body['dataset_version'], 'memo': body.get('memo'), 'model_version': None, 'deployable': None,
        'evaluation_id': None, 'metrics': {}, 'error_code': None, 'created_at': _now(), 'finished_at': None,
        '_request_id': body.get('request_id'), '_started': time.monotonic()}
    return reply(200, '', {'job_id': job_id, 'status': 'QUEUED'})


@app.get('/api/v1/train-jobs/{job_id}')
def training_status(job_id: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    if job_id not in REG['jobs']:
        return reply(613, 'no job', http=404)
    return reply(200, '', _public(_advance(REG['jobs'][job_id])))


@app.delete('/api/v1/train-jobs/{job_id}')
def stop_training(job_id: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    job = REG['jobs'].get(job_id)
    if not job:
        return reply(613, 'no job', http=404)
    if _advance(job)['status'] not in ('QUEUED', 'RUNNING'):
        return reply(608, 'job already finished', http=409)
    job.update(status='CANCELLED', finished_at=_now())
    return reply(200, '', {'status': 'CANCELLED'})


@app.get('/api/v1/evaluations/{evaluation_id}')
def get_evaluation(evaluation_id: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    if evaluation_id not in REG['evaluations']:
        return reply(613, 'no evaluation', http=404)
    return reply(200, '', REG['evaluations'][evaluation_id])


@app.get('/api/v1/models')
def list_models(status: str = None, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    items = [{k: v for k, v in m.items() if k != 'evaluation_id'} for m in REG['models'].values() if not status or m['status'] == status]
    return reply(200, '', {'models': items})


@app.get('/api/v1/models/{version}')
def get_model(version: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    m = REG['models'].get(version)
    if not m:
        return reply(613, 'no model', http=404)
    manifest = {'model_version': version, 'base_model': 'BAAI/bge-reranker-v2-m3', 'dataset_version': m['dataset_version'],
                'format': 'pytorch-fp16', 'threshold': m['threshold'], 'artifact_sha256': 'a' * 64}
    return reply(200, '', {'manifest': manifest, 'evaluation_id': m['evaluation_id'], 'roc_points': [], 'train_curve': [], 'baselines': [],
                           'deployable': m['deployable'], 'compared_with': m['compared_with']})


def _serve(mode, version):
    dep = REG['deployment']
    if dep['serving_version']:
        REG['models'][dep['serving_version']]['serving'] = False
    previous = dep['serving_version']
    dep.update(mode=mode, serving_version=version, previous_version=previous, deployed_at=_now(),
               threshold=REG['models'][version]['threshold'] if version else dep['threshold'])
    if version:
        REG['models'][version].update(serving=True, served_before=True)
    os.environ['TRAINER_MODE'] = mode   # /health and /judgments follow the serving mode
    return dict(dep)


@app.get('/api/v1/deployment')
def get_deployment(x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    return reply(200, '', dict(REG['deployment']))


@app.put('/api/v1/deployment')
def switch_deployment(body: dict, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    m = REG['models'].get(body.get('model_version'))
    if not m or m['status'] != 'REGISTERED' or m['served_before']:
        return reply(610, 'model state', http=409)
    if not m['deployable']:
        return reply(614, 'below deploy criteria', http=409)
    return reply(200, '', _serve('CUSTOM', m['model_version']))


@app.post('/api/v1/rollbacks')
def rollback(body: dict, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    target = body.get('target')
    if target == 'LLM_ONLY':
        return reply(200, '', _serve('LLM_ONLY', None))
    if target == 'previous':
        target = REG['deployment']['previous_version']
    m = REG['models'].get(target)
    if not m or not m['served_before'] or m['status'] != 'REGISTERED':
        return reply(610, 'no serving history', http=409)
    return reply(200, '', _serve('CUSTOM', target))


@app.get('/api/v1/jobs')
def list_jobs(type: str = None, status: str = None, page: int = 1, size: int = 20, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    items = [_public(_advance(j)) for j in REG['jobs'].values()]
    items = [{k: j[k] for k in ('job_id', 'type', 'status', 'progress', 'created_at', 'finished_at')} for j in items
             if (not type or j['type'] == type) and (not status or j['status'] == status)]
    return reply(200, '', {'jobs': items[(page - 1) * size: page * size]})


@app.get('/api/v1/jobs/{job_id}')
def get_job(job_id: str, x_api_key: str = Header(None)):
    if not _auth(x_api_key):
        return reply(612, 'invalid api key', http=401)
    j = REG['jobs'].get(job_id)
    if not j:
        return reply(613, 'no job', http=404)
    j = _advance(j)
    return reply(200, '', {'job_id': j['job_id'], 'type': j['type'], 'status': j['status'], 'progress': j['progress'],
                           'result': {'evaluation_id': j['evaluation_id']}, 'fault_id': None})
