"""Migration transport/precondition failures; no real SSH or production access."""
from copy import deepcopy
import builtins
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts.release.deploy import COORDINATOR, ReleaseError, Service
from scripts.release import online_migration as migration


PLAN = {'from_revision': 16, 'to_revision': 17, 'compatibility': 16,
        'steps': [{'from_revision': 16, 'to_revision': 17, 'compatibility': 16,
                   'source_digest': 'a' * 64, 'target_digest': 'b' * 64,
                   'operations': [{'kind': 'add_column', 'table': 'runs',
                                   'name': 'online_probe', 'type': 'TEXT', 'nullable': True}]}]}
PLAN['digest'] = hashlib.sha256(json.dumps(PLAN, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
CONTRACTS = dict(zip(('api', 'worker', 'broker', 'coordinator'), (c * 64 for c in 'abcd')))
START = datetime(2026, 10, 11, tzinfo=timezone.utc)
IDENTITY = '41@' + START.isoformat()
EXPECTED = {'schema': 16, 'policy': 'f' * 64, 'contracts': CONTRACTS,
            'owners': [IDENTITY], 'coordinators': [IDENTITY],
            'brokers': [], 'broker_leaders': [], 'identities': {COORDINATOR: IDENTITY}}


@pytest.fixture
def service():
    env = {'MOYAI_RUNTIME_ROLE': 'coordinator', 'MOYAI_BUILD_SHA': 'b' * 40,
           'RENDER_MIGRATION_STAGE': 'false', 'MAINTENANCE_DRAIN': 'false',
           'SESSION_SECRET': 'synthetic-secret-must-not-appear'}
    return Service(COORDINATOR, 'coordinator', env, deepcopy(env), 'b' * 40, 'dep-original')


@pytest.mark.parametrize('status', ['applied', 'already_applied'])
def test_one_exact_request_uses_pinned_ssh_and_never_sends_database_credentials(monkeypatch, service, status):
    calls = []
    receipt = {key: PLAN[key] for key in ('from_revision', 'to_revision', 'digest')}
    receipt['status'] = status

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='login notice\nMOYAI_ONLINE_MIGRATION=' + json.dumps(receipt), stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    result = migration.SSHMigration(Path('/synthetic-key'))(service, PLAN, EXPECTED)
    assert result == receipt and len(calls) == 1
    args, kwargs = calls[0]
    assert 'StrictHostKeyChecking=yes' in args and 'IdentitiesOnly=yes' in args
    assert args[-2] == COORDINATOR + '@ssh.oregon.render.com'
    assert kwargs['timeout'] == 60 and kwargs['capture_output'] is True
    assert Path(migration.__file__).read_text() in kwargs['input']
    assert 'synthetic-secret-must-not-appear' not in ' '.join(args)
    # Each shell branch receives the same single, quoted JSON argument.
    shell = shlex.shlex(args[-1], posix=True, punctuation_chars=True)
    shell.whitespace_split = True
    embedded = [value for value in shell if value.startswith('{')]
    assert len(embedded) == 2
    options = json.loads(embedded[0])
    assert options == json.loads(embedded[1])
    assert options['service_id'] == COORDINATOR and options['plan'] == PLAN
    assert options['expected'] == EXPECTED and options['service']['sha'] == service.sha


@pytest.mark.parametrize('outcome', ['timeout', 'nonzero', 'missing', 'duplicate', 'malformed',
    'unconfirmed', 'wrong_from', 'wrong_to', 'wrong_digest', 'array', 'null', 'unknown_status',
    'nonstring_status', 'extra_field'])
def test_ambiguous_or_mismatched_response_is_never_retried_or_reported_success(monkeypatch, service, outcome, capsys):
    calls = []
    receipt = {'status': 'applied', **{key: PLAN[key] for key in ('from_revision', 'to_revision', 'digest')}}
    if outcome.startswith('wrong_'):
        receipt[{'wrong_from': 'from_revision', 'wrong_to': 'to_revision', 'wrong_digest': 'digest'}[outcome]] = 'wrong'
    elif outcome == 'unconfirmed':
        receipt = {'status': 'unconfirmed'}
    elif outcome == 'unknown_status':
        receipt['status'] = 'skipped'
    elif outcome == 'nonstring_status':
        receipt['status'] = []
    elif outcome == 'extra_field':
        receipt['private'] = 'private-diagnostic'
    elif outcome == 'array':
        receipt = []
    elif outcome == 'null':
        receipt = None
    stdout = 'MOYAI_ONLINE_MIGRATION=' + json.dumps(receipt)
    if outcome == 'duplicate':
        stdout += '\n' + stdout
    elif outcome == 'missing':
        stdout = 'MOYAI_RELEASE_PROBE=' + json.dumps(receipt)
    elif outcome == 'malformed':
        stdout = 'MOYAI_ONLINE_MIGRATION={private-diagnostic'

    def run(*args, **kwargs):
        calls.append(args)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('ssh', 60, output='private-diagnostic')
        return SimpleNamespace(returncode=1 if outcome == 'nonzero' else 0,
                               stdout=stdout, stderr='private-diagnostic')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ReleaseError, match='uncertain') as caught:
        migration.SSHMigration(Path('/synthetic-key'))(service, PLAN, EXPECTED)
    assert len(calls) == 1 and 'private-diagnostic' not in str(caught.value)
    assert capsys.readouterr() == ('', '')


def test_oversize_plan_does_not_invoke_ssh(monkeypatch, service):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('Oversized payload invoked SSH'))
    with pytest.raises(ReleaseError, match='uncertain'):
        migration.SSHMigration(Path('/synthetic-key'))(service, {**PLAN, 'steps': ['x' * 65536]}, EXPECTED)


@pytest.fixture
def execution(monkeypatch, service):
    import app.config
    import psycopg
    import scripts.release.rolling_probe

    calls = []
    state = {**deepcopy(EXPECTED), 'ok': True, 'ready': True, 'identity': IDENTITY,
             'schema_support': {'protocol': 1, 'required': 16, 'compatibility': 16}}
    settings = SimpleNamespace(moyai_schema_mode='verify', moyai_coordinator_handoff=True,
        moyai_separate_broker=True, moyai_runtime_role='coordinator',
        moyai_database_url='synthetic-private-dsn', moyai_database_schema='moyai_synthetic')
    connection = SimpleNamespace(__enter__=lambda self: self, __exit__=lambda *args: None)

    class Connection:
        def __enter__(self):
            return connection

        def __exit__(self, *args):
            pass

    def connect(*args, **kwargs):
        calls.append(('connect', args, kwargs))
        return Connection()

    def apply(conn, schema, plan, *, before_apply):
        calls.append(('apply', conn, schema, plan))
        before_apply()
        return {'status': 'applied', **{key: plan[key] for key in ('from_revision', 'to_revision', 'digest')}}

    engine = ModuleType('app.online_migrations')
    engine.apply_plan = apply
    monkeypatch.setitem(sys.modules, 'app.online_migrations', engine)
    monkeypatch.setattr(app.config, 'Settings', lambda **kwargs: settings)
    monkeypatch.setattr(scripts.release.rolling_probe, 'probe', lambda options: state)
    monkeypatch.setattr(psycopg, 'connect', connect)
    monkeypatch.setattr(migration, 'check_runtime', lambda conn, schema, expected: calls.append(('check_runtime', expected)))
    options = {'service_id': service.id, 'service': service.probe_options(),
               'expected': deepcopy(EXPECTED), 'plan': deepcopy(PLAN)}
    return options, state, settings, calls


def test_remote_runner_rechecks_runtime_inside_engine_transaction(execution):
    options, state, _, calls = execution
    assert migration.execute(options, probe_fn=lambda _: state)['status'] == 'applied'
    assert [call[0] for call in calls] == ['connect', 'apply', 'check_runtime']
    assert calls[0][2]['autocommit'] is True
    assert calls[-1][1] == options['expected']


def test_remote_bundle_calls_installed_engine_without_scripts_package(execution, monkeypatch, service, capsys):
    options, state, _, calls = execution
    receipt = {'status': 'applied', **{key: PLAN[key] for key in ('from_revision', 'to_revision', 'digest')}}
    runner, captured = migration.SSHMigration(Path('/synthetic-key')), []

    def transport(*args, source, **kwargs):
        captured.append(source)
        return receipt

    monkeypatch.setattr(runner.transport, 'run', transport)
    assert runner(service, PLAN, EXPECTED) == receipt
    real_import, real_exec = builtins.__import__, builtins.exec

    def installed_import(name, *args, **kwargs):
        if name == 'scripts' or name.startswith('scripts.'):
            raise ModuleNotFoundError('Render image does not contain scripts')
        return real_import(name, *args, **kwargs)

    def load_probe(source, namespace, *args, **kwargs):
        real_exec(source, namespace, *args, **kwargs)
        if namespace.get('__name__') == 'moyai_migration_probe':
            assert callable(namespace['probe'])
            # Only network observation is substituted. The actual bundled
            # runner must import and invoke its installed app engine.
            namespace['probe'] = lambda _: state

    def installed_apply(conn, schema, plan, *, before_apply):
        calls.append(('installed_engine', schema, plan, callable(before_apply)))
        return receipt

    monkeypatch.setattr(sys.modules['app.online_migrations'], 'apply_plan', installed_apply)
    monkeypatch.setattr(builtins, '__import__', installed_import)
    monkeypatch.setattr(builtins, 'exec', load_probe)
    monkeypatch.setattr(sys, 'argv', ['-', json.dumps(options)])
    real_exec(captured[0], {'__name__': '__main__'})
    assert [call[0] for call in calls] == ['connect', 'installed_engine']
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out.removeprefix('MOYAI_ONLINE_MIGRATION=')) == receipt


@pytest.mark.parametrize('change', ['unready', 'identity', 'policy', 'contracts', 'schema',
    'owners', 'coordinators', 'brokers', 'broker_leaders', 'support_missing', 'support_protocol',
    'support_compatibility', 'from_revision', 'noop', 'role', 'mode', 'handoff', 'separate_broker'])
def test_changed_remote_precondition_never_connects_for_migration(execution, change):
    options, state, settings, calls = execution
    if change == 'unready':
        state['ready'] = False
    elif change == 'identity':
        state['identity'] = '42@replacement'
    elif change in {'policy', 'contracts', 'schema', 'owners', 'coordinators', 'brokers', 'broker_leaders'}:
        state[change] = 'changed'
    elif change == 'support_missing':
        del state['schema_support']
    elif change.startswith('support_'):
        state['schema_support'][change.removeprefix('support_')] = 99
    elif change == 'from_revision':
        options['plan']['from_revision'] = 15
    elif change == 'noop':
        options['plan']['to_revision'] = 16
    else:
        field, value = {'role': ('moyai_runtime_role', 'api'), 'mode': ('moyai_schema_mode', 'auto'),
            'handoff': ('moyai_coordinator_handoff', False), 'separate_broker': ('moyai_separate_broker', False)}[change]
        setattr(settings, field, value)
    with pytest.raises(ValueError, match='precondition'):
        migration.execute(options, probe_fn=lambda _: state)
    assert calls == []


class RuntimeConnection:
    def __init__(self, *, policy=None, contract=None, schema=None, owners=None):
        self.policy, self.contract = policy or (EXPECTED['policy'],), contract
        self.schema = schema or (16, 0)
        self.owners = owners if owners is not None else [
            (726940, 41, START, 'ShareLock'), (726943, 41, START, 'ExclusiveLock')]
        self.statements = []

    def execute(self, statement, params=()):
        text = str(statement)
        self.statements.append(text)
        if 'runtime_policy' in text:
            row = self.policy
        elif 'cluster_fingerprint' in text:
            role = next(role for role in CONTRACTS if 'runtime_' + role + '_policy' in text)
            row = self.contract or (EXPECTED['policy'], CONTRACTS[role])
        elif 'schema_state' in text:
            row = self.schema
        else:
            row = None
        return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: self.owners)


def test_runtime_contracts_are_locked_until_ddl_transaction_finishes():
    conn = RuntimeConnection()
    migration.check_runtime(conn, 'moyai_synthetic', EXPECTED)
    assert sum('FOR SHARE' in statement for statement in conn.statements) == 5
    assert any('l.pid<>pg_backend_pid()' in statement for statement in conn.statements)


@pytest.mark.parametrize('change', ['policy', 'contract', 'schema', 'dirty', 'pid_reuse',
                                 'missing_owner', 'extra_owner', 'wrong_mode'])
def test_runtime_recheck_rejects_drift_and_pid_reuse(change):
    edits = {'policy': {'policy': ('other',)}, 'contract': {'contract': ('other', 'other')},
        'schema': {'schema': (17, 0)}, 'dirty': {'schema': (16, 1)},
        'pid_reuse': {'owners': [(726940, 41, START.replace(day=10), 'ShareLock')]},
        'missing_owner': {'owners': []},
        'extra_owner': {'owners': [(726940, 41, START, 'ShareLock'), (726943, 41, START, 'ExclusiveLock'),
                                    (726940, 42, START, 'ShareLock')]},
        'wrong_mode': {'owners': [(726940, 41, START, 'ExclusiveLock')]}}
    with pytest.raises(ValueError, match='precondition'):
        migration.check_runtime(RuntimeConnection(**edits[change]), 'moyai_synthetic', EXPECTED)


def test_remote_entrypoint_never_prints_malformed_input_or_exception_details(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['online_migration.py', 'malformed-private-credential'])
    runpy.run_path(migration.__file__, run_name='__main__')
    captured = capsys.readouterr()
    assert captured.out == 'MOYAI_ONLINE_MIGRATION={"status": "unconfirmed"}\n'
    assert captured.err == ''
