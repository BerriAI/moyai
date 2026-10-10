"""Read-only production checks, piped over SSH; no Store/app initialization.

Only aggregate results leave the service. Never print exception bodies, settings,
connection strings, prompts or session identifiers.
"""
import hashlib
import json
import os
import sys
import urllib.request
from urllib.parse import urlsplit

SAFE_PHASES = ('idle', 'warm', 'prepare', 'provision', 'waiting_environment',
               'install', 'startup_wait', 'transport_wait', 'waiting_children',
               'waiting_credential')
POLICY_FIELDS = ('session_secret', 'encryption_key', 'object_storage_bucket',
                 'object_storage_endpoint', 'object_storage_prefix', 'public_url',
                 'temporal_address', 'temporal_namespace', 'temporal_task_queue',
                 'max_concurrent_runs', 'max_pending_runs', 'max_concurrent_model_requests',
                 'moyai_build_sha')


def snapshot(connection, schema):
    from psycopg import sql
    # PostgreSQL enforces this even if this probe is accidentally changed later.
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    connection.execute("SET LOCAL statement_timeout = '5s'")
    connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    owners = dict(connection.execute('''SELECT classid::bigint,count(*) FROM pg_locks
        WHERE locktype='advisory' AND classid IN (726940,726943,726944)
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND objid=%s::regnamespace::oid AND objsubid=2 AND granted GROUP BY classid''', (schema,)).fetchall())
    return {
        'owners': owners.get(726940, 0), 'coordinators': owners.get(726943, 0),
        'brokers': owners.get(726944, 0),
        'unsafe_sessions': connection.execute('''SELECT count(*) FROM durable_sessions d
            JOIN runs r ON r.id=d.run_id WHERE r.deleted_at='' AND
            (COALESCE(d.state::jsonb->>'phase','idle') != ALL(%s) OR r.status='stopping')''',
            (list(SAFE_PHASES),)).fetchone()[0],
        'legacy_active_sessions': connection.execute('''SELECT count(*) FROM runs r
            WHERE r.deleted_at='' AND r.status NOT IN ('queued','idle','completed','failed','cancelled','interrupted')
            AND NOT EXISTS(SELECT 1 FROM durable_sessions d WHERE d.run_id=r.id)''').fetchone()[0],
        'model_requests': connection.execute("SELECT count(*) FROM model_requests WHERE status IN ('pending','running')").fetchone()[0],
        'live_leases': connection.execute('SELECT count(*) FROM runtime_leases WHERE expires_at>clock_timestamp()').fetchone()[0],
        'database_connections': connection.execute('SELECT count(*) FROM pg_stat_activity').fetchone()[0],
        'policy': connection.execute('SELECT fingerprint FROM runtime_policy WHERE id=1').fetchone()[0],
    }


def require(condition):
    if not condition:
        raise ValueError('Runtime check failed')


def policy_fingerprint(settings):
    # The first split-capable release adds a policy field even while disabled.
    # Select the schema from the running build's Settings, never from a failed
    # comparison, so the same release can verify its pre-upgrade baseline.
    fields = POLICY_FIELDS + (('moyai_separate_broker',) if hasattr(settings, 'moyai_separate_broker') else ())
    return hashlib.sha256(json.dumps({key: getattr(settings, key) for key in fields}, sort_keys=True).encode()).hexdigest()


def probe(options):
    from render_start import configure_environment
    from app.config import Settings
    import psycopg

    require(os.environ.get('RENDER_GIT_COMMIT') == options['sha'])
    require(os.environ.get('MOYAI_BUILD_SHA') == options['sha'])
    require(os.environ.get('MOYAI_RUNTIME_ROLE') == options['role'])
    require(os.environ.get('RENDER_MIGRATION_STAGE', '').lower() == str(options['staged']).lower())
    require(os.environ.get('MAINTENANCE_DRAIN', '').lower() == str(options['drain']).lower())
    preserved = {key: os.environ.get(key) for key in options['env_keys']}
    digest = hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest()
    require(digest == options['env_digest'])
    configure_environment()
    settings = Settings(_env_file=None)
    request = urllib.request.Request('http://127.0.0.1:10000/health',
                                      headers={'Host': urlsplit(settings.public_url).netloc})
    with urllib.request.urlopen(request, timeout=10) as response:
        health = json.load(response)
        require(response.status == 200)
    if options['staged']:
        require(health == {'status': 'ok', 'mode': 'migration_staging'})
        return {'ok': True, 'staged': True}
    require(health == {'status': 'ok'})
    with psycopg.connect(settings.moyai_database_url, connect_timeout=10) as connection:
        state = snapshot(connection, settings.moyai_database_schema)
    expected = policy_fingerprint(settings)
    require(state.pop('policy') == expected)
    return {'ok': True, **state}


if __name__ == '__main__':
    try:
        result = probe(json.loads(sys.argv[1]))
    except Exception:
        # SSH may reach the draining old instance during Render's overlap.
        print('MOYAI_RELEASE_PROBE=' + json.dumps({'ok': False}))
        sys.exit(1)
    print('MOYAI_RELEASE_PROBE=' + json.dumps(result, sort_keys=True))
