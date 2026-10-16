import os
from uuid import uuid4

import psycopg
import pytest


@pytest.fixture
def target():
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_runtime_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    yield url, schema
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
