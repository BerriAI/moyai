"""Rejected application starts must not migrate or fence a healthy cluster."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app import main
from app.database import DatabaseError, runtime_fingerprint
from app.db import Store
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401


def schema_versions(store):
    # CREATE OR REPLACE of an identical function still changes its catalog row.
    return store.rows('''SELECT proname,xmin::text AS version FROM pg_proc
        WHERE pronamespace=?::regnamespace AND proname IN ('unicode_lower','json_text','json_number')
        ORDER BY proname''', (store.database.schema,))


@pytest.mark.parametrize('change', [
    {'moyai_build_sha': 'b' * 40},
    {'session_secret': 'different-signing-key'},
    {'max_concurrent_model_requests': 999},
])
@pytest.mark.parametrize('role', ['worker', 'broker'])
def test_mismatched_process_cannot_run_schema_or_legacy_backfill(split_settings, change, role):
    api = main.create_app(split_settings)
    store = api.state.store
    try:
        run = store.create_run('Synthetic legacy row', '', 'modal', [])
        store.execute("UPDATE runs SET model='' WHERE id=?", (run['id'],))
        before = schema_versions(store)
        with pytest.raises(DatabaseError, match='same shared runtime configuration'):
            main.create_app(split_settings.model_copy(update={'moyai_runtime_role': role, **change}))
        assert schema_versions(store) == before
        assert store.rows('SELECT model FROM runs WHERE id=?', (run['id'],))[0]['model'] == ''
    finally:
        store.close()


def test_duplicate_broker_is_rejected_before_schema_setup(split_settings):
    api = main.create_app(split_settings)
    broker_settings = split_settings.model_copy(update={'moyai_runtime_role': 'broker'})
    broker = main.create_app(broker_settings)
    try:
        before = schema_versions(broker.state.store)
        with pytest.raises(DatabaseError, match='Another inference broker'):
            main.create_app(broker_settings)
        assert schema_versions(broker.state.store) == before
    finally:
        broker.state.store.close()
        api.state.store.close()


def test_changed_coordinator_preserves_live_broker_policy_and_requests(split_settings):
    api = main.create_app(split_settings)
    broker = main.create_app(split_settings.model_copy(update={'moyai_runtime_role': 'broker'}))
    store = broker.state.store
    try:
        run = store.create_run('Synthetic in-flight model request', '', 'modal', [])
        request = broker.state.spend.begin(run, 'synthetic-model')
        before = schema_versions(store)
        api.state.store.close()
        with pytest.raises(DatabaseError, match='Drain and stop'):
            main.create_app(split_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
        assert schema_versions(store) == before
        assert store.rows('SELECT fingerprint FROM runtime_policy')[0]['fingerprint'] == runtime_fingerprint(split_settings)
        assert store.rows('SELECT status FROM model_requests WHERE id=?', (request,))[0]['status'] == 'pending'
    finally:
        store.close()
        api.state.store.close()
    # A drained upgrade remains supported after the last old owner exits.
    replacement = main.create_app(split_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    replacement.state.store.close()


def test_policy_is_published_only_after_component_setup(split_settings, monkeypatch):
    api = main.create_app(split_settings)
    api.state.store.close()
    changed = split_settings.model_copy(update={'moyai_build_sha': 'b' * 40})
    entered, finish = threading.Event(), threading.Event()
    create_components = main._create_app

    def hold_components(settings, store):
        entered.set()
        assert finish.wait(5)
        return create_components(settings, store)

    monkeypatch.setattr(main, '_create_app', hold_components)
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(main.create_app, changed)
        try:
            assert entered.wait(5)
            with pytest.raises(DatabaseError, match='Another Moyai instance'):
                main.create_app(changed.model_copy(update={'moyai_runtime_role': 'worker'}))
        finally:
            finish.set()
        replacement = starting.result(timeout=5)
    try:
        worker = main.create_app(changed.model_copy(update={'moyai_runtime_role': 'worker'}))
        worker.state.store.close()
        assert not replacement.state.store.database.startup_exclusive
    finally:
        replacement.state.store.close()


def test_failed_component_setup_preserves_policy_and_releases_startup_lock(split_settings, monkeypatch):
    api = main.create_app(split_settings)
    api.state.store.close()
    changed = split_settings.model_copy(update={'moyai_build_sha': 'b' * 40})

    def fail_components(settings, store):
        assert store.database.stored_policy() == runtime_fingerprint(split_settings)
        raise RuntimeError('Synthetic startup failure')

    with monkeypatch.context() as patch:
        patch.setattr(main, '_create_app', fail_components)
        with pytest.raises(RuntimeError, match='Synthetic startup failure'):
            main.create_app(changed)
    replacement = main.create_app(changed)
    try:
        assert replacement.state.store.database.stored_policy() == runtime_fingerprint(changed)
    finally:
        replacement.state.store.close()


def test_distributed_store_requires_configuration_before_opening_database(split_settings):
    with pytest.raises(DatabaseError, match='before schema setup'):
        Store(split_settings.data_dir, database_url=split_settings.moyai_database_url,
              database_schema=split_settings.moyai_database_schema, database_initialize=True,
              application_instance=True, runtime_role='worker')
