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
    worker = Worker(service, GeminiEmbedder(s), Mailer(s))
    last = 0
    while True:
        try:
            if time.monotonic() - last >= 60:
                worker.maintenance()
                last = time.monotonic()
            if not worker.run_one():
                time.sleep(1)
        except Exception:
            time.sleep(5)   # DB not reachable yet or a transient error: keep the process alive and retry


if __name__ == '__main__':
    main()
