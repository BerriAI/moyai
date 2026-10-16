"""Supplementary release-controller fixtures, not actual-app E2E evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, MUTABLE, ReleaseError
from scripts.release.maintenance_deploy import MaintenanceRelease
from scripts.release.native_probe import COUNTS, CORE_COUNTS, migration_plan
from scripts.release.native_control import fingerprint, owner
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID
from scripts.release.rehearse_maintenance import MaintenanceRender, CANDIDATE

MANIFEST = {'schema': 'native_fixture', 'app': {'v1': '1' * 64, 'v2': '2' * 64},
            'harness': {'sdk_version': '2.55.0', 'harness_version': '0.55.0'}}
EMPTY = {'schema': 'native_fixture', 'state': 'empty', 'app': {}, 'harness': None}


@pytest.fixture
def cutover(tmp_path):
    native = {**deepcopy(CANDIDATE), 'core': 'pydantic', 'default_harness': 'codex'}
    render = MaintenanceRender(candidate_contract=native)
    for env in (*render.env.values(), *render.installed.values()):
        env.update(AGENT_HARNESS='codex', MOYAI_PRODUCT_MODE='existing',
            MOYAI_NATIVE_SCHEMA='native_fixture', MOYAI_NATIVE_TASK_QUEUE='native-fixture-v1',
            MOYAI_NATIVE_ENCRYPTION_KEY='private-native-key-must-never-be-recorded',
            MOYAI_NATIVE_KEY_FORMAT='sha256-text-fernet-v1',
            MOYAI_NATIVE_OBJECT_PREFIX='native-fixture', MOYAI_NATIVE_OBJECT_FORMAT='fernet-hmac-sha256-v1')
    observed = deepcopy(EMPTY)
    render.counts.update(dict.fromkeys((*COUNTS, *CORE_COUNTS, 'legacy_core_runs',
                                      'legacy_automations', 'legacy_events', 'legacy_schedule_count'), 0))
    render.owners = {sid: [f'{index}@2026-10-15T00:00:00+00:00']
                     for index, sid in enumerate(render.env, 1)}
    control = {'receipt': None, 'calls': []}
    github, elapsed = Candidate(), [0]
    github.rolling_ready, github.forward_from = github.ready, lambda *args: None
    github.native_ready = lambda sha: True
    original_probe, original_call = render.probe, render.call
    def probe(service):
        keys = service.probe_options()
        actual = {key: render.installed[service.id].get(key) for key in keys['env_keys']}
        assert hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest() == keys['env_digest']
        return {**original_probe(service), 'core': render.installed[service.id].get('MOYAI_CORE', 'legacy'),
                'native': deepcopy(observed), 'native_drain': deepcopy(control['receipt'])}
    def call(method, path, body=None):
        if method != 'PUT' or not path.endswith('/MOYAI_CORE'):
            result = original_call(method, path, body)
            for sid, identities in render.owners.items():
                render.owners[sid] = [identity.replace('@new', '@2026-10-15T01:00:00+00:00')
                                     for identity in identities]
            return result
        sid = path.split('/')[2]
        assert 'MOYAI_CORE' not in MUTABLE
        assert render.env[sid]['RENDER_MIGRATION_STAGE'] == 'true'
        assert render.env[sid]['MOYAI_BUILD_SHA'] == NEW
        assert release.drained and release.selected is None
        render.before_write(method, sid, body)
        render.calls.append((method, sid, 'MOYAI_CORE', body['value']))
        render.env[sid]['MOYAI_CORE'] = body['value']
        render.after_write(method, sid, body)
        if render.unknown_put:
            raise ReleaseError('Synthetic lost core write response')
    render.call = call
    migrations, environments = [], []
    def manifest(env):
        environments.append(deepcopy(env))
        assert env['MOYAI_CORE'] == 'pydantic' and env['AGENT_HARNESS'] == 'codex'
        return deepcopy(MANIFEST)
    def migrate(service, plan, expected):
        assert service.id == COORDINATOR and not sum(render.owners.values(), [])
        assert len(render.history) == 5
        assert expected['native_required'] and expected['core_switch']
        assert release.record['native_migration']['status'] == 'requested'
        assert not release.record['native_migration']['confirmed']
        migrations.append(deepcopy(plan))
        observed.clear()
        observed.update(deepcopy(MANIFEST), state='receipted')
        control['receipt'] = {'protocol': 1, 'core': 'pydantic-v1', 'native_schema': MANIFEST['schema'],
            'task_queue': 'native-fixture-v1', 'generation': 0, 'paused': True, 'request_key': 'bootstrap',
            'request_fingerprint': hashlib.sha256(b'bootstrap').hexdigest(),
            'requester': {'pid': None, 'backend_start': None}, 'source_policy': None}
        return {'status': 'verified', 'target': deepcopy(MANIFEST)}
    def control_command(service, command, body):
        control['calls'].append((command, deepcopy(body)))
        if command != 'status':
            assert release.record['native_control'][-1]['confirmed'] is False
            if command == 'request':
                assert not render.history  # Source worker has not been restarted yet.
            else:
                assert len(render.history) == 10
                assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'false'
            control['receipt'] = {
                **{key: body[key] for key in ('protocol', 'core', 'native_schema', 'task_queue')},
                'generation': body['expected_generation'] + 1, 'paused': command == 'request',
                'request_key': body['request_key'], 'request_fingerprint': fingerprint(body, paused=command == 'request'),
                'requester': {key: body['expected_worker'][key] for key in ('pid', 'backend_start')},
                'source_policy': body['expected_worker']['source_policy'],
            }
            return deepcopy(control['receipt'])
        counts = dict(zip(('queued', 'parked', 'unsafe_turns', 'effects', 'requests'),
            (render.counts[key] for key in ('native_queued', 'native_parked', 'native_runs',
                                          'native_effects', 'native_requests'))))
        workers = []
        if render.owners[WORKER]:
            source = (render.candidate_contract if render.installed[WORKER]['MOYAI_BUILD_SHA'] == NEW
                      else render.old_contract)
            workers = [{**owner(render.owners[WORKER][-1]), 'source_policy': render.policy,
                        'worker_policy': source['contracts']['worker'],
                        'maintenance_drain': render.installed[WORKER]['MAINTENANCE_DRAIN'] == 'true'}]
        state = ('running' if not control['receipt']['paused'] else
                 'draining' if any(counts[key] for key in ('unsafe_turns', 'effects', 'requests')) else 'drained')
        return {**deepcopy(control['receipt']), 'counts': counts, 'workers': workers, 'state': state}
    release = MaintenanceRelease(render, github, probe, tmp_path / 'receipt.json',
        api_service_id=API_ID, contract=lambda env: deepcopy(native),
        native_manifest=manifest, native_migrate=migrate, native_control=control_command, target_core='pydantic',
        wait_seconds=1, readiness_timeout=2, retirement_timeout=2, deploy_timeout=2, drain_timeout=2,
        clock=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        log=lambda *args, **kwargs: None)
    release.test_control = control
    return release, render, observed, migrations, environments


def test_core_switch_preserves_source_drain_keys_and_harness_then_provisions_at_zero_owners(cutover):
    release, render, _, migrations, environments = cutover
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success'
    assert len(environments) == 4 and len(migrations) == 1 and migrations[0]['apply']
    assert 'MOYAI_CORE' not in render.history[0]['env']  # Old worker drains using the old core.
    assert all(h['env']['AGENT_HARNESS'] == 'codex' for h in render.history)
    assert len(render.history) == 10
    for sid, env in original.items():
        assert render.env[sid] == {**env, 'MOYAI_BUILD_SHA': NEW, 'MOYAI_CORE': 'pydantic'}
    assert release.record['native_migration']['verified']
    assert 'private-native-key' not in release.receipt.read_text()
    assert all(entry['previous'] is None for entry in release.record['core_migration'])


def test_native_preflight_records_receipts_without_provision_or_render_writes(cutover):
    release, render, _, migrations, _ = cutover
    release.preflight_only = True
    release.run()
    assert not render.calls and not migrations
    assert release.record['candidate_core'] == 'pydantic'
    assert release.record['native_migration']['plan']['target'] == MANIFEST


@pytest.mark.parametrize('key', (*COUNTS, *(key for key in CORE_COUNTS if key != 'native_automation_history'),
                                'legacy_core_runs', 'legacy_events'))
def test_unfinished_native_or_legacy_turns_block_core_switch_without_cancelling(cutover, key):
    release, render, _, migrations, _ = cutover
    render.counts[key] = 1
    with pytest.raises(ReleaseError, match='Drain timed out'):
        release.run()
    assert len(render.history) == 1 and not migrations
    assert 'MOYAI_CORE' not in render.history[0]['env']


@pytest.mark.parametrize('source_core', ['legacy', 'pydantic'])
def test_unqualified_native_automation_history_blocks_before_any_pause_or_deployment(cutover, source_core):
    release, render, _, migrations, _ = cutover
    if source_core == 'pydantic':
        configure_native_source(cutover, target='legacy')
        render.candidate_contract = {**deepcopy(CANDIDATE), 'core': 'legacy', 'default_harness': 'codex'}
        release.contract = lambda env: deepcopy(render.candidate_contract)
    render.counts['native_automation_history'] = 1
    with pytest.raises(ReleaseError, match='qualified Temporal control-workflow drain receipt'):
        release.run()
    assert not render.calls and not migrations and not release.test_control['calls']


def test_unpaused_source_automation_stops_preflight_before_worker_drain(cutover):
    release, render, _, migrations, _ = cutover
    render.counts['legacy_automations'] = 1
    with pytest.raises(ReleaseError, match='Pause source automations'):
        release.run()
    assert not render.calls and not migrations and not release.test_control['calls']


@pytest.mark.parametrize('fault', [None, 'unconfirmed', 'changed', 'missing_transport'])
def test_source_schedule_pause_is_reconfirmed_after_all_source_owners_retire(cutover, fault):
    release, render, _, migrations, _ = cutover
    render.counts['legacy_schedule_count'] = 1
    checks = []
    def inspect(service):
        checks.append(len(render.history))
        if len(checks) == 2:
            assert not sum(render.owners.values(), [])
        if fault == 'unconfirmed':
            return {'status': 'unconfirmed'}
        return {'status': 'remote_schedules_paused', 'count': 1,
                'digest': ('b' if fault == 'changed' and len(checks) == 2 else 'a') * 64}
    if fault != 'missing_transport':
        release.automation_probe = inspect
    if fault:
        with pytest.raises(ReleaseError, match='source schedules|Source automation pause'):
            release.run()
        assert len(render.history) == (5 if fault == 'changed' else 0) and not migrations
    else:
        release.run()
        assert checks == [0, 5]
        assert release.record['legacy_control_disposition'] == 'preserved_on_original_queue_with_paused_admission'


@pytest.mark.parametrize('sid', (API_ID, WORKER, BROKER, COORDINATOR))
def test_ambiguous_core_selector_write_is_not_retried(cutover, sid):
    release, render, _, migrations, _ = cutover
    def after(method, target, body):
        if method == 'PUT' and target == sid and body['value'] == 'pydantic':
            render.unknown_put = True
    render.after_write = after
    with pytest.raises(ReleaseError, match='lost core write'):
        release.run()
    assert len([c for c in render.calls if c[:3] == ('PUT', sid, 'MOYAI_CORE')]) == 1
    assert len(render.history) == 1 + (API_ID, WORKER, BROKER, COORDINATOR).index(sid)
    assert release.record['changes'][-1]['confirmed'] is False and not migrations


@pytest.mark.parametrize('fault', ('lost_ack', 'wrong_receipt', 'owner', 'effect', 'env', 'app'))
def test_native_migration_uncertainty_or_drift_never_activates(cutover, fault):
    release, render, observed, migrations, _ = cutover
    original = release.native_migrate
    def migrate(*args):
        result = original(*args)
        if fault == 'lost_ack':
            raise ReleaseError('Synthetic unknown native outcome')
        if fault == 'wrong_receipt':
            result['status'] = 'applied'
        elif fault == 'owner':
            render.owners[WORKER] = ['999@late']
        elif fault == 'effect':
            render.counts['native_effects'] = 1
        elif fault == 'env':
            render.env[BROKER]['ENCRYPTION_KEY'] = 'drift'
        elif fault == 'app':
            observed['app']['v2'] = '9' * 64
        return result
    release.native_migrate = migrate
    with pytest.raises(ReleaseError):
        release.run()
    assert len(render.history) == 5 and len(migrations) == 1
    assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.env.values())
    assert release.record['native_migration']['verified'] is False


@pytest.mark.parametrize('fault', ('unknown_app', 'wrong_manifest', 'partial', 'wrong_harness', 'missing_transport',
                                 'missing_native_schema', 'mismatched_key', 'unqualified'))
def test_native_preflight_fails_closed_before_source_is_drained(cutover, fault):
    release, render, observed, migrations, _ = cutover
    if fault == 'unknown_app':
        observed.update(state='receipted', app={'v999': 'a' * 64}, harness=MANIFEST['harness'])
    elif fault == 'wrong_manifest':
        observed.update(state='receipted', app={'v1': 'a' * 64}, harness=MANIFEST['harness'])
    elif fault == 'partial':
        observed.update(state='receipted', app={'v1': '1' * 64}, harness=None)
    elif fault == 'wrong_harness':
        observed.update(state='receipted', app=MANIFEST['app'], harness={'sdk_version': 'old'})
    elif fault == 'missing_transport':
        release.native_migrate = None
    elif fault == 'missing_native_schema':
        for env in render.env.values():
            env.pop('MOYAI_NATIVE_SCHEMA')
        for env in render.installed.values():
            env.pop('MOYAI_NATIVE_SCHEMA')
    elif fault == 'mismatched_key':
        render.env[BROKER]['MOYAI_NATIVE_ENCRYPTION_KEY'] = 'different'
    else:
        def blocked(env):
            raise ReleaseError('Native role is not qualified')
        release.native_manifest = blocked
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls and not migrations


def test_version_prefix_upgrades_are_explicit_and_read_only():
    observed = {'schema': MANIFEST['schema'], 'state': 'receipted',
                'app': {'v1': '1' * 64}, 'harness': MANIFEST['harness']}
    plan = migration_plan(observed, MANIFEST)
    assert plan['apply'] and plan['before'] == observed and plan['target'] == MANIFEST
    with pytest.raises(ValueError):
        migration_plan({**observed, 'app': {'v2': '2' * 64}}, MANIFEST)


def test_native_transport_uses_legacy_probe_and_never_retries(monkeypatch):
    from scripts.release.native_migration import SSHNativeMigration
    captured = []
    class Transport:
        def __init__(self, key, source): self.source = source
        def run(self, service, options, **kwargs):
            captured.append((options, kwargs))
            return {'status': 'unconfirmed'}
    monkeypatch.setattr('scripts.release.deploy.SSHProbe', Transport)
    service = SimpleNamespace(probe_options=lambda: {'fixture': True})
    with pytest.raises(ReleaseError, match='uncertain'):
        SSHNativeMigration(Path('/synthetic/key'))(service, {'target': MANIFEST}, {})
    assert len(captured) == 1
    assert not captured[0][1].get('read_only', False)
    assert '/app/native/.venv/bin/python' in captured[0][1]['source']


def test_native_command_checks_both_schema_receipts_without_exposing_output(monkeypatch):
    from scripts.release import native_migration
    monkeypatch.setenv('MOYAI_NATIVE_SCHEMA', MANIFEST['schema'])
    response = {'core': 'pydantic-v1', 'native_schema': MANIFEST['schema'],
                'app_schema': 'verified', 'harness_schema': 'verified',
                'deployment_readiness': 'not_qualified'}
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(response))
    monkeypatch.setattr(native_migration.subprocess, 'run', run)
    assert native_migration.native_command(['verify']) == response
    assert calls[0][0] == ['/app/native/.venv/bin/python', '-m', 'moyai.product_start', 'verify']
    response['harness_schema'] = 'missing'
    with pytest.raises(ValueError, match='both receipts'):
        native_migration.native_command(['verify'])


def test_native_schema_name_collision_is_reported_without_starting_cutover(monkeypatch):
    from scripts.release import native_migration
    monkeypatch.setattr(native_migration.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='{"error":"native_schema_identifier_collision"}', stderr='private-details'))
    with pytest.raises(ReleaseError, match='verified-empty native schema') as error:
        native_migration.candidate_manifest({})
    assert 'private-details' not in str(error.value)


def test_first_native_adoption_requires_its_ci_on_exact_selected_commit(cutover):
    release, render, _, migrations, _ = cutover
    def failed(sha):
        assert sha == NEW
        raise ReleaseError('Native CI failed')
    release.github.native_ready = failed
    with pytest.raises(ReleaseError, match='Native CI failed'):
        release.run()
    assert not render.calls and not migrations


def test_reviewed_legacy_switch_preserves_native_data_keys_queue_and_original_harness(cutover):
    release, render, observed, migrations, _ = cutover
    render.old_contract = {**deepcopy(render.old_contract), 'core': 'pydantic'}
    render.candidate_contract = {**deepcopy(CANDIDATE), 'core': 'legacy', 'default_harness': 'codex'}
    release.contract = lambda env: deepcopy(render.candidate_contract)
    release.target_core = 'legacy'
    for env in (*render.env.values(), *render.installed.values()):
        env['MOYAI_CORE'] = 'pydantic'
    observed.clear()
    observed.update(deepcopy(MANIFEST), state='receipted')
    seed_native_source(release, render)
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success' and release.record['native_migration'] is None
    assert not migrations
    assert render.history[0]['env']['MOYAI_CORE'] == 'pydantic'
    assert all(item['env']['MOYAI_CORE'] == 'legacy' for item in render.history[1:])
    for sid, env in original.items():
        assert render.env[sid] == {**env, 'MOYAI_BUILD_SHA': NEW, 'MOYAI_CORE': 'legacy'}
    assert observed == {**MANIFEST, 'state': 'receipted'}


def test_preserve_uses_native_source_selection_without_rewriting_selector(cutover):
    release, render, observed, migrations, _ = cutover
    release.target_core = 'preserve'
    render.old_contract = {**deepcopy(render.old_contract), 'core': 'pydantic'}
    for env in (*render.env.values(), *render.installed.values()):
        env['MOYAI_CORE'] = 'pydantic'
    observed.clear()
    observed.update(deepcopy(MANIFEST), state='receipted')
    seed_native_source(release, render)
    render.counts['native_queued'] = 3  # Saved same-core requests survive the deployment.
    render.counts['native_automations'] = 2  # Same-core schedules retain their native queue.
    render.counts['native_events'] = 1
    render.counts['native_automation_history'] = 2
    def verify(service, plan, expected):
        assert not expected.get('core_switch') and expected['native_required']
        assert not plan['apply'] and not sum(render.owners.values(), [])
        migrations.append(plan)
        return {'status': 'verified', 'target': plan['target']}
    release.native_migrate = verify
    release.run()
    assert len(migrations) == 1 and not release.core_changes
    assert render.counts['native_queued'] == 3
    assert render.counts['native_automations'] == 2
    assert render.counts['native_events'] == 1
    assert render.counts['native_automation_history'] == 2
    assert not any(call[0] == 'PUT' and call[2] == 'MOYAI_CORE' for call in render.calls)
    assert release.record['native_in_place_drain_verified']
    assert [entry['command'] for entry in release.record['native_control']] == ['request', 'resume']


def seed_native_source(release, render):
    release.test_control['receipt'] = {
        'protocol': 1, 'core': 'pydantic-v1', 'native_schema': MANIFEST['schema'],
        'task_queue': 'native-fixture-v1', 'generation': 1, 'paused': False, 'request_key': 'prior-resume',
        'request_fingerprint': 'a' * 64, 'requester': owner(render.owners[WORKER][0]),
        'source_policy': render.policy,
    }


def configure_native_source(cutover, *, target='preserve'):
    release, render, observed, _, _ = cutover
    release.target_core = target
    render.old_contract = {**deepcopy(render.old_contract), 'core': 'pydantic'}
    for env in (*render.env.values(), *render.installed.values()):
        env['MOYAI_CORE'] = 'pydantic'
    observed.clear()
    observed.update(deepcopy(MANIFEST), state='receipted')
    seed_native_source(release, render)
    release.native_migrate = lambda service, plan, expected: {'status': 'verified', 'target': plan['target']}
    return release, render


@pytest.mark.parametrize('command', ['request', 'resume'])
def test_uncertain_native_control_write_is_not_retried_or_automatically_reversed(cutover, command):
    release, render = configure_native_source(cutover)
    original = release.native_control
    def uncertain(service, selected, body):
        value = original(service, selected, body)
        if selected == command:
            raise ReleaseError('Synthetic lost native CAS response')
        return value
    release.native_control = uncertain
    with pytest.raises(ReleaseError, match='lost native CAS'):
        release.run()
    assert len(render.history) == (0 if command == 'request' else 10)
    assert sum(entry[0] == command for entry in release.test_control['calls']) == 1
    assert release.record['native_control'][-1]['confirmed'] is False


@pytest.mark.parametrize('counter', ['native_effects', 'native_requests', 'native_runs'])
def test_native_in_place_drain_waits_without_restarting_busy_worker(cutover, counter):
    release, render = configure_native_source(cutover)
    render.counts[counter] = 1
    with pytest.raises(ReleaseError, match='original worker remains alive'):
        release.run()
    assert not render.history
    assert release.test_control['receipt']['paused']
    assert all(env['MAINTENANCE_DRAIN'] == 'false' for env in render.env.values())


def test_same_core_parked_turns_survive_replacement_then_explicit_resume(cutover):
    release, render = configure_native_source(cutover)
    render.counts['native_runs'] = 1
    original = release.native_control
    def checkpoint(service, command, body):
        value = original(service, command, body)
        if command == 'request':
            render.counts['native_runs'], render.counts['native_parked'] = 0, 1
        elif command == 'resume':
            render.counts['native_runs'], render.counts['native_parked'] = 1, 0
        return value
    release.native_control = checkpoint
    release.run()
    assert release.record['status'] == 'success' and len(render.history) == 10
    assert render.counts['native_runs'] == 1 and not release.test_control['receipt']['paused']


@pytest.mark.parametrize('counter', ['native_queued', 'native_automations', 'native_events',
                                     'native_automation_history', 'legacy_automations'])
def test_new_native_intake_can_queue_after_candidate_policy_without_early_execution(cutover, counter):
    release, render, _, _, _ = cutover
    def after(method, sid, body):
        if method == 'POST' and len(render.history) == 9:
            assert release.drained and release.test_control['receipt']['paused']
            render.counts[counter] = 2
    render.after_write = after
    release.run()
    assert release.record['status'] == 'success' and render.counts[counter] == 2
    assert release.record['native_control'][-1]['receipt']['paused'] is False


@pytest.mark.parametrize('change', ['missing', 'wrong_sha', 'wrong_branch', 'wrong_workflow', 'pending', 'failed'])
def test_native_ci_checks_trusted_workflow_identity_and_exact_sha(change):
    from scripts.release.deploy import GitHub
    run = {'id': 9, 'head_sha': NEW, 'head_branch': 'main', 'event': 'push',
           'path': '.github/workflows/pydantic-native.yml', 'status': 'completed', 'conclusion': 'success'}
    if change == 'wrong_sha': run['head_sha'] = OLD
    elif change == 'wrong_branch': run['head_branch'] = 'feature'
    elif change == 'wrong_workflow': run['path'] = '.github/workflows/unrelated.yml'
    elif change == 'pending': run['status'] = 'in_progress'
    elif change == 'failed': run['conclusion'] = 'failure'
    class API:
        def call(self, method, path):
            assert method == 'GET' and 'pydantic-native.yml/runs?' in path and NEW in path
            return {'workflow_runs': [] if change == 'missing' else [run]}
    github = GitHub(API(), '99', NEW)
    if change == 'failed':
        with pytest.raises(ReleaseError, match='Native runtime CI'):
            github.native_ready(NEW)
    else:
        assert github.native_ready(NEW) is False
