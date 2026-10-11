"""Run existing behavioral tests against an isolated real Postgres schema.

Opt in with --postgres-backend and MOYAI_TEST_POSTGRES_URL. Every temporary data
folder gets its own schema; no shared/public schema or external service is used.
"""
import os
from uuid import uuid4

import pytest


def pytest_addoption(parser):
    parser.addoption('--postgres-backend', action='store_true', help='Use real Postgres for Store-based tests.')


@pytest.fixture(autouse=True)
def postgres_backend(request, monkeypatch):
    if not request.config.getoption('--postgres-backend'):
        yield
        return
    if request.node.get_closest_marker('sqlite_only'):
        pytest.skip('This test checks SQLite file snapshots; Postgres uses database backups.')
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.fail('MOYAI_TEST_POSTGRES_URL is required with --postgres-backend.')
    import psycopg
    from app.db import Store

    schemas, stores = {}, []
    initialize = Store.__init__

    def init(store, directory, *args, **kwargs):
        key = str(directory.resolve())
        if key not in schemas:
            schemas[key] = 'moyai_test_' + uuid4().hex
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(f'CREATE SCHEMA "{schemas[key]}"')
        kwargs.update(database_url=url, database_schema=schemas[key], database_initialize=True)
        initialize(store, directory, *args, **kwargs)
        stores.append(store)

    monkeypatch.setattr(Store, '__init__', init)
    try:
        yield
    finally:
        for store in stores:
            store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            for schema in schemas.values():
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
