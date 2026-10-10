"""Read-only rolling release receipt from the exact running source and HTTP PID.

No Store/app initialization, job mutation, customer content or credentials leave
the service. Pending work is allowed. Schema/contracts must already be activated.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROLES = ('api', 'worker', 'broker', 'coordinator')


def require(condition):
    if not condition:
        raise ValueError('Runtime check failed')


def snapshot(connection, schema):
    from psycopg import sql
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    connection.execute("SET LOCAL statement_timeout = '5s'")
    connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    rows = connection.execute('''SELECT l.classid::bigint,l.pid,a.backend_start,l.mode
        FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
        WHERE l.locktype='advisory' AND l.classid IN (726940,726943,726944,726946)
        AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND l.objid=%s::regnamespace::oid AND l.objsubid=2 AND l.granted''', (schema,)).fetchall()
    kinds = {726940: ('owners', 'ShareLock'), 726943: ('coordinators', 'ExclusiveLock'),
             726944: ('brokers', 'ShareLock'), 726946: ('broker_leaders', 'ExclusiveLock')}
    owners = {name: [] for name, _ in kinds.values()}
    for kind, pid, start, mode in rows:
        name, expected = kinds[kind]
        if start is None or mode != expected:
            raise ValueError('Unexpected rolling ownership mode')
        owners[name].append(f'{pid}@{start.isoformat()}')
    policy = connection.execute('SELECT fingerprint FROM runtime_policy WHERE id=1').fetchone()
    schema_state = connection.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone()
    if not policy or not schema_state or schema_state[1]:
        raise ValueError('Missing schema or policy')
    contracts = {}
    for role in ROLES:
        row = connection.execute(sql.SQL('SELECT cluster_fingerprint,fingerprint FROM {} WHERE id=1')
                                 .format(sql.Identifier('runtime_' + role + '_policy'))).fetchone()
        if not row or row[0] != policy[0]:
            raise ValueError('Missing or stale role contract')
        contracts[role] = row[1]
    return {**{key: sorted(value) for key, value in owners.items()},
            'policy': policy[0], 'schema': schema_state[0], 'contracts': contracts}


def health_identity(response, options, state):
    require(response.headers.get('X-Moyai-Build') == options['sha'])
    require(response.headers.get('X-Moyai-Role') == options['role'])
    require(response.headers.get('X-Moyai-Rolling-Release') == '1')
    pid = response.headers.get('X-Moyai-Owner', '')
    matches = [owner for owner in state['owners'] if owner.split('@')[0] == pid]
    require(len(matches) == 1)
    ready = response.status == 200 and json.load(response) == {'status': 'ok'}
    require(ready or response.status == 503)
    coordinator_state, broker_draining = None, None
    if options['role'] == 'coordinator':
        coordinator_state = response.headers.get('X-Moyai-Coordinator-State')
        require(coordinator_state in {'leader', 'standby', 'draining', 'starting', 'failed'})
        require(not ready or coordinator_state in {'leader', 'standby'})
    if options['role'] == 'broker':
        draining = response.headers.get('X-Moyai-Broker-Draining')
        require(draining in {'true', 'false'})
        broker_draining = draining == 'true'
        require(not ready or not broker_draining)
    return {'ready': ready, 'identity': matches[0], 'coordinator_state': coordinator_state,
            'broker_draining': broker_draining}


def probe(options):
    from app.config import Settings
    from app.runtime_compatibility import api_fingerprint, background_fingerprint
    from app.schema_migrations import SCHEMA_REVISION
    from render_start import configure_environment
    import psycopg
    require(not options['staged'] and not options['drain'])
    for name, value in {'RENDER_GIT_COMMIT': options['sha'], 'MOYAI_BUILD_SHA': options['sha'],
                        'MOYAI_RUNTIME_ROLE': options['role'], 'RENDER_MIGRATION_STAGE': 'false',
                        'MAINTENANCE_DRAIN': 'false', 'MOYAI_COORDINATOR_HANDOFF': 'true'}.items():
        require(os.environ.get(name) == value)
    preserved = {key: os.environ.get(key) for key in options['env_keys']}
    require(hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest() == options['env_digest'])
    configure_environment()
    settings = Settings(_env_file=None)
    require(settings.moyai_schema_mode == 'verify' and settings.moyai_separate_broker
            and settings.moyai_coordinator_handoff and settings.temporal_enabled)
    with psycopg.connect(settings.moyai_database_url, connect_timeout=10) as connection:
        state = snapshot(connection, settings.moyai_database_schema)
    contracts = {'api': api_fingerprint(settings), **{
        role: background_fingerprint(settings, role) for role in ('worker', 'broker', 'coordinator')}}
    require(state['schema'] == SCHEMA_REVISION and state['contracts'] == contracts)
    request = urllib.request.Request('http://127.0.0.1:10000/health',
                                    headers={'Host': urlsplit(settings.public_url).netloc})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, TimeoutError):
        return {'ok': True, **state, 'ready': None, 'identity': None}
    with response:
        health = health_identity(response, options, state)
    return {'ok': True, **state, **health}


if __name__ == '__main__':
    try:
        result = probe(json.loads(sys.argv[1]))
    except Exception:
        result = {'ok': False}
    print('MOYAI_RELEASE_PROBE=' + json.dumps(result, sort_keys=True))
