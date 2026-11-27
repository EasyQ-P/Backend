"""Record / replay of external provider calls for low-cost integration tests (agreed with the AI team 2026-11-27).

PROVIDER_MODE=live (default): providers are called normally; nothing here is used.
PROVIDER_MODE=record: a stored answer is used when the same request was recorded before; otherwise the real provider is
    called once and its answer is saved under PROVIDER_STORE.
PROVIDER_MODE=replay: only stored answers are used. A request that was never recorded raises ReplayMiss instead of
    calling (and paying for) the real provider.

Keys are SHA-256 hashes of the request (LLM: model, system, prompt, max_tokens; embedding: model, task, text), so a
fixed lecture + question set replays exactly. STT recordings are keyed by a name (PROVIDER_STT_RECORDING) because
audio arrives as a stream. The store holds lecture text and answers: keep it outside the repository (default var/).
Logs carry only the kind and key, never the text.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

MODES = ('live', 'record', 'replay')


class ReplayMiss(RuntimeError):
    """A replay-mode request with no recording. Raised instead of calling the provider (no accidental charges)."""


class ProviderStore:
    def __init__(self, root, mode):
        if mode not in MODES:
            raise ValueError('PROVIDER_MODE must be live, record or replay')
        self.root, self.mode = Path(root), mode

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def path(self, kind, key):
        return self.root / kind / (key + '.json')

    def load(self, kind, key):
        path = self.path(kind, key)
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        if self.mode == 'replay':
            print(json.dumps({'level': 'ERROR', 'service': 'replay', 'event': 'replay.miss', 'meta': {'kind': kind, 'key': key}}),
                  file=sys.stderr)
            raise ReplayMiss(f'no recording for {kind} {key}')
        return None

    def save(self, kind, key, value):
        path = self.path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)


def store_from(settings):
    """The ProviderStore for record/replay, or None in live mode."""
    mode = getattr(settings, 'provider_mode', 'live') or 'live'
    return None if mode == 'live' else ProviderStore(settings.provider_store, mode)


# ---- Claude (worker llm.py interface: complete / stream returning LLMResult-like dicts) ----------------------------
class RecordedLLM:
    def __init__(self, inner, store):
        self.inner, self.store = inner, store

    def complete(self, model, system, prompt, max_tokens):
        key = self.store.key('complete', model, system, prompt, max_tokens)
        saved = self.store.load('llm', key)
        if saved is None:
            saved = dict(self.inner.complete(model, system, prompt, max_tokens))
            self.store.save('llm', key, saved)
        return _result(saved)

    def stream(self, model, system, prompt, max_tokens, on_text):
        key = self.store.key('stream', model, system, prompt, max_tokens)
        saved = self.store.load('llm', key)
        if saved is None:
            chunks = []

            def collect(chunk):
                chunks.append(chunk)
                return on_text(chunk)
            result = dict(self.inner.stream(model, system, prompt, max_tokens, collect))
            if result.get('stop_reason') != 'stopped':   # an early stop is not the full answer: do not store it
                self.store.save('llm', key, {**result, 'chunks': chunks})
            return _result(result)
        for chunk in saved.get('chunks', []):
            if on_text(chunk) is False:
                return _result({**saved, 'text': ''.join(saved['chunks'][:saved['chunks'].index(chunk) + 1]),
                                'stop_reason': 'stopped', 'input_tokens': None, 'output_tokens': None})
        return _result(saved)


def _result(value):
    from easyq_worker.llm import LLMResult   # worker-only import: only the worker builds RecordedLLM
    return LLMResult(**{k: v for k, v in value.items() if k != 'chunks'})


# ---- Embeddings (worker embedding.py interface: embed / embed_query, list in -> list of vectors out) ---------------
class RecordedEmbedder:
    def __init__(self, inner, store, model):
        self.inner, self.store, self.model = inner, store, model

    def embed(self, texts):
        return self._embed(texts, 'RETRIEVAL_DOCUMENT', self.inner.embed)

    def embed_query(self, texts):
        return self._embed(texts, 'RETRIEVAL_QUERY', self.inner.embed_query)

    def _embed(self, texts, task, call):
        keys = [self.store.key(self.model, task, text) for text in texts]
        vectors = [self.store.load('embedding', key) for key in keys]
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        if missing:   # record mode only (replay raised above): one real call for the texts not recorded yet
            fresh = call([texts[i] for i in missing])
            for i, vector in zip(missing, fresh):
                self.store.save('embedding', keys[i], vector)
                vectors[i] = vector
        return vectors


# ---- STT (backend stt.py interface: open(terms, on_final) -> stream with async send(pcm, start_ms) / close()) ------
class RecordedSTT:
    """Record: relay to the real STT and save its final transcripts with times relative to the first frame.
    Replay: ignore the audio content and emit the recorded finals as the stream reaches their end time."""

    def __init__(self, inner, store, name):
        self.inner, self.store, self.name = inner, store, name

    def open(self, terms, on_final):
        if self.store.mode == 'replay':
            return _ReplayStream(self.store.load('stt', self.name) or [], on_final)
        return _RecordStream(self, terms, on_final)


class _RecordStream:
    def __init__(self, owner, terms, on_final):
        self.owner, self.on_final, self.first, self.finals = owner, on_final, None, []
        self.stream = owner.inner.open(terms, self.capture)

    async def capture(self, start, end, text):
        base = self.first or 0
        self.finals.append({'start_ms': start - base, 'end_ms': end - base, 'text': text})
        await self.on_final(start, end, text)

    async def send(self, pcm, start_ms):
        if self.first is None:
            self.first = start_ms
        await self.stream.send(pcm, start_ms)

    async def close(self):
        try:
            await self.stream.close()
        finally:
            self.owner.store.save('stt', self.owner.name, self.finals)


class _ReplayStream:
    def __init__(self, finals, on_final):
        self.pending, self.on_final, self.first = list(finals), on_final, None

    async def send(self, pcm, start_ms):
        if self.first is None:
            self.first = start_ms
        reached = start_ms - self.first + len(pcm) // 32   # 16 kHz mono PCM16 = 32 bytes per ms (audio.start sample_rate, as stt.py)
        while self.pending and self.pending[0]['end_ms'] <= reached:
            item = self.pending.pop(0)
            await self.on_final(self.first + item['start_ms'], self.first + item['end_ms'], item['text'])

    async def close(self):
        while self.pending and self.first is not None:
            item = self.pending.pop(0)
            await self.on_final(self.first + item['start_ms'], self.first + item['end_ms'], item['text'])
