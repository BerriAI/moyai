"""Run the installed offline migrator once, from a verified staged coordinator.

The database credentials never leave Render. The installed migrator acquires
exclusive runtime ownership before DDL and leaves a dirty marker on failure.
A missing response is uncertain: this transport never retries or rolls back.
"""
import json
from pathlib import Path
import re
import sys

INVENTORIES = ('owners', 'coordinators', 'brokers', 'broker_leaders')
COUNTS = ('unsafe_sessions', 'executing_sessions', 'legacy_active_sessions',
          'model_requests', 'live_leases')
NATIVE_COUNTS = ('native_runs', 'native_effects', 'native_requests')


def drained(counts, expected):
    keys = COUNTS + (NATIVE_COUNTS if expected.get('native_required') else ())
    if expected.get('core_switch'):
        keys += ('legacy_core_runs', 'legacy_automations', 'legacy_events')
        if expected.get('native_required'):
            keys += ('native_queued', 'native_parked', 'native_automations', 'native_events',
                     'native_automation_history')
    return all(type(counts.get(key)) is int and counts[key] == 0 for key in keys)


def require(condition):
    if not condition:
        raise ValueError('Offline maintenance migration precondition failed')


def check_snapshot(state, expected, revision):
    require(isinstance(state, dict) and state.get('ok') is True and state.get('ready') is True
            and state.get('staged') is True and state.get('identity') is None)
    require(state.get('schema') == revision and type(state.get('schema')) is int)
    require(all(state.get(key) == expected[key] for key in ('policy', 'contracts')))
    require(all(state.get(key) == [] for key in INVENTORIES))
    require(drained(state, expected))
    require(state.get('source_schema') == expected['source_schema'])


def check_locked(connection, schema, expected, *, drain_counts, owner_pid=None):
    """Recheck after exclusive admission, before the dirty marker or any DDL."""
    from psycopg import sql
    with connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        require(connection.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone()
                == (expected['schema'], 0))
        require(connection.execute('SELECT fingerprint FROM runtime_policy WHERE id=1').fetchone()
                == (expected['policy'],))
        for role in ('api', 'worker', 'broker', 'coordinator'):
            row = connection.execute(sql.SQL('SELECT cluster_fingerprint,fingerprint FROM {} WHERE id=1')
                .format(sql.Identifier('runtime_' + role + '_policy'))).fetchone()
            require(row == (expected['policy'], expected['contracts'][role]))
        locks = connection.execute('''SELECT classid::bigint,pid,mode FROM pg_locks
            WHERE locktype='advisory' AND classid IN (726940,726943,726944,726946)
            AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
            AND objid=%s::regnamespace::oid AND objsubid=2 AND granted''', (schema,)).fetchall()
        # Billing cutover holds this lock on its SQLAlchemy transaction. Its
        # callback supplies that transaction's actual pg_backend_pid; ordinary
        # schema migration continues to require this observer's own connection.
        owner_pid = connection.info.backend_pid if owner_pid is None else owner_pid
        require(type(owner_pid) is int and owner_pid > 0)
        require(locks == [(726940, owner_pid, 'ExclusiveLock')])
        counts = drain_counts(connection)
        require(drained(counts, expected))


def execute(options, *, probe_fn):
    # Import installed source without app.main or any application lifecycle.
    from app.config import Settings
    from app import schema_migrations

    service, expected = options['service'], options['expected']
    require(service['role'] == 'coordinator' and service['staged'] is True
            and service['drain'] is False and service['sha'] == expected['sha'])
    require(type(expected['schema']) is int and type(expected['source_schema']) is int
            and 0 < expected['schema'] < expected['source_schema'])
    require(all(expected[key] == [] for key in INVENTORIES))
    require(isinstance(expected['source_schema_digest'], str)
            and re.fullmatch('[0-9a-f]{64}', expected['source_schema_digest']))
    versions = json.loads(Path(schema_migrations.__file__).with_name('schema_versions.json').read_text())
    require(schema_migrations.SCHEMA_REVISION == expected['source_schema']
            and versions.get(str(expected['source_schema'])) == expected['source_schema_digest'])
    # Probe verifies the exact source/env and clean old schema before any write.
    check_snapshot(probe_fn(service), expected, expected['schema'])
    settings = Settings(_env_file=None)
    require(settings.moyai_runtime_role == 'coordinator' and settings.moyai_schema_mode == 'verify'
            and settings.moyai_coordinator_handoff and settings.moyai_separate_broker
            and not settings.moyai_database_initialize and not settings.maintenance_drain)
    if '_maintenance_counts' in globals():
        counts_fn = globals()['_maintenance_counts']
    else:
        from scripts.release.maintenance_probe import drain_counts as counts_fn
    receipt = schema_migrations.migrate(settings, before_migrate=lambda connection, schema:
        check_locked(connection, schema, expected, drain_counts=counts_fn))
    require(receipt == {'status': 'ready', 'schema_revision': expected['source_schema']})
    # The normal coordinator must publish contracts later, during activation.
    check_snapshot(probe_fn(service), expected, expected['source_schema'])
    return {'status': 'applied', 'from_revision': expected['schema'],
            'to_revision': expected['source_schema'], 'source_digest': expected['source_schema_digest']}


class SSHMaintenanceMigration:
    def __init__(self, key):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'maintenance_migration.py')

    def __call__(self, service, expected):
        from scripts.release.deploy import ReleaseError
        from scripts.release.maintenance_probe import piped_source
        # The nested namespace prevents the probe's __main__ from running.
        source = ("_probe_namespace = {'__name__': 'moyai_maintenance_migration_probe'}\n"
                  'exec(' + repr(piped_source()) + ', _probe_namespace)\n'
                  "_maintenance_probe = _probe_namespace['probe']\n"
                  "_maintenance_counts = _probe_namespace['drain_counts']\n" + self.transport.source.read_text())
        result = self.transport.run(service, {'service': service.probe_options(), 'expected': expected},
            marker='MOYAI_MAINTENANCE_MIGRATION=', timeout=600, source=source)
        receipt = {'status': 'applied', 'from_revision': expected['schema'],
                   'to_revision': expected['source_schema'], 'source_digest': expected['source_schema_digest']}
        if result != receipt:
            raise ReleaseError('Offline schema migration failed or its result is uncertain. All roles remain staged; inspect schema state and the receipt before recovery.')
        return result


if __name__ == '__main__':
    try:
        result = execute(json.loads(sys.argv[1]), probe_fn=globals()['_maintenance_probe'])
    except Exception:
        # Neither SQL diagnostics nor configuration values may reach logs.
        result = {'status': 'unconfirmed'}
    print('MOYAI_MAINTENANCE_MIGRATION=' + json.dumps(result, sort_keys=True))
