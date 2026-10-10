"""API-only releases preserve background work and fail closed at ambiguous boundaries."""
from copy import deepcopy
import json

import pytest

from scripts.release.api_deploy import APIRelease
from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID, APIRender, CONTRACT


@pytest.fixture
def setup(tmp_path):
    render, github, elapsed = APIRender(), Candidate(), [0]
    def sleep(seconds): elapsed[0] += seconds
    release = APIRelease(render, github, render.probe, tmp_path / 'receipt.json',
        api_service_id=API_ID, contract=lambda env: CONTRACT, wait_seconds=1,
        readiness_timeout=3, retirement_timeout=4, deploy_timeout=3,
        clock=lambda: elapsed[0], sleep=sleep)
    return release, render, github


def test_only_api_changes_and_old_owner_retires_before_success(setup):
    release, render, _ = setup
    original = deepcopy(render.env)
    render.retirement_polls = 6
    release.run()
    assert release.record['status'] == 'success'
    assert render.calls == [('PUT', API_ID, 'MOYAI_BUILD_SHA', NEW), ('POST', API_ID, NEW)]
    assert len(render.api_owners) == 1 and render.remaining == 0
    for sid in (COORDINATOR, WORKER, BROKER):
        assert render.env[sid] == original[sid] and render.live[sid]['commit']['id'] == OLD
    assert render.env[API_ID] == {**original[API_ID], 'MOYAI_BUILD_SHA': NEW}
    receipt = release.receipt.read_text()
    assert all(secret not in receipt for secret in ('do-not-log', 'postgresql://', 'private-bucket'))


@pytest.mark.parametrize('preflight', [True, False])
def test_preflight_and_noop_are_read_only(setup, preflight):
    release, render, github = setup
    release.preflight_only = preflight
    if not preflight: github.sha = OLD
    release.run()
    assert render.calls == []
    assert release.record['status'] == ('preflight_passed' if preflight else 'success')


@pytest.mark.parametrize('change', ['disk', 'shutdown', 'command', 'predeploy', 'autoscaling', 'role',
    'schema', 'split', 'drain', 'staging', 'secret', 'build', 'owner', 'extra_owner', 'missing_owner', 'unhealthy'])
def test_invalid_baselines_never_write(setup, change):
    release, render, _ = setup
    details = render.metadata[API_ID]['serviceDetails']
    if change == 'disk': details['disk'] = {'id': 'disk-persistent'}
    elif change == 'shutdown': details['maxShutdownDelaySeconds'] = 30
    elif change == 'command': details['envSpecificDetails']['dockerCommand'] = 'python custom.py'
    elif change == 'predeploy': details['envSpecificDetails']['preDeployCommand'] = 'python -m app.schema_migrations --apply'
    elif change == 'autoscaling': details['autoscaling'] = {'enabled': True}
    elif change == 'role': render.env[API_ID]['MOYAI_RUNTIME_ROLE'] = 'coordinator'
    elif change == 'schema': render.env[BROKER]['MOYAI_SCHEMA_MODE'] = 'auto'
    elif change == 'split': render.env[API_ID]['MOYAI_SEPARATE_BROKER'] = 'false'
    elif change == 'drain': render.env[API_ID]['MAINTENANCE_DRAIN'] = 'true'
    elif change == 'staging': render.env[API_ID]['RENDER_MIGRATION_STAGE'] = 'true'
    elif change == 'secret': render.env[API_ID]['ENCRYPTION_KEY'] = 'changed'
    elif change == 'build': render.env[API_ID]['MOYAI_BUILD_SHA'] = NEW
    elif change == 'owner': render.metadata[API_ID]['ownerId'] = 'different-workspace'
    elif change == 'extra_owner': render.background_owners.append('99@unexpected')
    elif change == 'missing_owner': render.background_owners.remove('2@worker')
    elif change == 'unhealthy': render.probe_ok = False
    with pytest.raises(ReleaseError): release.run()
    assert render.calls == []


@pytest.mark.parametrize('candidate', [{'schema': 4, 'contract': CONTRACT['contract']},
    {'schema': 3, 'contract': 'd' * 64}])
def test_candidate_must_match_running_contract_before_writes(setup, candidate):
    release, render, _ = setup
    release.contract = lambda env: candidate
    with pytest.raises(ReleaseError, match='incompatible'): release.run()
    assert render.calls == []


def test_candidate_uses_actual_saved_environment_and_selected_build(setup):
    release, render, _ = setup
    def contract(env):
        assert env == {**render.env[API_ID], 'MOYAI_BUILD_SHA': NEW}
        return CONTRACT
    release.contract = contract
    release.run()


def test_startup_failure_restores_only_saved_build_without_redeploying_healthy_old_api(setup):
    release, render, _ = setup
    render.fail_service = API_ID
    with pytest.raises(ReleaseError, match='previous healthy'): release.run()
    assert release.record['status'] == 'rolled_back'
    assert render.live[API_ID]['id'] == 'dep-oldapi'
    assert render.env[API_ID]['MOYAI_BUILD_SHA'] == OLD
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW)]


def test_confirmed_live_health_failure_redeploys_old_commit_once_and_verifies(setup):
    release, render, _ = setup
    render.unhealthy = True
    with pytest.raises(ReleaseError, match='restored and verified'): release.run()
    assert release.record['status'] == 'rolled_back'
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW), ('POST', API_ID, OLD)]
    assert render.live[API_ID]['commit']['id'] == OLD
    assert render.env[API_ID]['MOYAI_BUILD_SHA'] == OLD
    assert len(render.api_owners) == 1


@pytest.mark.parametrize('mutation', ['PUT', 'POST'])
def test_ambiguous_writes_are_not_retried_or_rolled_back(setup, mutation):
    release, render, _ = setup
    call = render.call
    def lost(method, path, body=None):
        result = call(method, path, body)
        if method == mutation: raise ReleaseError('Synthetic lost response')
        return result
    render.call = lost
    with pytest.raises(ReleaseError): release.run()
    assert len([c for c in render.calls if c[0] == mutation]) == 1
    assert release.record['status'] == 'failed'
    assert render.env[API_ID]['MOYAI_BUILD_SHA'] == NEW
    entry = release.record['changes'][-1] if mutation == 'PUT' else release.record['deploys'][-1]
    assert (entry['confirmed'] is False) if mutation == 'PUT' else entry['id'] is None


def test_unknown_readiness_never_causes_rollback(setup):
    release, render, _ = setup
    render.unreachable = True
    with pytest.raises(ReleaseError, match='readiness could not'): release.run()
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW)]
    assert release.record['status'] == 'failed'


@pytest.mark.parametrize('change', ['environment', 'topology', 'background_owner', 'policy', 'extra_api'])
def test_external_changes_stop_before_next_write(setup, change):
    release, render, _ = setup
    call, probe = render.call, render.probe
    def altered(method, path, body=None):
        result = call(method, path, body)
        if method == 'PUT':
            if change == 'environment': render.env[BROKER]['ENCRYPTION_KEY'] = 'human-edit'
            if change == 'topology': render.metadata[API_ID]['serviceDetails']['disk'] = {'id': 'human-disk'}
            if change == 'background_owner': render.background_owners[1] = '92@new-worker'
            if change == 'policy':
                release.probe = lambda service: {**probe(service), 'policy': 'changed'}
            if change == 'extra_api': render.background_owners += ['98@unexpected', '99@unexpected']
        return result
    render.call = altered
    with pytest.raises(ReleaseError): release.run()
    assert not [c for c in render.calls if c[0] == 'POST']


def test_dashboard_edit_after_unhealthy_candidate_prevents_rollback(setup):
    release, render, _ = setup
    render.unhealthy = True
    probe = render.probe
    def changed(service):
        state = probe(service)
        if service.id == API_ID and service.sha == NEW:
            render.env[API_ID]['MAX_PENDING_RUNS'] = '700'
        return state
    release.probe = changed
    with pytest.raises(ReleaseError, match='environment changed'): release.run()
    assert render.env[API_ID]['MAX_PENDING_RUNS'] == '700'
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW)]


def test_old_api_that_never_drains_cannot_be_reported_successful(setup):
    release, render, _ = setup
    render.retirement_polls = 100
    with pytest.raises(ReleaseError, match='did not drain'): release.run()
    assert release.record['status'] == 'failed'
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW)]


def test_interruption_after_deploy_does_not_retry_or_rollback(setup):
    release, render, _ = setup
    call = render.call
    def interrupted(method, path, body=None):
        result = call(method, path, body)
        if method == 'POST': raise KeyboardInterrupt()
        return result
    render.call = interrupted
    with pytest.raises(KeyboardInterrupt): release.run()
    assert release.record['status'] == 'failed'
    assert len([c for c in render.calls if c[0] == 'POST']) == 1
    assert json.loads(release.receipt.read_text())['deploys'][0]['id'] is None


def test_api_can_already_be_a_different_compatible_build_than_background_roles(setup):
    release, render, github = setup
    api_build = 'd' * 40
    render.env[API_ID]['MOYAI_BUILD_SHA'] = api_build
    render.live[API_ID]['commit']['id'] = api_build
    compared = []
    github.forward_from = lambda previous, selected: compared.append((previous, selected))
    release.run()
    assert compared == [(api_build, NEW)]
    assert all(render.env[sid]['MOYAI_BUILD_SHA'] == OLD for sid in (COORDINATOR, BROKER, WORKER))


@pytest.mark.parametrize('outcome', ['canceled', 'queued', 'unrecognized'])
def test_cancelled_pending_or_unknown_deploy_never_causes_rollback(setup, outcome):
    release, render, _ = setup
    call = render.call
    def interrupted(method, path, body=None):
        result = call(method, path, body)
        if method == 'GET' and '/deploys/' in path:
            result['status'] = outcome
        return result
    render.call = interrupted
    with pytest.raises(ReleaseError): release.run()
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW)]
    assert release.record['status'] == 'failed'


def test_failed_rollback_remains_failed_and_is_never_retried(setup):
    release, render, _ = setup
    render.unhealthy = True
    call = render.call
    def failed(method, path, body=None):
        if method == 'POST' and body['commitId'] == OLD:
            render.fail_service = API_ID
        return call(method, path, body)
    render.call = failed
    with pytest.raises(ReleaseError): release.run()
    assert release.record['status'] == 'failed'
    assert [c for c in render.calls if c[0] == 'POST'] == [('POST', API_ID, NEW), ('POST', API_ID, OLD)]
    assert render.live[API_ID]['commit']['id'] == NEW


def test_saved_api_id_disables_coordinated_mode_before_credentials_or_mutations(monkeypatch):
    from scripts.release.deploy import main
    monkeypatch.setattr('sys.argv', ['deploy'])
    monkeypatch.setenv('GITHUB_REPOSITORY', 'BerriAI/moyai')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    monkeypatch.setenv('MOYAI_API_SERVICE_ID', API_ID)
    with pytest.raises(ReleaseError, match='dedicated API topology'): main()
