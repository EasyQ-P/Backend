"""Run the same HTTP suite against real PostgreSQL 16/pgvector when configured.

The DSN must name a dedicated *_test database. Every run uses and removes its
own random schema; the supplied SQL and migration are applied without rewriting.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from easyq_common.db import Tx
from . import test_api
from .support import SCHEMA


@unittest.skipUnless(os.environ.get('EASYQ_TEST_DATABASE_URL'), 'PostgreSQL DSN not configured')
class PostgreSQLTests(test_api.APITests):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        dsn = os.environ['EASYQ_TEST_DATABASE_URL']
        if not conninfo_to_dict(dsn).get('dbname', '').endswith('_test'):
            raise RuntimeError('Use a dedicated database whose name ends in _test')
        super().setUp()
        self.db.connection.close()
        self.moment = datetime.now(timezone.utc)
        self.schema = 'easyq_test_' + uuid4().hex
        self.pg_admin = psycopg.connect(dsn, autocommit=True)
        self.pg_admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        self.pg_admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.pg_admin.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(self.schema)))
        self.pg_admin.execute(SCHEMA.read_text(encoding='utf-8'))
        migration = Path(__file__).resolve().parents[1] / 'migrations/001_auth_session_view.sql'
        self.pg_admin.execute(migration.read_text(encoding='utf-8'))
        schema = self.schema

        class PostgreSQLDatabase:
            @contextmanager
            def transaction(self):
                with psycopg.connect(dsn, row_factory=dict_row) as connection:
                    with connection.transaction():
                        connection.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(schema)))
                        yield Tx(connection)

        self.db = PostgreSQLDatabase()
        self.service.db = self.db
        self.worker.db = self.db

    def tearDown(self):
        from psycopg import sql
        if hasattr(self, 'pg_admin'):
            self.pg_admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
            self.pg_admin.close()
        self.tmp.cleanup()

    def test_native_postgresql_indexes(self):
        with self.db.transaction() as tx:
            indexes = tx.all('SELECT indexdef FROM pg_indexes WHERE schemaname=%s', (self.schema,))
            self.assertTrue(any('USING hnsw' in r['indexdef'] for r in indexes))
            self.assertTrue(any('USING gin' in r['indexdef'] for r in indexes))
            self.assertTrue(tx.one("SELECT to_tsvector('simple','hash collision') @@ plainto_tsquery('simple','hash') AS found")['found'])

    def test_concurrent_joins_cannot_exceed_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from easyq_common.contracts import APIError, Request, uid
        lecture, _ = self.lecture()
        with self.db.transaction() as tx:
            for _ in range(49):
                tx.execute("INSERT INTO participants(participant_id,session_id,kind,nickname) VALUES(%s,%s,'GUEST','test')", (uid(), lecture['session_id']))
        barrier = threading.Barrier(2)
        def join():
            barrier.wait(timeout=10)
            req = Request('POST', '/api/v1/participations', {'origin': self.settings.origin},
                {'join_code': lecture['join_code'], 'nickname': 'test', 'service_consent': True})
            try:
                return self.service.execute(req, 'backend').status
            except APIError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: join(), range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(self.row('SELECT count(*) n FROM participants')['n'], 50)
