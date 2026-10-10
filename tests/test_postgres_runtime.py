"""Runtime guarantees against PostgreSQL 17, including an imported database."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import DatabaseError, IntegrityError, bindings
from app.db import Store
from app.main import create_app
from app import postgres_migration as migration


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


@pytest.fixture
def store(tmp_path, target):
    url, schema = target
    store = Store(tmp_path, database_url=url, database_schema=schema, database_initialize=True, application_instance=True)
    yield store
    store.close()


def test_bindings_preserve_literal_question_marks_percent_and_comments():
    assert bindings("SELECT ?, '? %', \"?\" -- ?\n/* ? */ WHERE ? IS NULL") == (
        "SELECT %s, '? %%', \"?\" -- ?\n/* ? */ WHERE %s IS NULL")


def test_configuration_requires_explicit_postgres_and_no_sqlite_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unrelated-host/other-project')
    assert Settings(_env_file=None).moyai_database_url == ''
    with pytest.raises(ValueError, match='CHECKPOINT_DIR'):
        Settings(_env_file=None, moyai_database_url='postgresql://localhost/test', checkpoint_dir=tmp_path)
    with pytest.raises(ValueError):
        Settings(_env_file=None, moyai_database_schema='public; DROP SCHEMA moyai')
    assert 'private-password' not in repr(Settings(_env_file=None, moyai_database_url='postgresql://user:private-password@localhost/test'))


def test_empty_schema_requires_explicit_initialization(tmp_path, target):
    url, schema = target
    with pytest.raises(DatabaseError, match='INITIALIZE'):
        Store(tmp_path, database_url=url, database_schema=schema)
    assert not (tmp_path / 'workspace.db').exists()


def test_no_fallback_to_sqlite_on_failed_postgres(tmp_path):
    with pytest.raises(DatabaseError, match='initialization failed'):
        Store(tmp_path, database_url='postgresql://nobody:secret@127.0.0.1:1/absent')
    assert not (tmp_path / 'workspace.db').exists()


def test_generated_ids_rollback_constraints_and_binary_roundtrip(store):
    run = store.create_run('日本語 🗿', '', 'demo', [], chat_enabled=True)
    first = store.messages(run['id'])[0]['id']
    with pytest.raises(IntegrityError):
        with store.connect() as conn:
            conn.begin_write()
            conn.execute("UPDATE runs SET summary='must roll back' WHERE id=?", (run['id'],))
            conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES('absent','status','','{}','')")
    assert store.run(run['id'])['summary'] == ''
    row, _ = store.enqueue_message(run['id'], 'Second', 'second')
    assert row['id'] > first
    payload = bytes(range(256))
    saved = store.attachments.save(uuid4().hex, 'test', 'binary.bin', payload, ('application/octet-stream', b'', ''), 1024)
    raw = store.rows('SELECT * FROM attachments WHERE id=?', (saved['id'],))[0]
    assert store.attachments.payload(raw) == payload
    assert not store.path.exists()


def test_concurrent_submission_and_claim_are_atomic(store):
    with ThreadPoolExecutor(max_workers=6) as executor:
        runs = list(executor.map(lambda _: store.create_run('One request', '', 'demo', [], chat_enabled=True,
                                                           client_id='dedupe', user_id='owner'), range(12)))
    assert len({run['id'] for run in runs}) == 1
    run_id = runs[0]['id']
    with ThreadPoolExecutor(max_workers=6) as executor:
        claimed = list(executor.map(lambda _: store.claim_message(run_id), range(12)))
    assert sum(message is not None for message in claimed) == 1
    assert len(store.messages(run_id)) == 1


def test_read_projection_keeps_one_snapshot_while_writes_continue(store):
    run = store.create_run('Snapshot', '', 'demo', [])
    with store.connect() as conn:
        conn.begin_read()
        before = conn.execute('SELECT count(*) FROM events WHERE run_id=?', (run['id'],)).fetchone()[0]
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(store.event, run['id'], 'status', 'Arrived later').result(timeout=5)
        assert conn.execute('SELECT count(*) FROM events WHERE run_id=?', (run['id'],)).fetchone()[0] == before
    assert len(store.events(run['id'])) == before + 1


def test_second_instance_cannot_run_startup_recovery(store, target, tmp_path):
    url, schema = target
    run = store.create_run('Running work', '', 'demo', [], chat_enabled=True)
    store.update_run(run['id'], status='running')
    with pytest.raises(DatabaseError, match='Another Moyai instance'):
        create_app(Settings(_env_file=None, data_dir=tmp_path / 'other', moyai_database_url=url, moyai_database_schema=schema))
    assert store.run(run['id'])['status'] == 'running'


def test_lost_ownership_fences_old_process_and_allows_replacement(store, target, tmp_path):
    url, schema = target
    run = store.create_run('Preserve me', '', 'demo', [], chat_enabled=True)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute('SELECT pg_terminate_backend(%s)', (store.database.owner_pid,))
    with pytest.raises(DatabaseError, match='ownership'):
        store.rows('SELECT 1')
    with pytest.raises(DatabaseError, match='ownership'):
        store.update_run(run['id'], summary='stale process')
    replacement = Store(tmp_path / 'replacement', database_url=url, database_schema=schema)
    try:
        assert replacement.run(run['id'])['prompt'] == 'Preserve me'
    finally:
        replacement.close()


@pytest.mark.parametrize('phase', ['recover', 'shutdown'])
def test_lifespan_failure_releases_application_ownership(target, tmp_path, monkeypatch, phase):
    from app.runner import RunManager

    url, schema = target
    settings = Settings(_env_file=None, data_dir=tmp_path, moyai_database_url=url,
                        moyai_database_schema=schema, moyai_database_initialize=True,
                        session_titles_enabled=False)
    monkeypatch.setattr(RunManager, phase, AsyncMock(side_effect=RuntimeError('Synthetic lifecycle failure')))
    app = create_app(settings)
    with pytest.raises(RuntimeError, match='Synthetic lifecycle failure'):
        with TestClient(app):
            pass
    replacement = Store(tmp_path, database_url=url, database_schema=schema, application_instance=True)
    replacement.close()


def test_restart_resumes_queued_messages_and_event_cursor(store, target, tmp_path):
    url, schema = target
    run = store.create_run('First', '', 'demo', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    store.finish_message(run['id'], first['id'], 'Saved answer')
    second, _ = store.enqueue_message(run['id'], 'Continue', 'followup')
    cursor = store.events(run['id'])[-1]['id']
    store.close()
    replacement = Store(tmp_path, database_url=url, database_schema=schema)
    try:
        claimed = replacement.claim_message(run['id'])
        assert claimed['id'] == second['id']
        assert any(m['content'] == 'Saved answer' for m in replacement.messages(run['id']))
        replacement.event(run['id'], 'status', 'Resumed')
        assert all(e['id'] > cursor for e in replacement.events(run['id'], cursor))
    finally:
        replacement.close()


def test_imported_database_runs_without_touching_original_sqlite(tmp_path, target):
    url, schema = target
    # The importer creates the target itself; remove only our empty test schema.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}"')
    directory = tmp_path / 'source'
    source = create_app(Settings(_env_file=None, data_dir=directory, session_titles_enabled=False))
    run = source.state.store.create_run('Imported history — 🗿', '', 'demo', [], chat_enabled=True)
    ciphertext = source.state.security.fernet.encrypt(b'synthetic-credential').decode()
    source.state.store.execute('INSERT INTO connections VALUES(?,?,?,?)', ('github', ciphertext, 'Synthetic', '2026-10-09'))
    assert migration.transfer(directory, url, schema)['verified']
    before = migration.plan(directory)['tables']
    destination = create_app(Settings(_env_file=None, data_dir=directory, moyai_database_url=url, moyai_database_schema=schema,
                                      session_titles_enabled=False))
    try:
        store = destination.state.store
        assert store.claim_message(run['id'])['content'] == 'Imported history — 🗿'
        stored = store.rows('SELECT encrypted FROM connections')[0]['encrypted']
        assert destination.state.security.fernet.decrypt(stored.encode()) == b'synthetic-credential'
        store.enqueue_message(run['id'], 'Written only to Postgres', 'new')
        assert migration.plan(directory)['tables'] == before
    finally:
        source.state.store.close()
        destination.state.store.close()


def test_typed_json_fields_match_sqlite_for_history_and_permissions(store, tmp_path):
    local = Store(tmp_path / 'sqlite')
    try:
        for document in ['{}', '[]', 'null', '{"key":null}', '{"key":true}', '{"key":1.25}', '{"key":7}', '{"key":"7"}']:
            query = "SELECT json_number(?,'key') AS number,json_text(?,'key') AS text"
            assert store.rows(query, (document, document)) == local.rows(query, (document, document))
    finally:
        local.close()
