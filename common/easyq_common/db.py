"""Parameterized PostgreSQL transactions. SQLite is deliberately test-only."""
from contextlib import contextmanager


class Database:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def transaction(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.url, row_factory=dict_row) as connection:
            with connection.transaction():
                yield Tx(connection)


class Tx:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        return self.connection.execute(sql, args)

    def one(self, sql, args=()):
        return self.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.execute(sql, args).fetchall()

    def lock(self, key):
        # Stable per-resource transaction lock, including rows not created yet.
        self.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def notify(self, event_id):
        self.execute("SELECT pg_notify('easyq_events', %s)", (str(event_id),))
