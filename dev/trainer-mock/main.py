"""Training-server MOCK for local tests (design 7.4.1 /health, 7.4.26 /judgments). Not the real model.

Scores candidates by character-bigram overlap with the question. Env:
  TRAINER_MODE=CUSTOM|LLM_ONLY  (LLM_ONLY -> /judgments returns 611, health mode LLM_ONLY)
  TRAINER_THRESHOLD=0.35        TRAINER_API_KEY=...   TRAINER_DELAY_MS=0 (set >800 to test the timeout fallback)
"""
import os, time
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

app = FastAPI(title='EasyQ trainer mock')
KEY = os.environ.get('TRAINER_API_KEY', 'dev-only-trainer-key')
VERSION = 'judge-mock'


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
    return reply(200, '', {'status': 'ok', 'mode': mode, 'serving_version': None if mode == 'LLM_ONLY' else VERSION, 'threshold': tau, 'gpu_busy': False})


@app.post('/api/v1/judgments')
def judge(body: dict, x_api_key: str = Header(None)):
    if x_api_key != KEY:
        return reply(612, 'invalid api key', http=401)
    mode, tau, delay = cfg()
    if mode == 'LLM_ONLY':
        return reply(611, 'serving mode is LLM_ONLY', http=503)
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
