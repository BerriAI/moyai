"""One bounded online expansion over existing private Render SSH access.

The candidate supplies a declarative plan to the installed, versioned migration
engine. Database credentials stay on the service. A lost response is ambiguous;
the caller persists intent before this single invocation and never retries it.
"""
import json
import sys


def require(condition):
    if not condition:
        raise ValueError('Online migration precondition failed')


def check_runtime(connection, schema, expected):
    """Hold contracts stable and recheck owners inside the DDL transaction."""
    from psycopg import sql

    connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    policy = connection.execute('SELECT fingerprint FROM runtime_policy WHERE id=1 FOR SHARE').fetchone()
    require(policy == (expected['policy'],))
    for role in ('api', 'worker', 'broker', 'coordinator'):
        row = connection.execute(sql.SQL('SELECT cluster_fingerprint,fingerprint FROM {} WHERE id=1 FOR SHARE')
            .format(sql.Identifier('runtime_' + role + '_policy'))).fetchone()
        require(row == (expected['policy'], expected['contracts'][role]))
    state = connection.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone()
    require(state == (expected['schema'], 0))
    rows = connection.execute('''SELECT l.classid::bigint,l.pid,a.backend_start,l.mode
        FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
        WHERE l.locktype='advisory' AND l.classid IN (726940,726943,726944,726946)
        AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND l.objid=%s::regnamespace::oid AND l.objsubid=2 AND l.granted
        AND l.pid<>pg_backend_pid()''', (schema,)).fetchall()
    kinds = {726940: ('owners', 'ShareLock'), 726943: ('coordinators', 'ExclusiveLock'),
             726944: ('brokers', 'ShareLock'), 726946: ('broker_leaders', 'ExclusiveLock')}
    owners = {name: [] for name, _ in kinds.values()}
    for kind, pid, started, mode in rows:
        name, wanted_mode = kinds[kind]
        require(started is not None and mode == wanted_mode)
        owners[name].append(f'{pid}@{started.isoformat()}')
    require(all(sorted(value) == sorted(expected[key]) for key, value in owners.items()))


def execute(options, *, probe_fn):
    # Import installed bridge code, never app.main or application lifecycles.
    from app.config import Settings
    from app.online_migrations import apply_plan
    import psycopg

    service, expected, plan = options['service'], options['expected'], options['plan']
    state = probe_fn(service)
    require(state.get('ok') is True and state.get('ready') is True)
    require(state.get('identity') == expected['identities'][options['service_id']])
    require(all(state.get(key) == expected[key] for key in (
        'policy', 'contracts', 'schema', 'owners', 'coordinators', 'brokers', 'broker_leaders')))
    support = state.get('schema_support', {})
    require(support.get('protocol') == 1 and support.get('compatibility') == plan['compatibility'])
    require(plan['from_revision'] == expected['schema'] and plan['to_revision'] > expected['schema'])
    settings = Settings(_env_file=None)
    require(settings.moyai_schema_mode == 'verify' and settings.moyai_coordinator_handoff
            and settings.moyai_separate_broker and settings.moyai_runtime_role == 'coordinator')
    with psycopg.connect(settings.moyai_database_url, autocommit=True, connect_timeout=10) as connection:
        return apply_plan(connection, settings.moyai_database_schema, plan,
            before_apply=lambda: check_runtime(connection, settings.moyai_database_schema, expected))


class SSHMigration:
    def __init__(self, key):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'online_migration.py')
        self.probe_source = ROOT / 'rolling_probe.py'

    def __call__(self, service, plan, expected_state):
        from scripts.release.deploy import ReleaseError
        # Render images contain app/, not scripts/. Send the read-only helper
        # with the runner; its imports still inspect the installed app source.
        source = ("_probe_namespace = {'__name__': 'moyai_migration_probe'}\n"
                  'exec(' + repr(self.probe_source.read_text()) + ', _probe_namespace)\n'
                  "_migration_probe = _probe_namespace['probe']\n" + self.transport.source.read_text())
        result = self.transport.run(service,
            {'service_id': service.id, 'service': service.probe_options(),
             'plan': plan, 'expected': expected_state},
            marker='MOYAI_ONLINE_MIGRATION=', timeout=60, source=source)
        if (not isinstance(result, dict) or set(result) != {'status', 'from_revision', 'to_revision', 'digest'}
                or not isinstance(result['status'], str) or result['status'] not in {'applied', 'already_applied'}
                or any(result.get(key) != plan[key] for key in ('from_revision', 'to_revision', 'digest'))):
            raise ReleaseError('Online migration failed or its result is uncertain. No services were replaced; inspect the migration receipt before retrying.')
        return result


if __name__ == '__main__':
    try:
        result = execute(json.loads(sys.argv[1]), probe_fn=globals()['_migration_probe'])
    except Exception:
        # SQL exceptions and settings may contain credentials or private data.
        result = {'status': 'unconfirmed'}
    print('MOYAI_ONLINE_MIGRATION=' + json.dumps(result, sort_keys=True))
