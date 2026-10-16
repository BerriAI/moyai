"""Canonical storage services used by migration tests; no executor or HTTP server."""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from app.config import Settings
from app.db import Store
from app.security import Security
from app.spend import Spend
from app.schema_migrations import initialize_components, initialize_defaults
from postgres_fixture import target  # noqa: F401


@pytest.fixture
def split_settings(tmp_path, target):
    url, schema = target
    return Settings(_env_file=None, data_dir=tmp_path / 'api',
        moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
        moyai_runtime_role='coordinator', moyai_separate_broker=True,
        moyai_build_sha='a' * 40, temporal_enabled=True, temporal_tls=False,
        object_storage_bucket='synthetic-broker-test', session_secret='synthetic-session-key',
        encryption_key=Fernet.generate_key().decode(), session_titles_enabled=False,
        memory_review_enabled=False, litellm_spend_recovery_enabled=False)


def schema_versions(store):
    return store.rows("""SELECT proname,xmin::text AS version FROM pg_proc
        WHERE pronamespace=?::regnamespace AND proname IN ('unicode_lower','json_text','json_number')
        ORDER BY proname""", (store.database.schema,))


def open_services(settings):
    store = Store(settings.data_dir, default_model=settings.resolve_model(),
        database_url=settings.moyai_database_url, database_schema=settings.moyai_database_schema,
        database_initialize=settings.moyai_database_initialize, application_instance=True,
        database_pool_size=settings.moyai_database_pool_size, runtime_role=settings.moyai_runtime_role,
        runtime_settings=settings, schema_mode=settings.moyai_schema_mode)
    try:
        security = Security(settings)
        if store.schema_updates:
            initialize_components(store)
            key = settings.encryption_key or (settings.data_dir / 'encryption.key').read_text().strip()
            initialize_defaults(store, settings.model_copy(update={'encryption_key': key}))
        spend = Spend(store, settings, security, None)
        if store.database:
            store.database.configure_runtime(settings)
        return SimpleNamespace(state=SimpleNamespace(store=store, security=security, spend=spend))
    except BaseException:
        store.close()
        raise
