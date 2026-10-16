"""Actual PostgreSQL locks/contracts and process-correlated health receipts."""
from contextlib import ExitStack
from email.message import Message
import io
import json
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
import pytest

from scripts.release.deploy import GitHub, ReleaseError
from scripts.release.rolling_probe import health_identity, snapshot
from postgres_fixture import target  # noqa: F401


@pytest.fixture
def owners(target):
    url, schema = target
    with ExitStack() as lifetime:
        connections = [lifetime.enter_context(psycopg.connect(url, autocommit=True)) for _ in range(4)]
        for connection in connections:
            connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
            connection.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        coordinator, worker, broker, api = connections
        coordinator.execute('SELECT pg_advisory_lock(726943,%s::regnamespace::oid::int)', (schema,))
        broker.execute('SELECT pg_advisory_lock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        broker.execute('SELECT pg_advisory_lock(726946,%s::regnamespace::oid::int)', (schema,))
        coordinator.execute('CREATE TABLE runtime_policy(id integer primary key,fingerprint text)')
        coordinator.execute('INSERT INTO runtime_policy VALUES(1,%s)', ('f' * 64,))
        coordinator.execute('CREATE TABLE schema_state(id integer primary key,revision integer,dirty integer)')
        coordinator.execute('INSERT INTO schema_state VALUES(1,5,0)')
        for role, digit in zip(('api', 'worker', 'broker', 'coordinator'), 'abcd'):
            coordinator.execute(sql.SQL('CREATE TABLE {} (id integer primary key,cluster_fingerprint text,fingerprint text)')
                                .format(sql.Identifier('runtime_' + role + '_policy')))
            coordinator.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s)')
                .format(sql.Identifier('runtime_' + role + '_policy')), ('f' * 64, digit * 64))
        yield url, schema, connections


def test_read_only_snapshot_tracks_shared_brokers_and_singleton_leaders(owners):
    url, schema, connections = owners
    with psycopg.connect(url) as reader:
        state = snapshot(reader, schema)
        assert len(state['owners']) == 4
        assert len(state['coordinators']) == len(state['brokers']) == len(state['broker_leaders']) == 1
        assert state['brokers'] == state['broker_leaders']
        assert state['coordinators'][0].startswith(str(connections[0].info.backend_pid) + '@')
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            reader.execute('DELETE FROM runtime_policy')
    with psycopg.connect(url, autocommit=True) as replacement:
        replacement.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        replacement.execute('SELECT pg_advisory_lock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        with psycopg.connect(url) as reader:
            overlap = snapshot(reader, schema)
            assert len(overlap['owners']) == 5 and len(overlap['brokers']) == 2
            assert overlap['broker_leaders'] == state['broker_leaders']
            assert overlap['contracts'] == state['contracts']


def test_api_probe_preserves_shared_broker_membership_inventory(owners):
    from scripts.release.api_probe import snapshot as api_snapshot
    url, schema, _ = owners
    with psycopg.connect(url) as reader:
        baseline = api_snapshot(reader, schema)
        assert len(baseline['owners']) == 4 and len(baseline['brokers']) == 1
    with psycopg.connect(url, autocommit=True) as replacement:
        replacement.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        replacement.execute('SELECT pg_advisory_lock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        with psycopg.connect(url) as reader:
            overlap = api_snapshot(reader, schema)
            assert len(overlap['owners']) == 5 and len(overlap['brokers']) == 2
            assert set(baseline['brokers']) < set(overlap['brokers'])


@pytest.mark.parametrize('change', ['dirty', 'stale_contract', 'missing_contract', 'exclusive_broker'])
def test_snapshot_rejects_unactivated_or_ambiguous_baseline(owners, change):
    url, schema, connections = owners
    control, _, broker, _ = connections
    if change == 'dirty': control.execute('UPDATE schema_state SET dirty=1')
    elif change == 'stale_contract': control.execute("UPDATE runtime_worker_policy SET cluster_fingerprint='stale'")
    elif change == 'missing_contract': control.execute('DELETE FROM runtime_coordinator_policy')
    else:
        broker.execute('SELECT pg_advisory_unlock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        broker.execute('SELECT pg_advisory_lock(726944,%s::regnamespace::oid::int)', (schema,))
    with psycopg.connect(url) as reader, pytest.raises(ValueError):
        snapshot(reader, schema)


def response(role, *, status=200):
    body = io.BytesIO(json.dumps({'status': 'ok'}).encode())
    body.status, body.headers = status, Message()
    for key, value in {'X-Moyai-Build': 'a' * 40, 'X-Moyai-Role': role,
            'X-Moyai-Rolling-Release': '1', 'X-Moyai-Owner': '42',
            'X-Moyai-Coordinator-State': 'leader', 'X-Moyai-Broker-Draining': 'false'}.items():
        body.headers[key] = value
    return body


@pytest.fixture
def observed_probe(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from scripts.release import rolling_probe
    calls = []
    contracts = dict(zip(('api', 'worker', 'broker', 'coordinator'), (c * 64 for c in 'abcd')))
    state = {'owners': ['42@start'], 'coordinators': [], 'brokers': [], 'broker_leaders': [],
             'policy': 'f' * 64, 'schema': 18, 'contracts': dict(contracts)}
    settings = SimpleNamespace(moyai_schema_mode='verify', moyai_separate_broker=True,
        moyai_coordinator_handoff=True, temporal_enabled=True, moyai_database_url='private-dsn',
        moyai_database_schema='synthetic', public_url='https://synthetic.example')
    options = {'sha': 'a' * 40, 'role': 'worker', 'staged': False, 'drain': False,
               'env_keys': [], 'env_digest': hashlib.sha256(b'{}').hexdigest()}
    for key, value in {'RENDER_GIT_COMMIT': options['sha'], 'MOYAI_BUILD_SHA': options['sha'],
            'MOYAI_RUNTIME_ROLE': 'worker', 'RENDER_MIGRATION_STAGE': 'false',
            'MAINTENANCE_DRAIN': 'false', 'MOYAI_COORDINATOR_HANDOFF': 'true'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('render_start.configure_environment', lambda: None)
    monkeypatch.setattr('app.config.Settings', lambda **kwargs: settings)
    monkeypatch.setattr('app.runtime_compatibility.api_fingerprint', lambda _: contracts['api'])
    monkeypatch.setattr('app.runtime_compatibility.background_fingerprint', lambda _, role: contracts[role])
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: Connection())
    def snapshot(*args):
        calls.append('snapshot')
        return dict(state)
    monkeypatch.setattr(rolling_probe, 'snapshot', snapshot)
    monkeypatch.setattr(rolling_probe, 'schema_capability', lambda *args: {
        'schema_support': {'protocol': 1, 'required': 18, 'compatibility': 18}})
    def health(*args, **kwargs):
        calls.append('health')
        return response('worker')
    monkeypatch.setattr(rolling_probe.urllib.request, 'urlopen', health)
    return rolling_probe, options, state, calls


@pytest.mark.parametrize('error,reason', [
    (psycopg.errors.ConnectionFailure, 'database_unavailable'),
    (psycopg.errors.AdminShutdown, 'database_unavailable'),
    (psycopg.errors.InvalidPassword, 'database_error'),
    (psycopg.OperationalError, 'database_error'),
    (psycopg.errors.QueryCanceled, 'database_error'),
])
def test_only_classified_connection_establishment_failures_can_retry(observed_probe, monkeypatch, capsys, error, reason):
    module, options, _, calls = observed_probe
    def connect(*args, **kwargs): raise error('private-diagnostic')
    monkeypatch.setattr(psycopg, 'connect', connect)
    assert module.result(options) == {'ok': False, 'reason': reason}
    assert calls == [] and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('stage', ['snapshot', 'schema_capability', 'health'])
def test_errors_after_observation_begins_are_terminal_even_when_transport_is_transient(
        observed_probe, monkeypatch, stage):
    from scripts.release.deploy import TRANSIENT_PROBE_FAILURES
    module, options, state, calls = observed_probe
    state['policy'] = '0' * 64  # A later healthy sample must not hide this observation.
    def failed(*args, **kwargs):
        if stage == 'health': raise TimeoutError('private-diagnostic')
        raise psycopg.errors.QueryCanceled('private-diagnostic')
    if stage == 'health': monkeypatch.setattr(module.urllib.request, 'urlopen', failed)
    else: monkeypatch.setattr(module, stage, failed)
    value = module.result(options)
    assert value == {'ok': False, 'reason': 'health_unavailable' if stage == 'health' else 'database_error'}
    assert value['reason'] not in TRANSIENT_PROBE_FAILURES
    assert calls == ([] if stage == 'snapshot' else ['snapshot'])


@pytest.mark.parametrize('change', ['environment', 'configuration', 'snapshot', 'contracts', 'health_identity', 'health_unready', 'none'])
def test_validated_runtime_checks_do_not_become_retryable_failures(observed_probe, monkeypatch, change):
    module, options, state, _ = observed_probe
    if change == 'environment': monkeypatch.setenv('MOYAI_BUILD_SHA', 'b' * 40)
    elif change in {'configuration', 'snapshot'}:
        def failed(*args, **kwargs): raise ValueError('private-diagnostic')
        if change == 'configuration': monkeypatch.setattr('app.config.Settings', failed)
        else: monkeypatch.setattr(module, 'snapshot', failed)
    elif change == 'contracts': state['contracts']['worker'] = '0' * 64
    elif change == 'health_identity':
        body = response('worker')
        body.headers.replace_header('X-Moyai-Build', 'b' * 40)
        monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: body)
    elif change == 'health_unready':
        monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: response('worker', status=503))
    value = module.result(options)
    if change in {'none', 'health_unready'}:
        assert value['ok'] is True and value['ready'] is (change == 'none')
    else:
        assert value == {'ok': False, 'reason': 'runtime_validation_failed', 'stage': change}


@pytest.mark.parametrize('role', ['api', 'worker', 'broker', 'coordinator'])
@pytest.mark.parametrize('status', [200, 503])
def test_health_is_bound_to_exact_build_role_and_database_process(role, status):
    result = health_identity(response(role, status=status), {'sha': 'a' * 40, 'role': role}, {'owners': ['42@start']})
    assert result['identity'] == '42@start' and result['ready'] == (status == 200)


@pytest.mark.parametrize('header,value', [('X-Moyai-Build', 'b' * 40), ('X-Moyai-Role', 'api'),
    ('X-Moyai-Rolling-Release', '0'), ('X-Moyai-Owner', '99'), ('X-Moyai-Broker-Draining', 'true')])
def test_wrong_identity_or_draining_broker_never_looks_ready(header, value):
    body = response('broker')
    body.headers.replace_header(header, value)
    with pytest.raises(ValueError):
        health_identity(body, {'sha': 'a' * 40, 'role': 'broker'}, {'owners': ['42@start']})


def test_ssh_probe_helpers_do_not_depend_on_scripts_packaged_in_docker():
    # Docker only includes app/ and startup files. Execute the piped source with
    # scripts imports forbidden, exercising the helper instead of trusting AST.
    import builtins
    original = builtins.__import__
    def import_without_scripts(name, *args, **kwargs):
        if name == 'scripts' or name.startswith('scripts.'):
            raise ModuleNotFoundError('scripts is absent from the production image')
        return original(name, *args, **kwargs)
    with patch('builtins.__import__', import_without_scripts):
        for name in ('api_probe.py', 'rolling_probe.py'):
            namespace = {'__name__': 'ssh_piped_probe'}
            exec(compile(Path('scripts/release', name).read_text(), name, 'exec'), namespace)
            namespace['require'](True)
            with pytest.raises(ValueError): namespace['require'](False)
            if name == 'rolling_probe.py':
                assert namespace['health_identity'](response('worker'),
                    {'sha': 'a' * 40, 'role': 'worker'}, {'owners': ['42@start']})['ready']


@pytest.mark.parametrize('outcome', ['success', 'missing', 'pending', 'failed', 'wrong_sha', 'wrong_workflow'])
def test_required_rolling_suites_must_pass_on_exact_dispatched_main(outcome):
    class API:
        def call(self, method, path):
            workflow = path.split('/workflows/')[1].split('/runs?')[0]
            run = {'id': 1, 'head_sha': 'a' * 40, 'head_branch': 'main', 'event': 'push',
                'path': '.github/workflows/' + workflow, 'status': 'completed', 'conclusion': 'success'}
            if outcome == 'missing': return {'workflow_runs': []}
            if outcome == 'pending': run['status'] = 'in_progress'
            if outcome == 'failed': run['conclusion'] = 'failure'
            if outcome == 'wrong_sha': run['head_sha'] = 'b' * 40
            if outcome == 'wrong_workflow': run['path'] = '.github/workflows/unrelated.yml'
            return {'workflow_runs': [run]}
    github = GitHub(API(), '1', 'a' * 40)
    github.ready = lambda sha: True
    if outcome == 'failed':
        with pytest.raises(ReleaseError, match='CI failed'): github.rolling_ready('a' * 40)
    else:
        assert github.rolling_ready('a' * 40) == (outcome == 'success')


@pytest.mark.parametrize('workflow', ['postgres-runtime.yml', 'production-release-tests.yml'])
def test_required_rolling_workflows_run_for_every_main_push(workflow):
    # Any selected main commit can be deployed, including UI-only or docs-only
    # changes. Requiring an exact-SHA check while filtering it out deadlocks CI.
    text = Path('.github/workflows', workflow).read_text()
    pull_request, push = text.split('  push:\n', 1)
    assert '  pull_request:' in pull_request
    push = push.split('\npermissions:', 1)[0]
    assert '    branches: [main]' in push
    assert 'paths' not in push


@pytest.mark.parametrize('handoff', [False, True])
def test_legacy_probe_includes_handoff_only_when_running_settings_support_it(handoff):
    import hashlib
    from types import SimpleNamespace
    from scripts.release.probe import POLICY_FIELDS, policy_fingerprint
    values = {name: 'synthetic-' + name for name in POLICY_FIELDS}
    legacy = SimpleNamespace(**values)
    assert policy_fingerprint(legacy) == hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    current = {**values, 'moyai_separate_broker': True, 'moyai_coordinator_handoff': handoff,
               'sandbox_prepared_pool_size': 0, 'sandbox_prepared_idle_seconds': 300}
    assert policy_fingerprint(SimpleNamespace(**current)) == hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
