"""Run the same HTTP suite against real PostgreSQL 16/pgvector when configured.

The DSN must name a dedicated *_test database. Every run uses and removes its
own random schema; the supplied SQL and migration are applied without rewriting.
"""
from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from easyq_common.db import Tx
from . import test_api
from .support import SCHEMA


PG_DSN = os.environ.get('EASYQ_TEST_DATABASE_URL')
# EASYQ_TEST_ROLES=1: grant migrations/002_runtime_grants.sql in the test schema and run every auth/backend request and
# worker job under its own least-privilege role (easyq_auth/easyq_backend/easyq_worker must exist in the cluster, e.g.
# from dev/db-init/03_dev_roles.sql). Fixture SQL in tests stays on the owner connection.
PG_ROLES = os.environ.get('EASYQ_TEST_ROLES') == '1'
GRANTS = Path(__file__).resolve().parents[1] / 'migrations' / '002_runtime_grants.sql'
CURRENT_ROLE = contextvars.ContextVar('easyq_test_role', default=None)


def run_as(role, func):
    def wrapper(*args, **kwargs):
        token = CURRENT_ROLE.set(role)
        try:
            return func(*args, **kwargs)
        finally:
            CURRENT_ROLE.reset(token)
    return wrapper


class PostgresMixin:
    """Put first in the bases of any APIBase suite to run it on PostgreSQL:
        @unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
        class PGFooTests(PostgresMixin, FooTests): pass
    """

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
        schema = self.schema
        if PG_ROLES:
            self.pg_admin.execute(GRANTS.read_text(encoding='utf-8'))
            self.pg_admin.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO easyq_auth, easyq_backend, easyq_worker').format(sql.Identifier(schema)))
            execute = self.service.execute
            self.service.execute = lambda req, mode='backend': run_as('easyq_' + mode, execute)(req, mode)
            for name in ('run_one', 'maintenance', 'keep_judge_model_warm'):
                if hasattr(self.worker, name):
                    setattr(self.worker, name, run_as('easyq_worker', getattr(self.worker, name)))

        class PostgreSQLDatabase:
            @contextmanager
            def transaction(self):
                with psycopg.connect(dsn, row_factory=dict_row) as connection:
                    with connection.transaction():
                        connection.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(schema)))
                        if CURRENT_ROLE.get():
                            connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(CURRENT_ROLE.get())))
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



@unittest.skipUnless(PG_DSN, 'PostgreSQL DSN not configured')
class PostgreSQLTests(PostgresMixin, test_api.APITests):
    def test_keyword_vector_keeps_rc_v1_tokens_verbatim(self):
        """to_tsvector('simple') would split C++ / O(n) / e-mail; the literal keeps them, with term frequencies (AI #5)."""
        from easyq_worker.retrieval import keyword_vector
        with self.db.transaction() as tx:
            row = tx.one("SELECT %s::tsvector AS v", (keyword_vector('C++ O(n) e-mail a_b C++'),))
            lexemes = {r['lexeme']: r['n'] for r in tx.all(
                'SELECT lexeme, array_length(positions,1) AS n FROM unnest(%s::tsvector)', (row['v'],))}
            self.assertEqual(lexemes, {'c++': 2, 'o(n)': 1, 'e-mail': 1, 'a_b': 1})
            self.assertTrue(tx.one("SELECT %s::tsvector @@ %s::tsquery AS hit", (row['v'], "'c++' | 'nothing'"))['hit'])

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
