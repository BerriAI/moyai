"""Native rolling-controller boundaries; actual application proof is separate."""
from copy import deepcopy

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.native_rolling_deploy import NativeRollingRelease
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID
from test_rolling_deploy import CONTRACT, RollingRender


NATIVE = {'schema': 'native_rolling_test', 'state': 'receipted',
          'app': {'v1': '1' * 64}, 'harness': {'sdk_version': '2.55.0', 'harness_version': '0.55.0'}}
DRAIN = {'core': 'pydantic-v1', 'generation': 4, 'paused': False}


@pytest.fixture
def native_rolling(tmp_path):
    render, github, elapsed = RollingRender(), Candidate(), [0]
    for env in render.env.values():
        env.update(MOYAI_CORE='pydantic', MOYAI_PRODUCT_MODE='existing',
                   MOYAI_NATIVE_SCHEMA=NATIVE['schema'])
    github.rolling_ready = github.native_ready = github.ready
    github.forward_from = lambda previous, selected: None
    original = render.probe
    def probe(service):
        state = original(service)
        if not state.get('ok'):
            return state
        return {**state, 'core': 'pydantic-v1', 'role': service.role,
                'native': deepcopy(NATIVE), 'native_drain': deepcopy(DRAIN),
                'native_workers': list(render.owners[WORKER])}
    release = NativeRollingRelease(
        render, github, probe, tmp_path / 'receipt.json', api_service_id=API_ID,
        contract=lambda env: deepcopy(CONTRACT),
        manifest=lambda env: {key: deepcopy(NATIVE[key]) for key in ('schema', 'app', 'harness')},
        wait_seconds=1, readiness_timeout=4, retirement_timeout=6, deploy_timeout=4,
        clock=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        log=lambda *args, **kwargs: None)
    return release, render, github


def test_native_roles_roll_without_schema_changes_or_global_drain(native_rolling):
    release, render, _ = native_rolling
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success'
    assert [call for call in render.calls if call[0] == 'POST'] == [
        ('POST', sid, NEW) for sid in (WORKER, BROKER, COORDINATOR, API_ID)]
    assert release.record['native'] == NATIVE and release.record['native_drain'] == DRAIN
    assert release.record['schema_migration'] is None
    assert len(release.record['replacements']) == 4
    for sid in original:
        assert render.env[sid] == {**original[sid], 'MOYAI_BUILD_SHA': NEW}
        assert len(render.owners[sid]) == 1
    assert all(call[2] == 'MOYAI_BUILD_SHA' for call in render.calls if call[0] == 'PUT')
    assert 'private' not in release.receipt.read_text()


@pytest.mark.parametrize('preflight', [True, False])
def test_native_preflight_and_noop_remain_read_only(native_rolling, preflight):
    release, render, github = native_rolling
    release.preflight_only = preflight
    if not preflight:
        github.sha = OLD
    release.run()
    assert release.record['status'] == ('preflight_passed' if preflight else 'success')
    assert not render.calls


@pytest.mark.parametrize('fault', ['schema', 'app_addition', 'app_removal', 'manifest',
                                  'framework', 'native_schema', 'role_contract'])
def test_native_incompatible_candidate_stops_before_mutation(native_rolling, fault):
    release, render, _ = native_rolling
    native, contract = release.manifest({}), deepcopy(CONTRACT)
    if fault == 'schema': contract['schema'] += 1
    elif fault == 'app_addition': native['app']['v2'] = '2' * 64
    elif fault == 'app_removal': native['app'].clear()
    elif fault == 'manifest': native['app']['v1'] = '2' * 64
    elif fault == 'framework': native['harness']['sdk_version'] = 'different'
    elif fault == 'native_schema': native['schema'] = 'another_schema'
    else: contract['contracts']['worker'] = 'e' * 64
    release.source_contract = lambda env: deepcopy(contract)
    release.manifest = lambda env: deepcopy(native)
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['legacy', 'bootstrap', 'rehearsal'])
def test_source_must_be_an_active_verified_native_cluster(native_rolling, fault):
    release, render, _ = native_rolling
    env = render.env[COORDINATOR]
    if fault == 'legacy': env['MOYAI_CORE'] = 'legacy'
    elif fault == 'bootstrap': env['MOYAI_NATIVE_SCHEMA_MODE'] = 'bootstrap'
    else: env['MOYAI_PRODUCT_LOCAL_REHEARSAL'] = 'true'
    with pytest.raises(ReleaseError, match='already-running native'):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['paused', 'generation', 'receipt', 'core', 'unregistered', 'duplicate'])
def test_native_baseline_requires_receipts_and_exact_live_worker(native_rolling, fault):
    release, render, _ = native_rolling
    probe = release.probe
    def invalid(service):
        state = probe(service)
        if fault == 'paused': state['native_drain']['paused'] = True
        elif fault == 'generation': state['native_drain']['generation'] = 0
        elif fault == 'receipt': state['native']['state'] = 'empty'
        elif fault == 'core': state['core'] = 'legacy'
        elif fault == 'unregistered': state['native_workers'] = []
        else: state['native_workers'] *= 2
        return state
    release.probe = invalid
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['generation', 'pause', 'receipt', 'worker'])
def test_drift_after_intent_stops_before_deployment(native_rolling, fault):
    release, render, _ = native_rolling
    probe = release.probe
    def changed(service):
        state = probe(service)
        if render.calls:
            if fault == 'generation': state['native_drain']['generation'] += 1
            elif fault == 'pause': state['native_drain']['paused'] = True
            elif fault == 'receipt': state['native']['app']['v2'] = '2' * 64
            else: state['native_workers'] = ['999@unexpected']
        return state
    release.probe = changed
    with pytest.raises(ReleaseError):
        release.run()
    assert render.calls == [('PUT', WORKER, 'MOYAI_BUILD_SHA', NEW)]


def test_native_runtime_ci_is_required_before_any_write(native_rolling):
    release, render, github = native_rolling
    def failed(sha):
        assert sha == NEW
        raise ReleaseError('Native runtime CI failed')
    github.native_ready = failed
    with pytest.raises(ReleaseError, match='Native runtime CI failed'):
        release.run()
    assert not render.calls


def test_ready_replacement_worker_must_have_registered(native_rolling):
    release, render, _ = native_rolling
    probe = release.probe
    old_worker = render.owners[WORKER][0]
    def unregistered(service):
        state = probe(service)
        if service.id == WORKER and service.sha == NEW:
            state['native_workers'] = [old_worker] if old_worker in state['owners'] else []
        return state
    release.probe = unregistered
    with pytest.raises(ReleaseError, match='register|membership'):
        release.run()
    assert [call for call in render.calls if call[0] == 'POST'] == [('POST', WORKER, NEW)]


def test_worker_can_acquire_ownership_before_registration_while_warming(native_rolling):
    release, render, _ = native_rolling
    probe, warm_observations = release.probe, []
    old_worker = render.owners[WORKER][0]
    def warming(service):
        state = probe(service)
        if render.active_service == WORKER and service.id != WORKER:
            state['native_workers'] = [old_worker]
            warm_observations.append(service.role)
        return state
    release.probe = warming
    release.run()
    assert release.record['status'] == 'success' and warm_observations


def test_lost_deployment_response_is_not_retried_or_rolled_back(native_rolling):
    release, render, _ = native_rolling
    render.unknown_write = True
    with pytest.raises(ReleaseError):
        release.run()
    assert len([call for call in render.calls if call[0] == 'POST']) == 1
    assert release.record['deploys'][0]['id'] is None
    assert release.record['status'] == 'failed'
