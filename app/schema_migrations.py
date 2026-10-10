"""Offline schema setup and fail-closed, opt-in PostgreSQL runtime admission.

Increment SCHEMA_REVISION when changing any initializer or default/backfill below.
The migrator holds the exclusive runtime lock through the final readiness receipt.
It never imports app.main, constructs clients, or starts application lifecycles.
"""
import argparse
import base64
import importlib
import json

from .database import DatabaseError, identifier

SCHEMA_REVISION = 2
# Dependency order; optional features are prepared even when disabled at runtime.
COMPONENTS = (
    'user_roles', 'sandbox_settings', 'github_repositories', 'github_write_access',
    'github', 'session_pull_requests', 'durable_runner', 'prepared_sandboxes',
    'user_preferences', 'environments', 'infrastructure_costs', 'spend', 'agents',
    'tracing', 'credentials', 'skills', 'memory', 'memory_review', 'lens_feedback',
    'session_titles', 'automations', 'automation_events', 'automation_tools',
    'model_tools', 'session_folders', 'media_shares', 'computer', 'github_setup',
)


def prepare_schema(owner, schema, mode):
    table = identifier(schema) + '.schema_state'
    exists = owner.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]
    state = owner.execute(f'SELECT revision,dirty FROM {table} WHERE id=1').fetchone() if exists else None
    if mode == 'auto':
        if exists:
            raise DatabaseError('This schema uses explicit migrations. Set MOYAI_SCHEMA_MODE=verify.')
        return
    if mode == 'verify':
        if state != (SCHEMA_REVISION, 0):
            raise DatabaseError('Schema is missing, incomplete, or incompatible. Run the offline migration command before starting Moyai.')
        return
    if state and state[0] > SCHEMA_REVISION:
        raise DatabaseError('Database schema is newer than this migrator. Use the current release; downgrades are not supported.')
    # Commit a dirty marker before the first schema/data change. A failed or
    # killed migration can be retried, but cannot leave an old ready receipt.
    with owner.transaction():
        owner.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, dirty INTEGER NOT NULL)''')
        owner.execute(f'''INSERT INTO {table} VALUES(1,%s,1)
            ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,dirty=1''', (SCHEMA_REVISION,))


def initialize_components(store):
    for name in COMPONENTS:
        importlib.import_module('.' + name, __package__).initialize_schema(store)
    from .trace_outbox import TABLES, initialize_schema
    for table in sorted(TABLES):
        initialize_schema(store, table)


def initialize_defaults(store, settings):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    store.execute('INSERT INTO organization(id,name) VALUES(1,?) ON CONFLICT DO NOTHING', (settings.organization_name,))
    if not store.rows('SELECT 1 FROM sandbox_settings WHERE id=1'):
        key = settings.substrate_signing_key or base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        encrypted = Fernet(settings.encryption_key.encode()).encrypt(json.dumps({'substrate_signing_key': key}).encode()).decode()
        store.execute('INSERT INTO sandbox_settings VALUES(1,0,?)', (encrypted,))


def migrate(settings):
    from .db import Store

    if not settings.moyai_database_url or not settings.encryption_key or not settings.session_secret:
        raise DatabaseError('Offline migration requires PostgreSQL and the existing explicit ENCRYPTION_KEY and SESSION_SECRET.')
    store = Store(settings.data_dir, default_model=settings.resolve_model(),
        database_url=settings.moyai_database_url, database_schema=settings.moyai_database_schema,
        database_initialize=settings.moyai_database_initialize, database_pool_size=1,
        application_instance=True, runtime_role='standalone', schema_mode='migrate')
    try:
        initialize_components(store)
        initialize_defaults(store, settings)
        # No runtime policy is published and no pending job is recovered.
        store.execute('UPDATE schema_state SET dirty=0 WHERE id=1 AND revision=?', (SCHEMA_REVISION,))
        return {'schema_revision': SCHEMA_REVISION, 'status': 'ready'}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', required=True,
        help='Apply schema/backfills while all runtime owners are stopped; never starts jobs.')
    parser.parse_args()
    from .config import Settings
    try:
        print(json.dumps(migrate(Settings())))
    except DatabaseError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
