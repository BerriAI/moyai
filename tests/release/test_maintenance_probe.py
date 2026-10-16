"""Maintenance observations preserve source identity without admitting a runtime."""
from contextlib import ExitStack
from email.message import Message
import builtins
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg import sql
import pytest

from scripts.release import maintenance_probe as probe
from postgres_fixture import target  # noqa: F401


def response(role='worker', *, staged=False, status=200, owner='42'):
    body = io.BytesIO(json.dumps({'status': 'ok', **({'mode': 'migration_staging'} if staged else {})}).encode())
    body.status, body.headers = status, Message()
    if not staged:
        for name, value in {'X-Moyai-Build': 'a' * 40, 'X-Moyai-Role': role,
                           'X-Moyai-Rolling-Release': '1', 'X-Moyai-Owner': owner,
                           'X-Moyai-Coordinator-State': 'leader',
                           'X-Moyai-Broker-Draining': 'false'}.items():
            body.headers[name] = value
    return body


@pytest.fixture
def observed(monkeypatch):
    options = {'sha': 'a' * 40, 'role': 'worker', 'staged': False, 'drain': False,
               'env_keys': [], 'env_digest': hashlib.sha256(b'{}').hexdigest()}
    contracts = dict(zip(probe._rolling_probe.ROLES, (c * 64 for c in 'abcd')))
    state = {'owners': ['42@start'], 'coordinators': [], 'brokers': [], 'broker_leaders': [],
             'policy': 'f' * 64, 'schema': 18, 'contracts': dict(contracts),
             'unsafe_sessions': 0, 'executing_sessions': 0, 'legacy_active_sessions': 0,
             'model_requests': 0, 'live_leases': 0, 'database_connections': 1}
    settings = SimpleNamespace(moyai_schema_mode='verify', moyai_separate_broker=True,
        moyai_coordinator_handoff=True, temporal_enabled=True, moyai_runtime_role='worker',
        maintenance_drain=False, moyai_database_url='private-dsn', moyai_database_schema='synthetic',
        public_url='https://synthetic.example', default_harness=lambda: 'deepagents-temporal')
    for key, value in {'RENDER_GIT_COMMIT': options['sha'], 'MOYAI_BUILD_SHA': options['sha'],
            'MOYAI_RUNTIME_ROLE': 'worker', 'RENDER_MIGRATION_STAGE': 'false',
            'MAINTENANCE_DRAIN': 'false', 'MOYAI_COORDINATOR_HANDOFF': 'true'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('render_start.configure_environment', lambda: None)
    monkeypatch.setattr('app.config.Settings', lambda **kwargs: settings)
    monkeypatch.setattr('app.database.runtime_fingerprint', lambda _: 'e' * 64)
    monkeypatch.setattr('app.runtime_compatibility.api_fingerprint', lambda _: contracts['api'])
    monkeypatch.setattr('app.runtime_compatibility.background_fingerprint', lambda _, role: contracts[role])
    calls = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: Connection())
    def snapshot(*args):
        calls.append('snapshot')
        return dict(state)
    monkeypatch.setattr(probe, 'snapshot', snapshot)
    monkeypatch.setattr(probe, 'source_schema', lambda *args, **kwargs: {
        'source_schema': 19, 'schema_support': {'protocol': 1, 'required': 19, 'compatibility': 19}})
    def health(*args, **kwargs):
        calls.append('health')
        return response(options['role'], staged=options['staged'])
    monkeypatch.setattr(probe.urllib.request, 'urlopen', health)
    return options, state, settings, calls


@pytest.mark.parametrize('role', probe._rolling_probe.ROLES)
@pytest.mark.parametrize('staged,drain', [(False, False), (False, True), (True, True)])
def test_active_drained_and_staged_roles_have_unambiguous_receipts(observed, monkeypatch, role, staged, drain):
    options, state, settings, calls = observed
    options.update(role=role, staged=staged, drain=drain)
    settings.moyai_runtime_role, settings.maintenance_drain = role, drain
    monkeypatch.setenv('MOYAI_RUNTIME_ROLE', role)
    monkeypatch.setenv('RENDER_MIGRATION_STAGE', str(staged).lower())
    monkeypatch.setenv('MAINTENANCE_DRAIN', str(drain).lower())
    if staged:
        # Candidate source reports its contracts alongside the still-old policy.
        state['contracts'] = {r: '9' * 64 for r in probe._rolling_probe.ROLES}
        state['owners'] = []
    result = probe.result(options)
    assert result['ok'] is result['ready'] is True
    assert result['staged'] is staged and result['drain'] is drain
    assert result['identity'] == (None if staged else '42@start')
    assert result['default_harness'] == 'deepagents-temporal'
    assert result['source_schema'] == 19 and result['schema'] == 18
    assert result['source_policy'] == 'e' * 64 and result['policy'] == 'f' * 64
    assert (result['source_contracts'] == result['contracts']) is not staged
    assert calls == ['snapshot', 'health']


@pytest.mark.parametrize('field', ['MOYAI_BUILD_SHA', 'RENDER_GIT_COMMIT', 'MOYAI_RUNTIME_ROLE',
                                 'MAINTENANCE_DRAIN', 'RENDER_MIGRATION_STAGE', 'MOYAI_COORDINATOR_HANDOFF'])
def test_wrong_process_environment_stops_before_any_observation(observed, monkeypatch, field):
    options, _, _, calls = observed
    monkeypatch.setenv(field, 'wrong')
    assert probe.result(options) == {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'environment'}
    assert calls == []


def test_preserved_secret_digest_mismatch_never_leaks_value(observed, monkeypatch, capsys):
    options, _, _, calls = observed
    options['env_keys'] = ['SESSION_SECRET']
    monkeypatch.setenv('SESSION_SECRET', 'private-value')
    assert probe.result(options) == {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'environment'}
    assert calls == [] and capsys.readouterr() == ('', '')


def test_active_contract_mismatch_is_terminal_even_with_healthy_process(observed):
    options, state, _, calls = observed
    state['contracts']['api'] = '9' * 64
    assert probe.result(options) == {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'contracts'}
    assert calls == ['snapshot']


def test_staged_candidate_reports_other_owners_without_claiming_one(observed, monkeypatch):
    options, state, _, _ = observed
    options['staged'] = True
    monkeypatch.setenv('RENDER_MIGRATION_STAGE', 'true')
    state['owners'] = ['10@coordinator', '20@worker', '30@broker']
    state['contracts'] = {role: '9' * 64 for role in probe._rolling_probe.ROLES}
    value = probe.result(options)
    assert value['ok'] and value['identity'] is None and value['owners'] == state['owners']


@pytest.mark.parametrize('reason,error', [('database_unavailable', psycopg.errors.ConnectionFailure),
    ('database_unavailable', psycopg.errors.AdminShutdown), ('database_error', psycopg.errors.InvalidPassword),
    ('database_error', psycopg.OperationalError)])
def test_only_classified_database_connection_failures_are_retryable(observed, monkeypatch, capsys, reason, error):
    options, _, _, calls = observed
    def failed(*args, **kwargs): raise error('private-error')
    monkeypatch.setattr(psycopg, 'connect', failed)
    assert probe.result(options) == {'ok': False, 'reason': reason}
    assert calls == [] and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('phase', ['snapshot', 'health'])
def test_failure_after_observation_is_not_retryable(observed, monkeypatch, phase):
    from scripts.release.deploy import TRANSIENT_PROBE_FAILURES
    options, _, _, _ = observed
    def failed(*args, **kwargs):
        if phase == 'health': raise TimeoutError('private-error')
        raise psycopg.errors.ConnectionFailure('private-error')
    if phase == 'health': monkeypatch.setattr(probe.urllib.request, 'urlopen', failed)
    else: monkeypatch.setattr(probe, 'snapshot', failed)
    value = probe.result(options)
    assert value == {'ok': False, 'reason': 'health_unavailable' if phase == 'health' else 'database_error'}
    assert value['reason'] not in TRANSIENT_PROBE_FAILURES


@pytest.mark.parametrize('change', ['ordinary_health', 'owner', 'not_ready', 'malformed'])
def test_staged_health_never_qualifies_an_active_or_unverified_process(change):
    body = response(staged=change != 'ordinary_health', status=503 if change == 'not_ready' else 200)
    if change == 'owner': body.headers['X-Moyai-Owner'] = '42'
    if change == 'malformed': body = io.BytesIO(b'not-json'); body.status = 200
    with pytest.raises(ValueError):
        probe.health_identity(body, {'staged': True}, {'owners': []})


def test_active_http_owner_must_be_in_exact_snapshot():
    with pytest.raises(ValueError):
        probe.health_identity(response(owner='99'), {'staged': False, 'sha': 'a' * 40, 'role': 'worker'},
                              {'owners': ['42@start']})


def test_staged_schema_support_can_inspect_older_revision_without_admission(monkeypatch):
    from app.schema_migrations import SCHEMA_REVISION
    def forbidden(*args, **kwargs): raise AssertionError('Staging must not require activated schema')
    monkeypatch.setattr(probe._rolling_probe, 'schema_capability', forbidden)
    monkeypatch.setattr('app.online_migrations.schema_is_compatible', lambda *args: False)
    result = probe.source_schema(None, 'synthetic', SCHEMA_REVISION - 1, staged=True)
    assert result['source_schema'] == result['schema_support']['required'] == SCHEMA_REVISION
    assert result['schema_compatible'] is False and len(result['source_schema_digest']) == 64


def test_staged_schema_identity_rejects_missing_or_malformed_registry_entry(monkeypatch):
    monkeypatch.setattr(probe.Path, 'read_text', lambda *args, **kwargs: '{}')
    with pytest.raises(ValueError): probe.source_schema(None, 'synthetic', 18, staged=True)


def test_active_schema_support_requires_installed_compatibility(monkeypatch):
    def forbidden(*args, **kwargs): raise ValueError('Incompatible schema')
    monkeypatch.setattr(probe._rolling_probe, 'schema_capability', forbidden)
    with pytest.raises(ValueError, match='Incompatible schema'):
        probe.source_schema(None, 'synthetic', 18, staged=False)


def test_pre_capability_source_is_allowed_only_at_exact_revision_when_active(monkeypatch):
    from app.schema_migrations import SCHEMA_REVISION
    original = builtins.__import__
    def pre_bridge(name, *args, **kwargs):
        if name == 'app.online_migrations':
            raise ModuleNotFoundError('not installed', name='app.online_migrations')
        return original(name, *args, **kwargs)
    with patch('builtins.__import__', pre_bridge):
        active = probe.source_schema(None, 'synthetic', SCHEMA_REVISION, staged=False)
        staged = probe.source_schema(None, 'synthetic', SCHEMA_REVISION - 1, staged=True)
        assert active['source_schema'] == staged['source_schema'] == SCHEMA_REVISION
        assert 'schema_support' not in active and 'schema_support' not in staged
        assert active['schema_compatible'] is True and staged['schema_compatible'] is False
        with pytest.raises(ValueError): probe.source_schema(None, 'synthetic', SCHEMA_REVISION - 1, staged=False)


def test_piped_probe_never_depends_on_scripts_inside_render_image():
    source = probe.piped_source()
    original = builtins.__import__
    def restricted(name, *args, **kwargs):
        if name == 'scripts' or name.startswith('scripts.') or name == 'app.main':
            raise AssertionError('Unavailable or unsafe production import')
        return original(name, *args, **kwargs)
    with patch('builtins.__import__', restricted):
        namespace = {'__name__': 'ssh_piped_maintenance_probe'}
        exec(compile(source, 'maintenance_probe.py', 'exec'), namespace)
        assert namespace['health_identity'](response(staged=True), {'staged': True}, {'owners': []})['ready']


def test_transport_forwards_read_only_mode_and_observed_service_options(monkeypatch):
    calls = []
    class Transport:
        def __init__(self, key, **kwargs): self.key = key
        def run(self, *args, **kwargs): calls.append((args, kwargs)); return {'ok': True}
        def invalidate(self, service): calls.append(('invalidate', service))
    monkeypatch.setattr('scripts.release.deploy.SSHProbe', Transport)
    service = SimpleNamespace(probe_options=lambda: {'synthetic': True})
    transport = probe.SSHMaintenanceProbe(Path('/synthetic/key'))
    assert transport(service) == {'ok': True}
    assert calls[0][0] == (service, {'synthetic': True})
    assert calls[0][1]['read_only'] is True and '_rolling_probe' in calls[0][1]['source']
    transport.invalidate(service)
    assert calls[1] == ('invalidate', service)


@pytest.fixture
def database(target):
    url, schema = target
    with ExitStack() as lifetime:
        owners = [lifetime.enter_context(psycopg.connect(url, autocommit=True)) for _ in range(4)]
        for owner in owners:
            owner.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
            owner.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        writer = owners[0]
        writer.execute('SELECT pg_advisory_lock(726943,%s::regnamespace::oid::int)', (schema,))
        owners[1].execute('SELECT pg_advisory_lock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        owners[1].execute('SELECT pg_advisory_lock(726946,%s::regnamespace::oid::int)', (schema,))
        writer.execute('CREATE TABLE schema_state(id integer primary key,revision integer,dirty integer)')
        writer.execute('INSERT INTO schema_state VALUES(1,18,0)')
        writer.execute('CREATE TABLE runtime_policy(id integer primary key,fingerprint text)')
        writer.execute('INSERT INTO runtime_policy VALUES(1,%s)', ('f' * 64,))
        for role, char in zip(probe._rolling_probe.ROLES, 'abcd'):
            table = sql.Identifier('runtime_' + role + '_policy')
            writer.execute(sql.SQL('CREATE TABLE {}(id integer primary key,cluster_fingerprint text,fingerprint text)').format(table))
            writer.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s)').format(table), ('f' * 64, char * 64))
        writer.execute('CREATE TABLE runs(id text primary key,deleted_at text,status text)')
        writer.execute('CREATE TABLE durable_sessions(run_id text primary key,state text)')
        writer.execute('CREATE TABLE model_requests(status text)')
        writer.execute('CREATE TABLE runtime_leases(expires_at timestamptz)')
        yield url, schema, owners


def test_real_postgres_aggregates_and_owner_inventory_share_a_read_only_snapshot(database, monkeypatch):
    url, schema, owners = database
    writer = owners[0]
    writer.execute("INSERT INTO runs VALUES('safe','','running'),('unsafe','','running'),('stop','','stopping'),('legacy','','running'),('gone','deleted','running')")
    writer.execute("INSERT INTO durable_sessions VALUES('safe','{\"phase\":\"waiting_credential\"}'),('unsafe','{\"phase\":\"execute\"}'),('stop','{\"phase\":\"idle\"}'),('gone','{\"phase\":\"execute\"}')")
    writer.execute("INSERT INTO model_requests VALUES('pending'),('running'),('completed')")
    writer.execute("INSERT INTO runtime_leases VALUES(clock_timestamp()+interval '1 hour'),(clock_timestamp()-interval '1 hour')")
    original = probe._rolling_probe.snapshot
    def concurrent_write(connection, schema):
        state = original(connection, schema)
        writer.execute("INSERT INTO runs VALUES('late','','running')")
        writer.execute('UPDATE schema_state SET revision=19')
        return state
    monkeypatch.setattr(probe._rolling_probe, 'snapshot', concurrent_write)
    with psycopg.connect(url) as reader:
        result = probe.snapshot(reader, schema)
        assert result['schema'] == 18
        assert result['unsafe_sessions'] == 2 and result['executing_sessions'] == 1
        assert result['legacy_active_sessions'] == 1 and result['model_requests'] == 2
        assert result['live_leases'] == 1
        assert len(result['owners']) == 4 and len(result['coordinators']) == len(result['broker_leaders']) == 1
        assert result['brokers'] == result['broker_leaders']
        assert any(identity.startswith(str(writer.info.backend_pid) + '@') for identity in result['owners'])
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction): reader.execute('DELETE FROM runs')


@pytest.mark.parametrize('change', ['dirty', 'stale_contract', 'missing_contract'])
def test_staged_snapshot_cannot_qualify_dirty_or_incoherent_database(database, change):
    url, schema, owners = database
    writer = owners[0]
    if change == 'dirty': writer.execute('UPDATE schema_state SET dirty=1')
    elif change == 'stale_contract': writer.execute("UPDATE runtime_api_policy SET cluster_fingerprint='wrong'")
    else: writer.execute('DELETE FROM runtime_coordinator_policy')
    with psycopg.connect(url) as reader, pytest.raises(ValueError): probe.snapshot(reader, schema)


@pytest.mark.parametrize('installed_layout', ['scheduler', 'definitions'])
def test_automation_snapshot_uses_installed_schedule_version(database, monkeypatch, installed_layout):
    from app import automation_definitions
    url, schema, owners = database
    version = 7
    if installed_layout == 'definitions':
        monkeypatch.setattr(automation_definitions, 'SCHEDULE_VERSION', version)
        legacy_version = 99  # A valid current definition must not use the fallback.
    else:
        monkeypatch.delattr(automation_definitions, 'SCHEDULE_VERSION')
        legacy_version = version
    monkeypatch.setitem(sys.modules, 'app.automations', SimpleNamespace(SCHEDULE_VERSION=legacy_version))
    writer = owners[0]
    writer.execute('''CREATE TABLE automations(
        paused integer, revision integer, synced_revision integer,
        synced_schedule_version integer, sync_error text)''')
    writer.execute('''INSERT INTO automations VALUES
        (1,2,2,%s,''), (1,2,2,%s,''), (0,2,2,%s,''), (1,2,2,%s,'unconfirmed')''',
        (version, version - 1, version, version))
    with psycopg.connect(url) as reader:
        result = probe.snapshot(reader, schema)
        assert result['legacy_automations'] == 3
        assert result['legacy_events'] == result['legacy_schedule_count'] == 0
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            reader.execute('DELETE FROM automations')


@pytest.mark.parametrize('version', [None, True, 0, -1, '1'])
def test_invalid_installed_schedule_version_cannot_fall_back(database, monkeypatch, version):
    from app import automation_definitions
    url, schema, owners = database
    monkeypatch.setattr(automation_definitions, 'SCHEDULE_VERSION', version)
    monkeypatch.setitem(sys.modules, 'app.automations', SimpleNamespace(SCHEDULE_VERSION=1))
    owners[0].execute('CREATE TABLE automations(id integer)')
    with psycopg.connect(url) as reader, pytest.raises(ValueError, match='Runtime check failed'):
        probe.snapshot(reader, schema)
