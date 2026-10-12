"""Offline migration transport gates; no deployment credentials or real SSH."""
from copy import deepcopy
import builtins
from pathlib import Path
import json
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app import config, schema_migrations
from scripts.release import maintenance_migration as migration
from scripts.release.deploy import COORDINATOR, ReleaseError, Service


@pytest.fixture
def execution(monkeypatch):
    digest = json.loads(Path(schema_migrations.__file__).with_name('schema_versions.json').read_text())[
        str(schema_migrations.SCHEMA_REVISION)]
    expected = {'schema': schema_migrations.SCHEMA_REVISION - 1,
                'source_schema': schema_migrations.SCHEMA_REVISION, 'source_schema_digest': digest,
                'sha': 'a' * 40, 'policy': 'b' * 64, 'contracts': {'api': 'c' * 64},
                **{key: [] for key in migration.INVENTORIES}}
    env = {'MOYAI_RUNTIME_ROLE': 'coordinator', 'MOYAI_BUILD_SHA': expected['sha'],
           'RENDER_MIGRATION_STAGE': 'true', 'MAINTENANCE_DRAIN': 'false',
           'SESSION_SECRET': 'synthetic-secret-must-not-leave-render'}
    service = Service(COORDINATOR, 'coordinator', env, deepcopy(env), expected['sha'], 'dep-staged')
    state = {**deepcopy(expected), 'ok': True, 'ready': True, 'staged': True, 'identity': None,
             **{key: 0 for key in migration.COUNTS}}
    settings = SimpleNamespace(moyai_runtime_role='coordinator', moyai_schema_mode='verify',
        moyai_coordinator_handoff=True, moyai_separate_broker=True,
        moyai_database_initialize=False, maintenance_drain=False)
    calls = []
    def migrate(value, *, before_migrate):
        assert value is settings
        assert callable(before_migrate)
        calls.append('migrate')
        state['schema'] = expected['source_schema']
        return {'status': 'ready', 'schema_revision': expected['source_schema']}
    monkeypatch.setattr(config, 'Settings', lambda **kwargs: settings)
    monkeypatch.setattr(schema_migrations, 'migrate', migrate)
    options = {'service': service.probe_options(), 'expected': expected}
    return service, options, state, settings, calls


def receipt(expected):
    return {'status': 'applied', 'from_revision': expected['schema'],
            'to_revision': expected['source_schema'], 'source_digest': expected['source_schema_digest']}


def test_staged_source_migrates_once_and_rechecks_schema_and_policy(execution):
    _, options, state, _, calls = execution
    probes = []
    def probe(service):
        probes.append(state['schema'])
        return state
    assert migration.execute(options, probe_fn=probe) == receipt(options['expected'])
    assert calls == ['migrate']
    assert probes == [options['expected']['schema'], options['expected']['source_schema']]


@pytest.mark.parametrize('change', ['unready', 'active', 'identity', 'schema', 'policy', 'contracts',
    *migration.INVENTORIES, *migration.COUNTS, 'source_schema', 'source_digest',
    'downgrade', 'role', 'initialize', 'drain', 'mode'])
def test_changed_precondition_never_calls_installed_migrator(execution, change):
    _, options, state, settings, calls = execution
    if change in migration.INVENTORIES:
        state[change] = ['42@unexpected']
    elif change in migration.COUNTS:
        state[change] = 1
    elif change in {'policy', 'contracts', 'schema', 'source_schema'}:
        state[change] = 'changed'
    elif change == 'unready':
        state['ready'] = False
    elif change == 'active':
        state['staged'] = False
    elif change == 'identity':
        state['identity'] = '42@unexpected'
    elif change == 'source_digest':
        options['expected']['source_schema_digest'] = 'e' * 64
    elif change == 'downgrade':
        options['expected']['schema'] = options['expected']['source_schema'] + 1
    else:
        key, value = {'role': ('moyai_runtime_role', 'api'), 'initialize': ('moyai_database_initialize', True),
                      'drain': ('maintenance_drain', True), 'mode': ('moyai_schema_mode', 'auto')}[change]
        setattr(settings, key, value)
    with pytest.raises(ValueError, match='precondition'):
        migration.execute(options, probe_fn=lambda _: state)
    assert calls == []


def test_changed_postmigration_policy_is_not_reported_as_success(execution):
    _, options, state, _, calls = execution
    def probe(_):
        if calls:
            state['policy'] = 'unexpected'
        return state
    with pytest.raises(ValueError, match='precondition'):
        migration.execute(options, probe_fn=probe)
    assert calls == ['migrate']


@pytest.mark.parametrize('outcome', ['success', 'timeout', 'nonzero', 'duplicate', 'unconfirmed', 'wrong_revision'])
def test_transport_pins_source_preserves_secret_and_never_retries(execution, monkeypatch, outcome):
    service, options, _, _, _ = execution
    expected = options['expected']
    result = receipt(expected)
    if outcome == 'unconfirmed': result = {'status': 'unconfirmed'}
    if outcome == 'wrong_revision': result['to_revision'] += 1
    stdout = 'MOYAI_MAINTENANCE_MIGRATION=' + json.dumps(result)
    if outcome == 'duplicate': stdout += '\n' + stdout
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('ssh', 600, output='private-diagnostic')
        return SimpleNamespace(returncode=1 if outcome == 'nonzero' else 0, stdout=stdout, stderr='private-diagnostic')
    monkeypatch.setattr(subprocess, 'run', run)
    transport = migration.SSHMaintenanceMigration(Path('/synthetic-key'))
    if outcome == 'success':
        assert transport(service, expected) == result
    else:
        with pytest.raises(ReleaseError, match='uncertain'):
            transport(service, expected)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert 'ControlMaster=no' in args and 'ControlPath=none' in args
    assert 'StrictHostKeyChecking=yes' in args
    assert service.env['SESSION_SECRET'] not in ' '.join(args) + kwargs['input']
    assert kwargs['timeout'] == 600


def test_remote_entrypoint_suppresses_credentials_in_invalid_input(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['-', 'private-malformed-credential'])
    runpy.run_path(migration.__file__, run_name='__main__')
    assert capsys.readouterr() == ('MOYAI_MAINTENANCE_MIGRATION={"status": "unconfirmed"}\n', '')


def test_remote_bundle_runs_without_scripts_installed(execution, monkeypatch, capsys):
    service, options, state, _, calls = execution
    transport = migration.SSHMaintenanceMigration(Path('/synthetic-key'))
    captured = []
    def run(*args, source, **kwargs):
        captured.append(source)
        return receipt(options['expected'])
    monkeypatch.setattr(transport.transport, 'run', run)
    transport(service, options['expected'])
    original_import, original_exec = builtins.__import__, builtins.exec
    def installed_import(name, *args, **kwargs):
        if name == 'scripts' or name.startswith('scripts.'):
            raise ModuleNotFoundError('Deployment image has no scripts package')
        return original_import(name, *args, **kwargs)
    def exec_bundle(source, namespace, *args, **kwargs):
        original_exec(source, namespace, *args, **kwargs)
        if namespace.get('__name__') == 'moyai_maintenance_migration_probe':
            namespace['probe'] = lambda _: state
    monkeypatch.setattr(builtins, '__import__', installed_import)
    monkeypatch.setattr(builtins, 'exec', exec_bundle)
    monkeypatch.setattr(sys, 'argv', ['-', json.dumps(options)])
    original_exec(captured[0], {'__name__': '__main__'})
    assert calls == ['migrate']
    result = capsys.readouterr()
    assert result.err == ''
    assert json.loads(result.out.removeprefix('MOYAI_MAINTENANCE_MIGRATION=')) == receipt(options['expected'])
