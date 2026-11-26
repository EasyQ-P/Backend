"""Embedding adapter (Gemini Embedding 2, 768 dimensions)."""
import json
import urllib.request

from easyq_common.contracts import APIError, dumps


class GeminiEmbedder:
    """Explicitly configured Gemini model; fail rather than emit placeholder vectors."""
    def __init__(self, settings):
        self.settings = settings

    def embed(self, texts):
        s = self.settings
        if not s.embedding_url or not s.embedding_key or not s.embedding_model:
            raise APIError(503, 'EMBEDDING_NOT_CONFIGURED')
        body = {'requests': [{'model': s.embedding_model, 'content': {'parts': [{'text': t}]},
            'taskType': 'RETRIEVAL_DOCUMENT', 'outputDimensionality': 768} for t in texts]}
        request = urllib.request.Request(s.embedding_url, data=dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'x-goog-api-key': s.embedding_key}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.load(response)
            return [v['values'] for v in data['embeddings']]
        except Exception:
            raise APIError(503, 'EMBEDDING_PROVIDER_UNAVAILABLE') from None
