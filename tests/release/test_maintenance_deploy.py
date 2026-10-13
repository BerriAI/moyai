"""Four-role controller failure boundaries; synthetic control plane only."""
from copy import deepcopy

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.maintenance_deploy import MaintenanceRelease, COUNTS
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID
from scripts.release.rehearse_maintenance import MaintenanceRender, OLD_CONTRACT, CANDIDATE



@pytest.fixture
def setup(tmp_path):
    render, github, elapsed = MaintenanceRender(), Candidate(), [0]
    github.rolling_ready = github.ready
    github.forward_from = lambda *args: None
    def sleep(seconds):
        elapsed[0] += seconds
    release = MaintenanceRelease(render, github, render.probe, tmp_path / 'receipt.json',
        api_service_id=API_ID, contract=lambda env: deepcopy(CANDIDATE), wait_seconds=1,
        readiness_timeout=2, retirement_timeout=2, deploy_timeout=2, drain_timeout=2,
        clock=lambda: elapsed[0], sleep=sleep, log=lambda *args, **kwargs: None)
    return release, render, github


def test_protocol_changes_only_after_exact_zero_owner_boundary(setup):
    release, render, _ = setup
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success'
    assert release.record['zero_owner_boundary_verified'] is True
    assert [h['service'] for h in render.history] == [WORKER, API_ID, WORKER, BROKER, COORDINATOR,
                                                    COORDINATOR, BROKER, WORKER, API_ID, WORKER]
    assert render.history[0]['env']['MOYAI_BUILD_SHA'] == OLD
    assert render.history[0]['env']['MAINTENANCE_DRAIN'] == 'true'
    assert not sum(render.history[5]['owners'].values(), [])
    assert all(render.history[i]['env']['RENDER_MIGRATION_STAGE'] == 'true' for i in range(1, 5))
    assert render.history[7]['env']['MAINTENANCE_DRAIN'] == 'true'
    assert len(sum(render.history[-1]['owners'].values(), [])) == 4
    assert render.history[-1]['env']['MAINTENANCE_DRAIN'] == 'false'
    for sid, env in original.items():
        assert render.env[sid] == {**env, 'MOYAI_BUILD_SHA': NEW}
    assert len(release.retired) == 6  # old worker drain + original4 + drained candidate worker
    assert all(len(v) == 1 for v in render.owners.values())
    text = release.receipt.read_text()
    assert all(secret not in text for secret in ('do-not-log', 'postgresql://', 'private-bucket'))


def test_preflight_performs_no_writes_and_reports_effective_harness(setup):
    release, render, _ = setup
    release.preflight_only = True
    release.run()
    assert release.record['status'] == 'preflight_passed' and not render.calls
    assert release.record['candidate_default_harness'] == 'deepagents-native'
    assert all(v['default_harness'] == 'codex' for v in release.record['previous'].values())


@pytest.mark.parametrize('fault', ['harness', 'contracts', 'topology', 'setting', 'old_build', 'owner', 'count', 'ci'])
def test_preflight_rejects_invalid_complete_inventory_before_writes(setup, fault):
    release, render, github = setup
    if fault == 'harness':
        release.contract = lambda env: {**deepcopy(CANDIDATE), 'default_harness': 'codex'}
    elif fault == 'contracts':
        release.contract = lambda env: {**deepcopy(CANDIDATE), 'source_policy': ('d' if env['MOYAI_RUNTIME_ROLE'] == 'api' else 'e') * 64}
    elif fault == 'topology':
        render.metadata[API_ID]['serviceDetails']['numInstances'] = 2
    elif fault == 'setting':
        render.env[API_ID]['SESSION_SECRET'] = 'different'
    elif fault == 'old_build':
        render.live[API_ID]['commit']['id'] = 'd' * 40
        render.env[API_ID]['MOYAI_BUILD_SHA'] = 'd' * 40
    elif fault == 'owner':
        render.owners[API_ID].append('unknown@owner')
    elif fault == 'count':
        render.counts['model_requests'] = True
    else:
        github.rolling_ready = lambda sha: (_ for _ in ()).throw(ReleaseError('CI failed'))
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('count', COUNTS)
def test_drain_failure_leaves_only_old_worker_drained(setup, count):
    release, render, _ = setup
    render.counts[count] = 1
    with pytest.raises(ReleaseError, match='Drain timed out'):
        release.run()
    assert [h['service'] for h in render.history] == [WORKER]
    assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
    assert all(env['MOYAI_BUILD_SHA'] == OLD for env in render.env.values())


@pytest.mark.parametrize('index', range(10))
def test_every_ambiguous_deployment_stops_without_retry_or_rollback(setup, index):
    release, render, _ = setup
    def after(method, sid, body):
        if method == 'POST' and len(render.history) == index + 1:
            render.unknown_write = True
    render.after_write = after
    with pytest.raises(ReleaseError, match='lost write response'):
        release.run()
    assert len(render.history) == index + 1
    assert release.record['deploys'][-1]['id'] is None
    assert release.record['deploys'][-1]['status'] == 'requested'
    assert release.record['status'] == 'failed'


@pytest.mark.parametrize('index', range(14))
def test_every_ambiguous_environment_write_stops_without_retry(setup, index):
    release, render, _ = setup
    def after(method, sid, body):
        if method == 'PUT' and sum(call[0] == 'PUT' for call in render.calls) == index + 1:
            render.unknown_put = True
    render.after_write = after
    with pytest.raises(ReleaseError, match='lost environment write response'):
        release.run()
    puts = [call for call in render.calls if call[0] == 'PUT']
    assert len(puts) == index + 1
    assert release.record['changes'][-1]['confirmed'] is False


@pytest.mark.parametrize('index', range(10))
def test_owner_retirement_required_after_every_deployment(setup, index):
    release, render, _ = setup
    def after(method, sid, body):
        if method == 'POST' and len(render.history) == index + 1:
            if render.retiring and render.retiring[1]:
                render.block_retirement = True
            else:
                render.owners[API_ID].append('unexpected@owner')
    render.after_write = after
    with pytest.raises(ReleaseError):
        release.run()
    assert len(render.history) == index + 1


@pytest.mark.parametrize('fault', ['env', 'topology', 'deploy', 'owner', 'policy', 'source', 'counts'])
def test_drift_after_mutation_stops_before_next_mutation(setup, fault):
    release, render, _ = setup
    def after(method, sid, body):
        if method != 'PUT' or sid != API_ID:
            return
        if fault == 'env':
            render.env[BROKER]['SESSION_SECRET'] = 'drift'
        elif fault == 'topology':
            render.metadata[BROKER]['serviceDetails']['numInstances'] = 2
        elif fault == 'deploy':
            render.live[BROKER]['id'] = 'dep-unrelated'
        elif fault == 'owner':
            render.owners[BROKER].append('unrelated@owner')
        elif fault == 'policy':
            render.policy = 'd' * 64
        elif fault == 'source':
            render.installed[BROKER]['MAINTENANCE_DRAIN'] = 'true'
        else:
            render.counts['model_requests'] = 1
    render.after_write = after
    with pytest.raises(ReleaseError):
        release.run()
    assert len(render.history) == 1
    assert len([c for c in render.calls if c[0] == 'PUT']) == 2


@pytest.fixture
def offline(setup, monkeypatch):
    release, render, github = setup
    monkeypatch.setitem(CANDIDATE, 'schema', 6)
    monkeypatch.setitem(CANDIDATE, 'schema_support', {'protocol': 1, 'required': 6, 'compatibility': 6})
    monkeypatch.setitem(CANDIDATE, 'source_schema_digest', 'b' * 64)
    calls = []
    def migrate(service, expected):
        assert service.id == COORDINATOR and service.env['RENDER_MIGRATION_STAGE'] == 'true'
        assert len(render.history) == 5 and not sum(render.owners.values(), [])
        assert release.record['schema_migration']['status'] == 'requested'
        assert release.record['schema_migration']['confirmed'] is False
        assert expected == {'schema': 5, 'policy': OLD_CONTRACT['source_policy'],
            'contracts': OLD_CONTRACT['contracts'], 'owners': [], 'coordinators': [],
            'brokers': [], 'broker_leaders': [], 'sha': NEW, 'source_schema': 6,
            'source_schema_digest': 'b' * 64}
        calls.append(deepcopy(expected))
        render.schema = 6
        return {'status': 'applied', 'from_revision': 5, 'to_revision': 6, 'source_digest': 'b' * 64}
    release.offline_migrate = migrate
    return release, render, github, calls


def test_incompatible_schema_migrates_after_zero_owner_and_before_activation(offline):
    release, render, _, calls = offline
    release.run()
    assert len(calls) == 1
    assert release.record['schema_migration'] == {'plan': {'mode': 'offline', 'from_revision': 5,
        'to_revision': 6, 'source_digest': 'b' * 64}, 'status': 'applied', 'confirmed': True, 'verified': True}
    assert release.record['schema_revision'] == 6 and release.record['status'] == 'success'
    assert len(render.history) == 10


def test_offline_preflight_validates_but_never_invokes_migration(offline):
    release, render, _, calls = offline
    release.preflight_only = True
    release.run()
    assert not calls and not render.calls
    assert release.record['schema_migration']['status'] == 'planned'


def test_missing_offline_transport_blocks_before_drain(offline):
    release, render, _, _ = offline
    release.offline_migrate = None
    with pytest.raises(ReleaseError, match='explicit offline'):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['lost_ack', 'wrong_digest', 'wrong_from', 'wrong_to', 'already_applied',
                                 'extra_field', 'schema', 'policy', 'owner', 'lease', 'settings'])
def test_uncertain_or_invalid_offline_migration_never_activates(offline, fault):
    release, render, _, calls = offline
    original = release.offline_migrate
    def migrate(service, expected):
        result = original(service, expected)
        if fault == 'lost_ack':
            raise ReleaseError('Synthetic unknown schema outcome')
        if fault == 'wrong_digest':
            result['source_digest'] = 'c' * 64
        elif fault == 'wrong_from':
            result['from_revision'] = 4
        elif fault == 'wrong_to':
            result['to_revision'] = 7
        elif fault == 'already_applied':
            result['status'] = 'already_applied'
        elif fault == 'extra_field':
            result['unexpected'] = True
        elif fault == 'schema':
            render.schema = 5
        elif fault == 'policy':
            render.policy = CANDIDATE['source_policy']
        elif fault == 'owner':
            render.owners[API_ID] = ['unknown@runtime']
        elif fault == 'lease':
            render.counts['live_leases'] = 1
        elif fault == 'settings':
            render.env[BROKER]['SESSION_SECRET'] = 'changed'
        return result
    release.offline_migrate = migrate
    with pytest.raises(ReleaseError):
        release.run()
    assert len(calls) == 1 and len(render.history) == 5
    assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.env.values())
    assert release.record['schema_migration']['verified'] is False


@pytest.mark.parametrize('fault', ['missing', 'malformed', 'wrong_schema'])
def test_candidate_schema_digest_and_support_are_required_before_mutation(setup, fault):
    release, render, _ = setup
    value = deepcopy(CANDIDATE)
    if fault == 'missing':
        value.pop('source_schema_digest')
    elif fault == 'malformed':
        value['source_schema_digest'] = 'private-non-digest'
    else:
        value['schema_support']['required'] = 20
    release.contract = lambda env: value
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls and 'private-non-digest' not in release.receipt.read_text()


def test_candidate_schema_downgrade_refused_before_drain(setup):
    release, render, _ = setup
    release.contract = lambda env: {**deepcopy(CANDIDATE), 'schema': 4,
        'schema_support': {'protocol': 1, 'required': 4, 'compatibility': 4}}
    with pytest.raises(ReleaseError, match='downgrade'):
        release.run()
    assert not render.calls


def test_additive_schema_without_reviewed_plan_does_not_fall_back_to_offline(setup):
    release, render, _ = setup
    release.contract = lambda env: {**deepcopy(CANDIDATE), 'schema': 6,
        'schema_support': {'protocol': 1, 'required': 6, 'compatibility': 5}}
    release.migration_plan = lambda *args: None
    release.offline_migrate = lambda *args: pytest.fail('Invalid online plan switched to offline mutation')
    with pytest.raises(ReleaseError, match='migration plan'):
        release.run()
    assert not render.calls


def test_final_undrain_rechecks_counts_before_put_and_post(setup):
    release, render, _ = setup
    def after(method, sid, body):
        if method == 'PUT' and sid == WORKER and body['value'] == 'false' and len(render.history) == 9:
            render.counts['model_requests'] = 1
    render.after_write = after
    with pytest.raises(ReleaseError, match='reappeared'):
        release.run()
    assert len(render.history) == 9  # Final undrained worker was never deployed.


def test_candidate_can_be_unready_temporarily_without_skipping_retirement(setup):
    release, render, _ = setup
    probe = release.probe
    observed = []
    def slow(service):
        state = probe(service)
        if release.selected == service.id and not observed:
            observed.append(service.id)
            return {**state, 'ready': False}
        return state
    release.probe = slow
    release.run()
    assert observed == [WORKER] and release.record['status'] == 'success'


def test_terminal_service_failure_does_not_activate_any_following_role(setup):
    release, render, _ = setup
    render.fail_service = API_ID
    with pytest.raises(ReleaseError, match='deployment failed'):
        release.run()
    assert len(render.history) == 2
    assert release.record['status'] == 'failed'


def test_read_only_transport_failure_is_bounded_without_mutation(setup):
    release, render, _ = setup
    attempts = []
    def unavailable(service):
        attempts.append(service.id)
        return {'ok': False, 'reason': 'ssh_connection_lost'}
    release.probe = unavailable
    with pytest.raises(ReleaseError, match='after 3 attempt'):
        release.run()
    assert attempts == [COORDINATOR] * 3 and not render.calls


def test_demo_runs_real_controller_and_records_all_safety_scenarios(tmp_path):
    from scripts.release.rehearse_maintenance import rehearse
    result = rehearse(tmp_path, log=lambda *args, **kwargs: None)
    assert [(v['status'], v['migration_attempts']) for v in result] == [
        ('success', 1), ('failed', 0), ('failed', 1)]
    assert all(v['confirmed_default_changes'] == 4 and v['old_worker_drain_default'] == 'codex'
               and v['candidate_default'] == 'deepagents-native' for v in result)


def test_compatible_coordinator_preserves_existing_cluster_policy(setup, monkeypatch):
    release, render, _ = setup
    monkeypatch.setitem(CANDIDATE, 'contracts', deepcopy(OLD_CONTRACT['contracts']))
    release.run()
    assert release.record['status'] == 'success'
    assert render.policy == OLD_CONTRACT['source_policy']
    assert render.policy != CANDIDATE['source_policy']


def test_changed_role_contract_cannot_rely_on_unchanged_coordinator(setup, monkeypatch):
    release, render, _ = setup
    monkeypatch.setitem(CANDIDATE['contracts'], 'coordinator', OLD_CONTRACT['contracts']['coordinator'])
    with pytest.raises(ReleaseError, match='coordinator compatibility'):
        release.run()
    assert not render.calls


def explicit_harnesses(release, render, values):
    """Model immutable process environments separately from saved Render env."""
    import hashlib
    import json
    from scripts.release.deploy import MUTABLE
    for sid, value in values.items():
        render.env[sid]['AGENT_HARNESS'] = value
        render.installed[sid]['AGENT_HARNESS'] = value
    original_probe, original_call = release.probe, render.call
    contract_environments = []
    def contract(env):
        contract_environments.append(deepcopy(env))
        return {**deepcopy(CANDIDATE), 'default_harness': env.get('AGENT_HARNESS', 'deepagents-native')}
    def probe(service):
        options = service.probe_options()
        actual = {key: render.installed[service.id].get(key) for key in options['env_keys']}
        if hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest() != options['env_digest']:
            return {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'environment'}
        state = original_probe(service)
        state['default_harness'] = render.installed[service.id].get('AGENT_HARNESS', state['default_harness'])
        return state
    def call(method, path, body=None):
        sid = path.split('/')[2]
        if method != 'PUT' or not path.endswith('/AGENT_HARNESS'):
            return original_call(method, path, body)
        assert 'AGENT_HARNESS' not in MUTABLE
        assert body == {'value': 'deepagents-native'}
        assert render.env[sid]['RENDER_MIGRATION_STAGE'] == 'true'
        assert render.env[sid]['MOYAI_BUILD_SHA'] == NEW
        assert release.drained and not release.selected
        assert render.installed[sid]['AGENT_HARNESS'] == values[sid]
        assert release.running[sid].original['AGENT_HARNESS'] == values[sid]
        assert release.record['changes'][-1] == {'service': sid, 'key': 'AGENT_HARNESS',
            'previous': values[sid], 'value': 'deepagents-native', 'confirmed': False}
        render.before_write(method, sid, body)
        render.calls.append((method, sid, 'AGENT_HARNESS', body['value']))
        render.env[sid]['AGENT_HARNESS'] = body['value']
        render.after_write(method, sid, body)
        if render.unknown_put:
            raise ReleaseError('Synthetic lost default harness response')
        return {'key': 'AGENT_HARNESS', 'value': body['value']}
    release.contract, release.probe, render.call = contract, probe, call
    return contract_environments


@pytest.mark.parametrize('harness', ['codex', 'claude-agent-sdk'])
def test_explicit_native_default_changes_only_for_each_staged_candidate(setup, harness):
    release, render, _ = setup
    values = {sid: harness for sid in render.env}
    contract_envs = explicit_harnesses(release, render, values)
    original = deepcopy(render.env)
    release.run()
    assert len(contract_envs) == 4
    assert all(env['AGENT_HARNESS'] == 'deepagents-native' for env in contract_envs)
    changes = [call for call in render.calls if call[0] == 'PUT' and call[2] == 'AGENT_HARNESS']
    assert [call[1] for call in changes] == [API_ID, WORKER, BROKER, COORDINATOR]
    assert render.history[0]['env']['AGENT_HARNESS'] == harness  # Drain redeploy remains the old engine.
    assert all(history['env']['AGENT_HARNESS'] == 'deepagents-native' for history in render.history[1:])
    for sid, env in original.items():
        assert render.env[sid] == {**env, 'MOYAI_BUILD_SHA': NEW, 'AGENT_HARNESS': 'deepagents-native'}
    assert all(entry['confirmed'] and entry['previous'] == harness
               for entry in release.record['changes'] if entry['key'] == 'AGENT_HARNESS')
    assert release.record['status'] == 'success'


def test_explicit_default_migration_preflight_is_read_only_and_reviewable(setup):
    release, render, _ = setup
    explicit_harnesses(release, render, {sid: 'codex' for sid in render.env})
    release.preflight_only = True
    release.run()
    assert not render.calls
    assert len(release.record['default_harness_migration']) == 4
    assert all(entry['previous'] == 'codex' and entry['value'] == 'deepagents-native'
               for entry in release.record['default_harness_migration'])
    assert all(env['AGENT_HARNESS'] == 'codex' for env in render.env.values())


def test_mixed_native_defaults_are_normalized_without_creating_absent_keys(setup):
    release, render, _ = setup
    values = {API_ID: 'codex', WORKER: 'claude-agent-sdk', BROKER: 'deepagents-native'}
    explicit_harnesses(release, render, values)
    release.run()
    changes = [call[1] for call in render.calls if call[0] == 'PUT' and call[2] == 'AGENT_HARNESS']
    assert changes == [API_ID, WORKER]
    assert 'AGENT_HARNESS' not in render.env[COORDINATOR]
    assert len(release.record['default_harness_migration']) == 2


def test_already_deepagents_override_is_not_rewritten(setup):
    release, render, _ = setup
    explicit_harnesses(release, render, {sid: 'deepagents-native' for sid in render.env})
    release.run()
    assert not [call for call in render.calls if call[0] == 'PUT' and call[2] == 'AGENT_HARNESS']
    assert release.record['default_harness_migration'] == []


@pytest.mark.parametrize('sid', [API_ID, WORKER, BROKER, COORDINATOR])
def test_ambiguous_default_write_never_retries_or_launches_candidate(setup, sid):
    release, render, _ = setup
    explicit_harnesses(release, render, {service: 'codex' for service in render.env})
    def after(method, target, body):
        if method == 'PUT' and target == sid and body['value'] == 'deepagents-native':
            render.unknown_put = True
    render.after_write = after
    with pytest.raises(ReleaseError, match='lost default harness'):
        release.run()
    assert len([call for call in render.calls if call[:3] == ('PUT', sid, 'AGENT_HARNESS')]) == 1
    assert render.installed[sid]['AGENT_HARNESS'] == 'codex'
    assert release.record['changes'][-1]['confirmed'] is False
    assert len(render.history) == 1 + [API_ID, WORKER, BROKER, COORDINATOR].index(sid)


def test_default_write_response_cannot_hide_other_environment_drift(setup):
    release, render, _ = setup
    explicit_harnesses(release, render, {sid: 'codex' for sid in render.env})
    def after(method, sid, body):
        if method == 'PUT' and body['value'] == 'deepagents-native':
            render.env[sid]['SESSION_SECRET'] = 'external-change'
    render.after_write = after
    with pytest.raises(ReleaseError, match='Saved environment'):
        release.run()
    assert len(render.history) == 1
    assert release.record['changes'][-1]['confirmed'] is False


@pytest.mark.parametrize('boundary', ['drain', 'staged', 'candidate_sha'])
def test_default_migration_refuses_outside_exact_candidate_staging_boundary(setup, boundary):
    release, render, _ = setup
    explicit_harnesses(release, render, {sid: 'codex' for sid in render.env})
    release.preflight()
    service = release.expected[API_ID]
    release.drained = boundary != 'drain'
    service.env['RENDER_MIGRATION_STAGE'] = 'false' if boundary == 'staged' else 'true'
    service.env['MOYAI_BUILD_SHA'] = OLD if boundary == 'candidate_sha' else NEW
    with pytest.raises(ReleaseError, match='only for a drained, staged candidate'):
        release.migrate_default_harness(service)
    assert not render.calls


def test_same_build_with_native_override_is_not_a_noop(setup):
    release, render, _ = setup
    render.policy, render.contracts = CANDIDATE['source_policy'], deepcopy(CANDIDATE['contracts'])
    for sid in render.env:
        render.env[sid]['MOYAI_BUILD_SHA'] = NEW
        render.installed[sid]['MOYAI_BUILD_SHA'] = NEW
        render.live[sid]['commit']['id'] = NEW
    explicit_harnesses(release, render, {sid: 'codex' for sid in render.env})
    release.run()
    assert len(render.history) == 10
    assert all(env['AGENT_HARNESS'] == 'deepagents-native' for env in render.env.values())
    assert release.record['status'] == 'success'
