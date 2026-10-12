"""Read-only maintenance receipt from the installed source and local HTTP process.

Staging has no application owner and may inspect older activated contracts. It
never starts Store, admits a runtime, migrates schema, or reads customer content.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

if '_rolling_probe' not in globals():
    from scripts.release import rolling_probe as _rolling_probe

ProbeFailure = _rolling_probe.ProbeFailure
require = _rolling_probe.require
validation_stage = _rolling_probe.validation_stage
SAFE_PHASES = ('idle', 'warm', 'prepare', 'provision', 'waiting_environment',
               'install', 'startup_wait', 'transport_wait', 'waiting_children',
               'waiting_credential')


def snapshot(connection, schema):
    # The helper establishes one read-only repeatable-read transaction. Keep
    # aggregates in that same transaction as schema, policy, and ownership.
    state = _rolling_probe.snapshot(connection, schema)
    state.update(drain_counts(connection))
    return state


def drain_counts(connection):
    """Aggregate only; caller owns transaction isolation and schema search_path."""
    return {
        'unsafe_sessions': connection.execute('''SELECT count(*) FROM durable_sessions d
            JOIN runs r ON r.id=d.run_id WHERE r.deleted_at='' AND
            (COALESCE(d.state::jsonb->>'phase','idle') != ALL(%s) OR r.status='stopping')''',
            (list(SAFE_PHASES),)).fetchone()[0],
        'executing_sessions': connection.execute('''SELECT count(*) FROM durable_sessions d
            JOIN runs r ON r.id=d.run_id WHERE r.deleted_at='' AND
            COALESCE(d.state::jsonb->>'phase','idle') != ALL(%s)''',
            (list(SAFE_PHASES),)).fetchone()[0],
        'legacy_active_sessions': connection.execute('''SELECT count(*) FROM runs r
            WHERE r.deleted_at='' AND r.status NOT IN ('queued','idle','completed','failed','cancelled','interrupted')
            AND NOT EXISTS(SELECT 1 FROM durable_sessions d WHERE d.run_id=r.id)''').fetchone()[0],
        'model_requests': connection.execute("SELECT count(*) FROM model_requests WHERE status IN ('pending','running')").fetchone()[0],
        'live_leases': connection.execute('SELECT count(*) FROM runtime_leases WHERE expires_at>clock_timestamp()').fetchone()[0],
        'database_connections': connection.execute('SELECT count(*) FROM pg_stat_activity').fetchone()[0],
    }


def source_schema(connection, schema, revision, *, staged):
    from app import schema_migrations
    SCHEMA_REVISION = schema_migrations.SCHEMA_REVISION
    digest = json.loads(Path(schema_migrations.__file__).with_name('schema_versions.json').read_text()).get(str(SCHEMA_REVISION))
    require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest))
    identity = {'source_schema': SCHEMA_REVISION, 'source_schema_digest': digest}
    if not staged:
        return {**identity, 'schema_compatible': True,
                **_rolling_probe.schema_capability(connection, schema, revision)}
    try:
        from app.online_migrations import schema_support, schema_is_compatible
    except ModuleNotFoundError as error:
        if error.name != 'app.online_migrations':
            raise
        return {**identity, 'schema_compatible': revision == SCHEMA_REVISION}
    support = schema_support()
    require(isinstance(support, dict) and set(support) == {'protocol', 'required', 'compatibility'}
            and all(type(value) is int for value in support.values())
            and support['protocol'] == 1 and support['required'] == SCHEMA_REVISION
            and 0 < support['compatibility'] <= support['required'])
    # A staged candidate intentionally sees the old schema before migration.
    # Reporting capability does not qualify that schema for application startup.
    return {**identity, 'schema_support': support,
            'schema_compatible': schema_is_compatible(connection, schema, support['required'], support['compatibility'])}


def health_identity(response, options, state):
    if not options['staged']:
        return _rolling_probe.health_identity(response, options, state)
    require(response.status == 200 and json.load(response) == {'status': 'ok', 'mode': 'migration_staging'})
    # Staging imports render_start's maintenance app, never the application owner.
    require(not any(response.headers.get(name) for name in (
        'X-Moyai-Owner', 'X-Moyai-Rolling-Release', 'X-Moyai-Coordinator-State', 'X-Moyai-Broker-Draining')))
    return {'ready': True, 'identity': None, 'coordinator_state': None, 'broker_draining': None}


def probe(options):
    with validation_stage('environment'):
        require(options['role'] in _rolling_probe.ROLES and type(options['staged']) is bool
                and type(options['drain']) is bool)
        for name, value in {'RENDER_GIT_COMMIT': options['sha'], 'MOYAI_BUILD_SHA': options['sha'],
                            'MOYAI_RUNTIME_ROLE': options['role'],
                            'RENDER_MIGRATION_STAGE': str(options['staged']).lower(),
                            'MAINTENANCE_DRAIN': str(options['drain']).lower(),
                            'MOYAI_COORDINATOR_HANDOFF': 'true'}.items():
            require(os.environ.get(name) == value)
        preserved = {key: os.environ.get(key) for key in options['env_keys']}
        require(hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest() == options['env_digest'])
    with validation_stage('configuration'):
        from app.config import Settings
        from app.database import runtime_fingerprint
        from app.runtime_compatibility import api_fingerprint, background_fingerprint
        from render_start import configure_environment
        import psycopg
        configure_environment()
        settings = Settings(_env_file=None)
        require(settings.moyai_schema_mode == 'verify' and settings.moyai_separate_broker
                and settings.moyai_coordinator_handoff and settings.temporal_enabled
                and settings.moyai_runtime_role == options['role']
                and settings.maintenance_drain is options['drain'])
        contracts = {'api': api_fingerprint(settings), **{
            role: background_fingerprint(settings, role) for role in ('worker', 'broker', 'coordinator')}}
        source_policy = runtime_fingerprint(settings)
        default_harness = settings.default_harness()
    try:
        connection = psycopg.connect(settings.moyai_database_url, connect_timeout=10)
    except psycopg.Error as error:
        transient = error.sqlstate in {'08000', '08001', '08003', '08006', '08007',
                                      '57P01', '57P02', '57P03'}
        raise ProbeFailure('database_unavailable' if transient else 'database_error') from None
    with validation_stage('snapshot'):
        try:
            with connection:
                state = snapshot(connection, settings.moyai_database_schema)
                state.update(source_schema(connection, settings.moyai_database_schema, state['schema'],
                                           staged=options['staged']))
        except psycopg.Error:
            raise ProbeFailure('database_error') from None
    with validation_stage('contracts'):
        if not options['staged']:
            require(state['contracts'] == contracts)
    request = urllib.request.Request('http://127.0.0.1:10000/health',
                                    headers={'Host': urlsplit(settings.public_url).netloc})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
        raise ProbeFailure('health_unavailable') from None
    with validation_stage('health_identity'):
        try:
            with response:
                health = health_identity(response, options, state)
        except (TimeoutError, ConnectionError, http.client.HTTPException):
            raise ProbeFailure('health_unavailable') from None
    return {'ok': True, 'staged': options['staged'], 'drain': options['drain'],
            'default_harness': default_harness, 'source_policy': source_policy,
            'source_contracts': contracts, **state, **health}


def result(options):
    try:
        return probe(options)
    except ProbeFailure as error:
        return {'ok': False, 'reason': error.reason, **({'stage': error.stage} if error.stage else {})}
    except Exception:
        return {'ok': False, 'reason': 'runtime_validation_failed'}


def piped_source():
    from scripts.release.deploy import ROOT
    # Render packages app/, not scripts/. Supply the read-only helper without
    # replacing imports from the installed app used for contract verification.
    return ("import types\n_rolling_probe = types.ModuleType('moyai_maintenance_rolling_probe')\n"
            'exec(' + repr((ROOT / 'rolling_probe.py').read_text()) + ', _rolling_probe.__dict__)\n'
            + (ROOT / 'maintenance_probe.py').read_text())


class SSHMaintenanceProbe:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'maintenance_probe.py', connections=connections)

    def invalidate(self, service):
        self.transport.invalidate(service)

    def __call__(self, service):
        return self.transport.run(service, service.probe_options(), marker='MOYAI_RELEASE_PROBE=',
                                  source=piped_source(), read_only=True)


if __name__ == '__main__':
    try:
        value = result(json.loads(sys.argv[1]))
    except Exception:
        value = {'ok': False, 'reason': 'probe_request_invalid'}
    print('MOYAI_RELEASE_PROBE=' + json.dumps(value, sort_keys=True))
