"""Controller failure boundaries; actual-app handoff is qualified separately."""
from copy import deepcopy

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, Release, ReleaseError
from scripts.release.native_api_probe import health_identity, piped_source
from scripts.release.rehearse import NEW
from scripts.release.rehearse_api import API_ID, CONTRACT
from test_api_deploy import setup  # noqa: F401
from test_rolling_probe import response


NATIVE = {
    'schema': 'native_api_test', 'state': 'receipted',
    'app': {'v1': '1' * 64},
    'harness': {'sdk_version': '2.55.0', 'harness_version': '0.55.0'},
}
CONTRACTS = {**{role: digit * 64 for role, digit in zip(
    ('worker', 'broker', 'coordinator'), 'abd')}, 'api': CONTRACT['contract']}
DRAIN = {'core': 'pydantic-v1', 'generation': 7, 'paused': False}


@pytest.fixture
def native_api(setup):
    release, render, github = setup
    for env in render.env.values():
        env.update(MOYAI_CORE='pydantic', MOYAI_PRODUCT_MODE='existing',
                   MOYAI_COORDINATOR_HANDOFF='true', MOYAI_NATIVE_SCHEMA=NATIVE['schema'])
    render.metadata[API_ID]['serviceDetails']['healthCheckPath'] = '/health'
    probe = render.probe
    def native_probe(service):
        state = probe(service)
        if not state.get('ok'):
            return state
        identity = (state['api_owner'] if service.role == 'api'
                    else next(owner for owner in render.background_owners if owner.endswith('@' + service.role)))
        return {**state, 'core': 'pydantic-v1', 'role': service.role, 'identity': identity,
                'coordinator_state': 'leader' if service.role == 'coordinator' else None,
                'broker_draining': False if service.role == 'broker' else None,
                'broker_leaders': ['3@broker'], 'contracts': deepcopy(CONTRACTS),
                'native': deepcopy(NATIVE), 'native_drain': deepcopy(DRAIN)}
    release.probe = native_probe
    release.contract = lambda env: {**CONTRACT, 'contracts': deepcopy(CONTRACTS), 'native': deepcopy(NATIVE)}
    github.rolling_ready = github.ready
    github.native_ready = github.ready
    return release, render, github


def test_native_api_replacement_preserves_background_and_maintenance_generation(native_api):
    release, render, _ = native_api
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success'
    assert render.calls == [('PUT', API_ID, 'MOYAI_BUILD_SHA', NEW), ('POST', API_ID, NEW)]
    assert release.record['native'] == NATIVE
    assert release.record['native_drain'] == DRAIN
    assert release.record['background_identities'] == {
        'coordinator': '1@coordinator', 'worker': '2@worker', 'broker': '3@broker'}
    for sid in (COORDINATOR, BROKER, WORKER):
        assert render.env[sid] == original[sid]


def test_native_preflight_does_not_pause_or_write(native_api):
    release, render, _ = native_api
    release.preflight_only = True
    release.run()
    assert release.record['status'] == 'preflight_passed'
    assert not render.calls


@pytest.mark.parametrize('path', [None, '', '/'])
def test_native_api_requires_provider_http_readiness_before_switch(native_api, path):
    release, render, _ = native_api
    render.metadata[API_ID]['serviceDetails']['healthCheckPath'] = path
    with pytest.raises(ReleaseError, match='HTTP readiness'):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['app_addition', 'app_removal', 'receipt', 'harness', 'worker', 'api', 'schema'])
def test_native_candidate_incompatibility_never_mutates(native_api, fault):
    release, render, _ = native_api
    contract = release.contract({})
    if fault == 'app_addition':
        contract['native']['app']['v2'] = '2' * 64
    elif fault == 'app_removal':
        contract['native']['app'].clear()
    elif fault == 'receipt':
        contract['native']['app']['v1'] = '3' * 64
    elif fault == 'harness':
        contract['native']['harness']['harness_version'] = 'new-version'
    elif fault in {'worker', 'api'}:
        contract['contracts'][fault] = '0' * 64
    else:
        contract['schema'] += 1
    release.contract = lambda env: contract
    with pytest.raises(ReleaseError, match='incompatible'):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['paused', 'missing_receipt', 'wrong_role', 'wrong_core',
                                  'missing_identity', 'broker_leader', 'unready_worker'])
def test_native_baseline_must_be_running_and_correlated_before_writes(native_api, fault):
    release, render, _ = native_api
    probe = release.probe
    def invalid(service):
        state = probe(service)
        if fault == 'paused': state['native_drain']['paused'] = True
        if fault == 'missing_receipt': state['native']['state'] = 'empty'
        if fault == 'wrong_role': state['role'] = 'api'
        if fault == 'wrong_core': state['core'] = 'legacy'
        if fault == 'missing_identity': state['identity'] = None
        if fault == 'broker_leader': state['broker_leaders'] = []
        if fault == 'unready_worker' and service.role == 'worker': state['ready'] = False
        return state
    release.probe = invalid
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls


@pytest.mark.parametrize('fault', ['receipt', 'pause', 'generation', 'worker', 'broker_leader'])
def test_native_drift_after_build_update_stops_before_deploy(native_api, fault):
    release, render, _ = native_api
    probe = release.probe
    def drifting(service):
        state = probe(service)
        if render.calls:
            if fault == 'receipt': state['native']['app']['v2'] = '2' * 64
            elif fault == 'pause': state['native_drain']['paused'] = True
            elif fault == 'generation': state['native_drain']['generation'] += 1
            elif fault == 'worker' and service.role == 'worker': state['identity'] = '9@worker'
            elif fault == 'broker_leader': state['broker_leaders'] = []
        return state
    release.probe = drifting
    with pytest.raises(ReleaseError):
        release.run()
    assert render.calls == [('PUT', API_ID, 'MOYAI_BUILD_SHA', NEW)]


@pytest.mark.parametrize('workflow', ['native_ready', 'rolling_ready'])
def test_native_api_requires_exact_commit_native_and_release_ci(native_api, workflow):
    release, render, github = native_api
    def failed(sha):
        assert sha == NEW
        raise ReleaseError('Required CI failed')
    setattr(github, workflow, failed)
    with pytest.raises(ReleaseError, match='Required CI failed'):
        release.run()
    assert not render.calls


def test_native_startup_failure_retains_old_api_and_receipts(native_api):
    release, render, _ = native_api
    render.fail_service = API_ID
    with pytest.raises(ReleaseError, match='previous healthy'):
        release.run()
    assert release.record['status'] == 'rolled_back'
    assert release.record['native_drain'] == DRAIN
    assert len([call for call in render.calls if call[0] == 'POST']) == 1


def test_native_rolling_background_deploys_remain_rejected(native_api):
    release, render, github = native_api
    # The generic controller's native admission guard is unchanged.
    generic = Release(render, github, release.probe, release.receipt)
    with pytest.raises(ReleaseError, match='Native services require'):
        generic.inspect(COORDINATOR, 'coordinator')


@pytest.mark.parametrize('fault', [None, 'core', 'role', 'build', 'owner', 'api_capability'])
def test_native_api_health_requires_process_and_core_identity(fault):
    body = response('api')
    body.headers['X-Moyai-Core'] = 'pydantic-v1'
    body.headers['X-Moyai-API-Release'] = '1'
    field = {'core': 'X-Moyai-Core', 'role': 'X-Moyai-Role', 'build': 'X-Moyai-Build',
             'owner': 'X-Moyai-Owner', 'api_capability': 'X-Moyai-API-Release'}.get(fault)
    if field:
        body.headers.replace_header(field, 'wrong')
        with pytest.raises(ValueError):
            health_identity(body, {'sha': 'a' * 40, 'role': 'api'}, {'owners': ['42@start']})
    else:
        observed = health_identity(body, {'sha': 'a' * 40, 'role': 'api'}, {'owners': ['42@start']})
        assert observed['ready'] and observed['api_owner'] == '42@start'


def test_piped_native_helpers_do_not_require_installed_release_scripts():
    namespace = {'__name__': 'piped_probe'}
    exec(piped_source(), namespace)
    native = namespace['_native_api_probe']
    assert native._rolling_probe is namespace['_rolling_probe']
    assert native._native_probe is namespace['_native_probe']
