"""Read-only API release probe, sent over SSH to the running source tree.

Never import app.main or construct a Store. Return only contract hashes, process
identities and readiness. In-flight jobs are allowed and never drained here.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def snapshot(connection, schema):
    from psycopg import sql
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    connection.execute("SET LOCAL statement_timeout = '5s'")
    connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    rows = connection.execute('''SELECT l.classid::bigint,l.pid,a.backend_start,l.mode
        FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
        WHERE l.locktype='advisory' AND l.classid IN (726940,726943,726944)
        AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND l.objid=%s::regnamespace::oid AND l.objsubid=2 AND l.granted''', (schema,)).fetchall()
    owners = {key: [] for key in ('owners', 'coordinators', 'brokers')}
    for kind, pid, start, mode in rows:
        if start is None or mode != ('ShareLock' if kind == 726940 else 'ExclusiveLock'):
            raise ValueError('Unexpected database ownership')
        owners[{726940: 'owners', 726943: 'coordinators', 726944: 'brokers'}[kind]].append(f'{pid}@{start.isoformat()}')
    policy = connection.execute('''SELECT p.fingerprint,a.cluster_fingerprint,a.fingerprint
        FROM runtime_policy p JOIN runtime_api_policy a ON a.id=p.id WHERE p.id=1''').fetchone()
    schema_state = connection.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone()
    if not policy or policy[0] != policy[1] or not schema_state or schema_state[1]:
        raise ValueError('Missing or stale compatibility contract')
    return {**{k: sorted(v) for k, v in owners.items()}, 'policy': policy[0],
            'contract': policy[2], 'schema': schema_state[0]}


def probe(options):
    from app.config import Settings
    from app.database import runtime_fingerprint
    from app.runtime_compatibility import api_fingerprint
    from app.schema_migrations import SCHEMA_REVISION
    from render_start import configure_environment
    from scripts.release.probe import require
    import psycopg

    require(not options['staged'] and not options['drain'])
    for name, value in {'RENDER_GIT_COMMIT': options['sha'], 'MOYAI_BUILD_SHA': options['sha'],
                        'MOYAI_RUNTIME_ROLE': options['role'], 'RENDER_MIGRATION_STAGE': 'false',
                        'MAINTENANCE_DRAIN': 'false'}.items():
        require(os.environ.get(name) == value)
    preserved = {key: os.environ.get(key) for key in options['env_keys']}
    require(hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest() == options['env_digest'])
    configure_environment()
    settings = Settings(_env_file=None)
    require(settings.moyai_schema_mode == 'verify' and settings.moyai_separate_broker)
    with psycopg.connect(settings.moyai_database_url, connect_timeout=10) as connection:
        state = snapshot(connection, settings.moyai_database_schema)
    require(state['schema'] == SCHEMA_REVISION and state['contract'] == api_fingerprint(settings))
    if options['role'] != 'api':
        require(state['policy'] == runtime_fingerprint(settings))
    request = urllib.request.Request('http://127.0.0.1:10000/health',
                                    headers={'Host': urlsplit(settings.public_url).netloc})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, TimeoutError):
        return {'ok': True, **state, 'ready': None, 'api_owner': None}
    with response:
        # Headers must identify the serving process even for an explicit 503.
        owner = None
        if options['role'] == 'api':
            require(response.headers.get('X-Moyai-Build') == options['sha'])
            require(response.headers.get('X-Moyai-API-Release') == '1')
            pid = response.headers.get('X-Moyai-Owner', '')
            matches = [value for value in state['owners'] if value.split('@')[0] == pid]
            require(len(matches) == 1)
            owner = matches[0]
        ready = response.status == 200 and json.load(response) == {'status': 'ok'}
        # Only a correlated explicit health failure may trigger rollback. Unknown
        # responses/timeouts may mean a networking fault or an old SSH instance.
        require(ready or response.status == 503)
    return {'ok': True, **state, 'ready': ready, 'api_owner': owner}


if __name__ == '__main__':
    try:
        result = probe(json.loads(sys.argv[1]))
    except Exception:
        result = {'ok': False}
    print('MOYAI_RELEASE_PROBE=' + json.dumps(result, sort_keys=True))
