"""worker container entry point (no listening port).  python -m easyq_worker.main"""
import time


def main():
    from easyq_common.auth_client import AuthClient
    from easyq_common.config import Settings
    from easyq_common.core import ServiceCore
    from easyq_common.db import Database
    from easyq_common.storage import storage_from
    from .embedding import GeminiEmbedder
    from .mail import Mailer
    from .worker import Worker
    s = Settings.from_env()
    service = ServiceCore(Database(s.database_url), s, storage_from(s), AuthClient(s, worker=True))
    from easyq_common.replay import RecordedEmbedder, RecordedLLM, store_from
    from .llm import AnthropicLLM
    store = store_from(s)   # PROVIDER_MODE=record|replay: Claude and embeddings go through the recording store
    embedder = GeminiEmbedder(s) if store is None else RecordedEmbedder(GeminiEmbedder(s), store, s.embedding_model)
    worker = Worker(service, embedder, Mailer(s))
    if store is not None:
        worker.llm = RecordedLLM(AnthropicLLM(s), store)
    last = 0
    while True:
        try:
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                worker.maintenance()
                worker.keep_judge_model_warm()   # 6.4.14 judge-model lease, about once a minute
            if not worker.run_one():
                time.sleep(1)
        except Exception:
            time.sleep(5)   # DB not reachable yet or a transient error: keep the process alive and retry


if __name__ == '__main__':
    main()
