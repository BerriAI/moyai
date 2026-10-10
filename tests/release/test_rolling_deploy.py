"""Synthetic control-plane failures; no production requests or credentials."""
from copy import deepcopy
import json

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID, APIRender
from scripts.release.rolling_deploy import RollingRelease

CONTRACT = {'schema': 5, 'contracts': {role: digit * 64 for role, digit in
             zip(('api', 'worker', 'broker', 'coordinator'), 'abcd')}}


class RollingRender(APIRender):
    def __init__(self):
        super().__init__()
        for env in self.env.values():
            env['MOYAI_COORDINATOR_HANDOFF'] = 'true'
        self.owners = {sid: [str(index + 1) + '@original'] for index, sid in enumerate(self.env)}
        self.active_service = None
        self.retirement_polls = 8
        self.contracts = deepcopy(CONTRACT)
        self.policy = 'f' * 64
        self.ready = True

    def pages(self, path, key):
        if key == 'envVar':
            return super().pages(path, key)
        sid = path.split('/')[2]
        return [deepcopy(self.live[sid]), *[deepcopy(deploy) for deploy in self.deployments.values()
            if deploy.get('service') == sid and deploy['id'] != self.live[sid]['id']]]

    def call(self, method, path, body=None):
        sid = path.split('/')[2]
        if method == 'POST':
            self.calls.append((method, sid, body['commitId']))
            deployment = {'id': 'dep-rolling' + str(len(self.deployments)), 'service': sid,
                'commit': {'id': body['commitId']}, 'status': 'update_failed' if self.fail_service == sid else 'live'}
            self.deployments[deployment['id']] = deepcopy(deployment)
            if deployment['status'] == 'live':
                self.owners[sid].append(str(10 + len(self.deployments)) + '@replacement')
                self.live[sid] = deepcopy(deployment)
                self.active_service = sid
                self.remaining = self.retirement_polls
            if self.unknown_write:
                raise ReleaseError('Synthetic lost write response')
            return deepcopy(deployment)
        return super().call(method, path, body)

    def probe(self, service):
        if self.active_service:
            if self.remaining:
                self.remaining -= 1
            else:
                self.owners[self.active_service] = self.owners[self.active_service][-1:]
                self.active_service = None
        owner = self.owners[service.id][-1]
        ready = self.ready and not (self.unhealthy and service.id == self.active_service)
        if self.unreachable and service.id == self.active_service:
            return {'ok': False}
        return {'ok': self.probe_ok, 'ready': ready, 'identity': owner, 'policy': self.policy,
            **deepcopy(self.contracts), 'owners': sorted(sum(self.owners.values(), [])),
            'coordinators': self.owners[COORDINATOR][:1], 'brokers': self.owners[BROKER][:],
            'broker_leaders': self.owners[BROKER][:1], 'broker_draining': False,
            'coordinator_state': 'leader' if owner == self.owners[COORDINATOR][0] else 'standby',
            'unsafe_sessions': 100, 'model_requests': 32, 'live_leases': 100}


@pytest.fixture
def setup(tmp_path):
    render, github, elapsed = RollingRender(), Candidate(), [0]
    github.rolling_ready = github.ready
    github.forward_from = lambda previous, selected: None
    def sleep(seconds): elapsed[0] += seconds
    release = RollingRelease(render, github, render.probe, tmp_path / 'receipt.json',
        api_service_id=API_ID, contract=lambda env: deepcopy(CONTRACT), wait_seconds=1,
        readiness_timeout=3, retirement_timeout=4, deploy_timeout=3,
        clock=lambda: elapsed[0], sleep=sleep)
    return release, render, github


def test_continuous_work_rolls_one_role_at_a_time_and_preserves_policy(setup):
    release, render, _ = setup
    original = deepcopy(render.env)
    release.run()
    assert release.record['status'] == 'success'
    assert [call for call in render.calls if call[0] == 'POST'] == [
        ('POST', sid, NEW) for sid in (WORKER, BROKER, COORDINATOR, API_ID)]
    assert all(len(owners) == 1 for owners in render.owners.values())
    assert len(release.record['replacements']) == 4
    for sid in original:
        assert render.env[sid] == {**original[sid], 'MOYAI_BUILD_SHA': NEW}
    assert all(call[2] == 'MOYAI_BUILD_SHA' for call in render.calls if call[0] == 'PUT')
    text = release.receipt.read_text()
    assert all(secret not in text for secret in ('do-not-log', 'postgresql://', 'private-bucket'))


@pytest.mark.parametrize('preflight', [True, False])
def test_preflight_and_noop_make_no_writes(setup, preflight):
    release, render, github = setup
    release.preflight_only = preflight
    if not preflight:
        github.sha = OLD
    release.run()
    assert render.calls == []
    assert release.record['status'] == ('preflight_passed' if preflight else 'success')


@pytest.mark.parametrize('role', ['api', 'worker', 'broker', 'coordinator'])
@pytest.mark.parametrize('change', ['disk', 'shutdown', 'command', 'schema', 'handoff', 'drain', 'stage', 'saved_sha'])
def test_invalid_topology_never_writes(setup, role, change):
    release, render, _ = setup
    sid = next(sid for sid, env in render.env.items() if env['MOYAI_RUNTIME_ROLE'] == role)
    details = render.metadata[sid]['serviceDetails']
    if change == 'disk': details['disk'] = {'id': 'persistent'}
    elif change == 'shutdown': details['maxShutdownDelaySeconds'] = 30
    elif change == 'command': details['envSpecificDetails']['preDeployCommand'] = 'migrate'
    elif change == 'schema': render.env[sid]['MOYAI_SCHEMA_MODE'] = 'auto'
    elif change == 'handoff': render.env[sid]['MOYAI_COORDINATOR_HANDOFF'] = 'false'
    elif change == 'drain': render.env[sid]['MAINTENANCE_DRAIN'] = 'true'
    elif change == 'stage': render.env[sid]['RENDER_MIGRATION_STAGE'] = 'true'
    elif change == 'saved_sha': render.env[sid]['MOYAI_BUILD_SHA'] = NEW
    with pytest.raises(ReleaseError): release.run()
    assert render.calls == []


@pytest.mark.parametrize('change', ['schema', 'api', 'worker', 'broker', 'coordinator'])
def test_candidate_contract_drift_fails_before_any_write(setup, change):
    release, render, _ = setup
    contract = deepcopy(CONTRACT)
    if change == 'schema': contract['schema'] += 1
    else: contract['contracts'][change] = 'e' * 64
    release.contract = lambda env: contract
    with pytest.raises(ReleaseError, match='incompatible'): release.run()
    assert render.calls == []


@pytest.mark.parametrize('sid', [WORKER, BROKER, COORDINATOR, API_ID])
def test_terminal_failure_stops_without_retry_or_rollback(setup, sid):
    release, render, _ = setup
    render.fail_service = sid
    with pytest.raises(ReleaseError, match='Candidate deployment failed'): release.run()
    deployments = [call for call in render.calls if call[0] == 'POST']
    assert deployments[-1] == ('POST', sid, NEW)
    assert len([call for call in deployments if call[1] == sid]) == 1
    assert render.live[sid]['commit']['id'] == OLD
    assert render.env[sid]['MOYAI_BUILD_SHA'] == NEW  # Ambiguity requires explicit recovery.
    assert release.record['status'] == 'failed'


@pytest.mark.parametrize('mutation', ['PUT', 'POST'])
def test_lost_write_response_is_never_retried(setup, mutation):
    release, render, _ = setup
    call = render.call
    def lost(method, path, body=None):
        result = call(method, path, body)
        if method == mutation: raise ReleaseError('Synthetic lost response')
        return result
    render.call = lost
    with pytest.raises(ReleaseError): release.run()
    assert len([call for call in render.calls if call[0] == mutation]) == 1
    assert release.record['status'] == 'failed'
    pending = release.record['changes'][-1] if mutation == 'PUT' else release.record['deploys'][-1]
    assert pending['confirmed'] is False if mutation == 'PUT' else pending['id'] is None


@pytest.mark.parametrize('change', ['environment', 'topology', 'owner', 'policy', 'contract', 'unexpected_deploy'])
def test_external_changes_stop_before_next_mutation(setup, change):
    release, render, _ = setup
    call = render.call
    def edited(method, path, body=None):
        result = call(method, path, body)
        if method == 'PUT':
            if change == 'environment': render.env[BROKER]['ENCRYPTION_KEY'] = 'changed'
            elif change == 'topology': render.metadata[BROKER]['serviceDetails']['numInstances'] = 2
            elif change == 'owner': render.owners[BROKER] = ['77@external']
            elif change == 'policy': render.policy = '0' * 64
            elif change == 'contract': render.contracts['contracts']['worker'] = '0' * 64
            else: render.deployments['dep-external'] = {'id': 'dep-external', 'service': BROKER, 'status': 'queued'}
        return result
    render.call = edited
    with pytest.raises(ReleaseError): release.run()
    assert not [call for call in render.calls if call[0] == 'POST']


def test_partial_compatible_release_resumes_only_remaining_roles(setup):
    release, render, _ = setup
    for sid in (WORKER, BROKER):
        render.env[sid]['MOYAI_BUILD_SHA'] = NEW
        render.live[sid]['commit']['id'] = NEW
    release.run()
    assert [call[1] for call in render.calls if call[0] == 'POST'] == [COORDINATOR, API_ID]


def test_old_owner_that_never_retires_blocks_the_next_role(setup):
    release, render, _ = setup
    render.retirement_polls = 1000
    with pytest.raises(ReleaseError, match='did not retire'): release.run()
    assert [call for call in render.calls if call[0] == 'POST'] == [('POST', WORKER, NEW)]


def test_candidate_probe_uncertainty_does_not_trigger_rollback(setup):
    release, render, _ = setup
    render.unreachable, render.retirement_polls = True, 1000
    with pytest.raises(ReleaseError, match='readiness could not'): release.run()
    assert [call for call in render.calls if call[0] == 'POST'] == [('POST', WORKER, NEW)]


def test_wrong_render_commit_stops_after_one_deployment(setup):
    release, render, _ = setup
    call = render.call
    def wrong(method, path, body=None):
        result = call(method, path, body)
        if method == 'GET' and '/deploys/' in path: result['commit']['id'] = OLD
        return result
    render.call = wrong
    with pytest.raises(ReleaseError, match='different commit'): release.run()
    assert len([call for call in render.calls if call[0] == 'POST']) == 1


def test_interruption_preserves_unconfirmed_deploy_intent(setup):
    release, render, _ = setup
    call = render.call
    def interrupted(method, path, body=None):
        result = call(method, path, body)
        if method == 'POST': raise KeyboardInterrupt()
        return result
    render.call = interrupted
    with pytest.raises(KeyboardInterrupt): release.run()
    assert json.loads(release.receipt.read_text())['deploys'][0]['id'] is None
    assert len([call for call in render.calls if call[0] == 'POST']) == 1
