"""Synthetic control-plane failures; no production requests or credentials."""
from copy import deepcopy
import json

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID, APIRender
from scripts.release.rolling_deploy import RollingRelease, WORKSPACE_IMAGE_PREDEPLOY

CONTRACT = {'schema': 5, 'contracts': {role: digit * 64 for role, digit in
             zip(('api', 'worker', 'broker', 'coordinator'), 'abcd')}}


class RollingRender(APIRender):
    def __init__(self):
        super().__init__()
        for env in self.env.values():
            env['MOYAI_COORDINATOR_HANDOFF'] = 'true'
        for sid in (WORKER, COORDINATOR):
            self.metadata[sid]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = WORKSPACE_IMAGE_PREDEPLOY
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
                if hasattr(self, 'replacement_support'):
                    self.schema_supports[sid] = deepcopy(self.replacement_support)
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
        support = getattr(self, 'schema_supports', {}).get(service.id)
        return {'ok': self.probe_ok, 'ready': ready, 'identity': owner, 'policy': self.policy,
            **({'schema_support': deepcopy(support)} if support is not None else {}),
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


def probe_sequence(release, outcomes, *, preflight=False):
    original, attempts = release.probe, []
    def probe(service):
        if (service.id == COORDINATOR and release.selected == (None if preflight else WORKER)
                and len(attempts) < len(outcomes)):
            value = outcomes[len(attempts)]
            attempts.append(service.role)
            return value(original(service)) if callable(value) else deepcopy(value)
        return original(service)
    release.probe = probe
    return attempts


@pytest.mark.parametrize('reason', ['ssh_timeout', 'ssh_connection_lost', 'database_unavailable'])
@pytest.mark.parametrize('preflight', [True, False])
def test_transient_read_only_probes_recover_without_repeating_writes(setup, reason, preflight):
    release, render, _ = setup
    attempts = probe_sequence(release, [{'ok': False, 'reason': reason}] * 2 + [lambda state: state],
                              preflight=preflight)
    release.run()
    assert len(attempts) == 3 and release.record['status'] == 'success'
    assert release.record['probe_failures'] == [
        {'role': 'coordinator', 'reason': reason, 'attempt': attempt} for attempt in (1, 2)]
    assert [call[1] for call in render.calls if call[0] == 'PUT'] == [WORKER, BROKER, COORDINATOR, API_ID]
    assert [call[1] for call in render.calls if call[0] == 'POST'] == [WORKER, BROKER, COORDINATOR, API_ID]


def test_exhausted_read_only_probe_stops_before_any_mutation(setup):
    release, render, _ = setup
    attempts = probe_sequence(release, [{'ok': False, 'reason': 'ssh_timeout'}] * 4)
    with pytest.raises(ReleaseError, match='coordinator probe failed.*after 3 attempt'):
        release.run()
    assert len(attempts) == 3 and not render.calls
    assert release.record['changes'] == release.record['deploys'] == []


@pytest.mark.parametrize('value', [None, [], {'ok': False}, {'ok': 'yes'},
    {'ok': False, 'reason': 'private-diagnostic'},
    {'ok': False, 'reason': 'ssh_timeout', 'extra': 'private-diagnostic'},
    {'ok': False, 'reason': 'ssh_timeout', 'stage': 'environment'},
    {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'private-diagnostic'},
    {'ok': False, 'reason': 'runtime_validation_failed', 'stage': []},
    *[{'ok': False, 'reason': reason} for reason in (
        'ssh_failed', 'runtime_validation_failed', 'database_error', 'health_unavailable', 'probe_output_invalid')],
])
def test_unclassified_or_terminal_failures_never_retry_or_expose_details(setup, value):
    release, render, _ = setup
    attempts = probe_sequence(release, [value, lambda state: state])
    with pytest.raises(ReleaseError, match='after 1 attempt'):
        release.run()
    assert len(attempts) == 1 and not render.calls
    assert 'private-diagnostic' not in release.receipt.read_text()


@pytest.mark.parametrize('stage', ['environment', 'configuration', 'snapshot', 'contracts', 'health_identity'])
def test_fixed_validation_stage_is_reported_without_retry(setup, stage):
    release, render, _ = setup
    attempts = probe_sequence(release, [{'ok': False, 'reason': 'runtime_validation_failed', 'stage': stage}])
    with pytest.raises(ReleaseError, match=f'runtime_validation_failed:{stage}'):
        release.run()
    assert len(attempts) == 1 and not render.calls
    assert release.record['probe_failures'] == [
        {'role': 'coordinator', 'reason': 'runtime_validation_failed', 'stage': stage, 'attempt': 1}]


@pytest.mark.parametrize('change', ['policy', 'contracts', 'schema', 'owners', 'identity', 'ready', 'schema_support'])
def test_recovered_probe_drift_is_fatal_on_that_sample(setup, change):
    release, render, _ = setup
    def changed(state):
        if change == 'policy': state['policy'] = '0' * 64
        elif change == 'contracts': state['contracts']['worker'] = '0' * 64
        elif change == 'schema': state['schema'] += 1
        elif change == 'owners': state['owners'].append(state['owners'][0])
        elif change == 'identity': state['identity'] = '77@external'
        elif change == 'ready': state['ready'] = False
        else: state['schema_support'] = {'protocol': 1, 'required': 999, 'compatibility': 999}
        return state
    attempts = probe_sequence(release, [{'ok': False, 'reason': 'ssh_timeout'}, changed, lambda state: state])
    with pytest.raises(ReleaseError): release.run()
    assert len(attempts) == 2 and not render.calls


def test_dashboard_change_during_retry_is_checked_before_another_probe_or_write(setup):
    release, render, _ = setup
    attempts = probe_sequence(release, [{'ok': False, 'reason': 'ssh_timeout'}, lambda state: state])
    sleep = release.sleep
    def edited(seconds):
        sleep(seconds)
        render.env[BROKER]['ENCRYPTION_KEY'] = 'private-external-edit'
    release.sleep = edited
    with pytest.raises(ReleaseError, match='environment changed outside'):
        release.run()
    assert len(attempts) == 1 and not render.calls
    assert 'private-external-edit' not in release.receipt.read_text()


def test_unverified_probe_is_not_reported_as_confirmed_cluster_drift(setup):
    release, _, _ = setup
    with pytest.raises(ReleaseError, match='probe did not return verified state'):
        release.check_state({'ok': False})


@pytest.mark.parametrize('sid', [WORKER, COORDINATOR])
@pytest.mark.parametrize('command', [None, '', WORKSPACE_IMAGE_PREDEPLOY])
def test_optional_image_build_hook_is_preserved_through_release(setup, sid, command):
    release, render, _ = setup
    render.metadata[sid]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = command
    original = deepcopy(render.metadata)
    release.run()
    assert release.record['status'] == 'success'
    assert render.metadata == original
    assert [call[1] for call in render.calls if call[0] == 'POST'] == [WORKER, BROKER, COORDINATOR, API_ID]


@pytest.mark.parametrize('sid', [API_ID, BROKER])
def test_image_build_hook_is_not_allowed_on_api_or_broker(setup, sid):
    release, render, _ = setup
    render.metadata[sid]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = WORKSPACE_IMAGE_PREDEPLOY
    with pytest.raises(ReleaseError, match='approved worker/coordinator image-build hook'):
        release.run()
    assert render.calls == []


@pytest.mark.parametrize('sid', [WORKER, COORDINATOR])
@pytest.mark.parametrize('command', [
    '/app/.venv/bin/python -m app.schema_migrations --apply',
    'python /app/build_workspace_image.py',
    WORKSPACE_IMAGE_PREDEPLOY + ' --extra',
    WORKSPACE_IMAGE_PREDEPLOY + '; migrate',
])
def test_other_image_build_commands_fail_before_any_write(setup, sid, command):
    release, render, _ = setup
    render.metadata[sid]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = command
    with pytest.raises(ReleaseError, match='approved worker/coordinator image-build hook'):
        release.run()
    assert render.calls == []


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


@pytest.mark.parametrize('change', ['environment', 'topology', 'predeploy', 'owner', 'policy', 'contract', 'unexpected_deploy'])
def test_external_changes_stop_before_next_mutation(setup, change):
    release, render, _ = setup
    call = render.call
    def edited(method, path, body=None):
        result = call(method, path, body)
        if method == 'PUT':
            if change == 'environment': render.env[BROKER]['ENCRYPTION_KEY'] = 'changed'
            elif change == 'topology': render.metadata[BROKER]['serviceDetails']['numInstances'] = 2
            elif change == 'predeploy': render.metadata[WORKER]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = None
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


@pytest.fixture
def online(setup):
    from app.online_migrations import digest
    from app.schema_migrations import SCHEMA_REVISION
    release, render, github = setup
    current, target = SCHEMA_REVISION, SCHEMA_REVISION + 1
    support = {'protocol': 1, 'required': current, 'compatibility': current}
    render.contracts['schema'] = current
    render.schema_supports = {sid: deepcopy(support) for sid in render.env}
    render.replacement_support = {**support, 'required': target}
    candidate = {**deepcopy(render.contracts), 'schema': target,
                 'schema_support': deepcopy(render.replacement_support)}
    release.contract = lambda env: deepcopy(candidate)
    plan = {'from_revision': current, 'to_revision': target, 'compatibility': current,
            'steps': [{'from_revision': current, 'to_revision': target, 'compatibility': current,
                       'source_digest': 'a' * 64, 'target_digest': 'b' * 64,
                       'operations': [{'kind': 'add_column', 'table': 'sessions',
                                       'name': 'release_test_value', 'type': 'TEXT', 'nullable': True}]}]}
    plan['digest'] = digest(plan)
    release.migration_plan = lambda previous, selected: deepcopy(plan)
    calls = []

    def migrate(service, value, expected):
        assert render.calls == []
        persisted = json.loads(release.receipt.read_text())['schema_migration']
        assert persisted['status'] == 'requested' and persisted['confirmed'] is False
        assert persisted['plan'] == value
        assert service.id == COORDINATOR and expected['schema'] == current
        assert expected['identities'] == release.identities
        calls.append(deepcopy(value))
        render.contracts['schema'] = target
        return {'status': 'applied', **{key: value[key] for key in ('from_revision', 'to_revision', 'digest')}}

    release.migrate = migrate
    return release, render, github, plan, calls


def test_reviewed_schema_expansion_precedes_every_service_write(online):
    release, render, _, plan, calls = online
    release.run()
    assert calls == [plan]
    assert release.record['schema_migration'] == {
        'plan': plan, 'status': 'applied', 'confirmed': True, 'verified': True}
    assert release.record['schema_revision'] == plan['to_revision']
    assert [call[1] for call in render.calls if call[0] == 'POST'] == [WORKER, BROKER, COORDINATOR, API_ID]
    assert release.record['status'] == 'success'


def test_schema_preflight_records_full_validated_plan_without_writes(online):
    release, render, _, plan, calls = online
    release.preflight_only = True
    release.run()
    receipt = json.loads(release.receipt.read_text())
    assert receipt['status'] == 'preflight_passed'
    assert receipt['schema_migration'] == {
        'plan': plan, 'status': 'planned', 'confirmed': False, 'verified': False}
    assert calls == render.calls == []
    assert render.contracts['schema'] == plan['from_revision']


@pytest.mark.parametrize('sid', [WORKER, BROKER, COORDINATOR, API_ID])
@pytest.mark.parametrize('change', ['missing', 'protocol', 'compatibility', 'required'])
def test_every_live_source_must_support_the_expansion_before_any_write(online, sid, change):
    release, render, _, plan, calls = online
    if change == 'missing':
        del render.schema_supports[sid]
    elif change == 'protocol':
        render.schema_supports[sid]['protocol'] = 2
    elif change == 'compatibility':
        render.schema_supports[sid]['compatibility'] = plan['compatibility'] - 1
    else:
        render.schema_supports[sid]['required'] = plan['to_revision']
    with pytest.raises(ReleaseError, match='incompatible.*running source'):
        release.run()
    assert calls == render.calls == []


@pytest.mark.parametrize('change', ['gap', 'digest', 'raw_sql', 'destructive', 'required_column', 'default',
                                  'target', 'compatibility', 'different_endpoints'])
def test_invalid_or_unsafe_online_plan_never_writes(online, change):
    from app.online_migrations import digest
    release, render, _, plan, calls = online
    invalid = deepcopy(plan)
    if change == 'gap':
        invalid['steps'] = []
    elif change == 'digest':
        invalid['digest'] = '0' * 64
    elif change == 'raw_sql':
        invalid['steps'][0]['operations'] = [{'kind': 'sql', 'sql': 'DROP TABLE sessions'}]
    elif change == 'destructive':
        invalid['steps'][0]['operations'][0]['kind'] = 'drop_column'
    elif change == 'required_column':
        invalid['steps'][0]['operations'][0]['nullable'] = False
    elif change == 'default':
        invalid['steps'][0]['operations'][0]['default'] = 'value'
    elif change == 'compatibility':
        invalid['compatibility'] -= 1
        invalid['steps'][0]['compatibility'] -= 1
    elif change == 'different_endpoints':
        for value in (invalid, invalid['steps'][0]):
            value['from_revision'] += 1
            value['to_revision'] += 1
    else:
        invalid['to_revision'] += 1
    if change != 'digest':
        invalid['digest'] = digest({key: value for key, value in invalid.items() if key != 'digest'})
    release.migration_plan = lambda previous, target: invalid
    with pytest.raises(ReleaseError, match='incompatible, incomplete or unsupported'):
        release.run()
    assert calls == render.calls == []


def test_missing_reviewed_manifest_step_never_writes(online):
    release, render, _, _, calls = online
    def missing(previous, target):
        raise ValueError('No reviewed manifest step')
    release.migration_plan = missing
    with pytest.raises(ReleaseError, match='incompatible, incomplete or unsupported'):
        release.run()
    assert calls == render.calls == []


def test_no_migration_transport_fails_before_service_writes(online):
    release, render, _, _, calls = online
    release.migrate = None
    with pytest.raises(ReleaseError, match='transport is unavailable'):
        release.run()
    assert calls == render.calls == []


@pytest.mark.parametrize('change', ['schema', 'identity', 'policy', 'contract', 'capability', 'environment'])
def test_last_baseline_check_stops_drift_before_migration(online, change):
    release, render, _, plan, calls = online
    def plan_after_drift(previous, target):
        if change == 'schema': render.contracts['schema'] += 1
        elif change == 'identity': render.owners[WORKER] = ['99@foreign']
        elif change == 'policy': render.policy = '0' * 64
        elif change == 'contract': render.contracts['contracts']['worker'] = '0' * 64
        elif change == 'capability': render.schema_supports[WORKER]['protocol'] = 2
        else: render.env[WORKER]['ENCRYPTION_KEY'] = 'changed'
        return deepcopy(plan)
    release.migration_plan = plan_after_drift
    with pytest.raises(ReleaseError): release.run()
    assert calls == render.calls == []


def test_same_schema_capability_bridge_accepts_legacy_sources_without_migration(online):
    release, render, _, plan, calls = online
    render.schema_supports.clear()
    render.replacement_support['required'] = plan['from_revision']
    candidate = {**deepcopy(render.contracts), 'schema_support': deepcopy(render.replacement_support)}
    release.contract = lambda env: deepcopy(candidate)
    release.migration_plan = lambda *args: pytest.fail('Same-schema bridge must not plan a migration')
    release.run()
    assert calls == [] and release.record['schema_migration'] is None
    assert release.record['status'] == 'success'
    assert len([call for call in render.calls if call[0] == 'POST']) == 4


@pytest.mark.parametrize('change', ['schema', 'identity', 'policy', 'contract', 'capability', 'readiness', 'environment'])
def test_migration_refresh_rejects_foreign_drift_before_deployment(online, change):
    release, render, _, _, calls = online
    original = release.migrate
    def drift(*args):
        receipt = original(*args)
        if change == 'schema': render.contracts['schema'] += 1
        elif change == 'identity': render.owners[WORKER] = ['99@foreign']
        elif change == 'policy': render.policy = '0' * 64
        elif change == 'contract': render.contracts['contracts']['worker'] = '0' * 64
        elif change == 'capability': render.schema_supports[WORKER]['protocol'] = 2
        elif change == 'readiness': render.ready = False
        else: render.env[WORKER]['ENCRYPTION_KEY'] = 'changed'
        return receipt
    release.migrate = drift
    with pytest.raises(ReleaseError): release.run()
    assert len(calls) == 1 and render.calls == []
    assert release.record['schema_migration']['confirmed'] is True
    assert release.record['schema_migration']['verified'] is False


def test_schema_drift_after_migration_stops_before_deployment(online):
    release, render, _, _, calls = online
    original = render.call
    def drift(method, path, body=None):
        result = original(method, path, body)
        if method == 'PUT': render.contracts['schema'] += 1
        return result
    render.call = drift
    with pytest.raises(ReleaseError, match='schema or compatibility contracts changed'):
        release.run()
    assert len(calls) == 1
    assert not [call for call in render.calls if call[0] == 'POST']


@pytest.mark.parametrize('failure', ['lost_response', 'interrupt', 'wrong_receipt'])
def test_uncertain_migration_is_never_retried_or_rolled_back(online, failure):
    release, render, _, plan, calls = online
    original = release.migrate
    def interrupted(*args):
        receipt = original(*args)
        if failure == 'interrupt': raise KeyboardInterrupt()
        if failure == 'lost_response': raise ReleaseError('Synthetic lost migration response')
        return {**receipt, 'digest': '0' * 64}
    release.migrate = interrupted
    with pytest.raises(KeyboardInterrupt if failure == 'interrupt' else ReleaseError):
        release.run()
    assert calls == [plan] and render.calls == []
    assert render.contracts['schema'] == plan['to_revision']
    assert release.record['schema_migration']['status'] == 'requested'
    assert release.record['schema_migration']['confirmed'] is False


@pytest.mark.parametrize('malformed', ['extra', 'missing', 'status_type'])
def test_malformed_migration_receipt_stops_before_deployment(online, malformed):
    release, render, _, plan, calls = online
    original = release.migrate
    def corrupt(*args):
        receipt = original(*args)
        if malformed == 'extra': receipt['details'] = 'unexpected-private-data'
        elif malformed == 'missing': del receipt['from_revision']
        else: receipt['status'] = []
        return receipt
    release.migrate = corrupt
    with pytest.raises(ReleaseError, match='incomplete or mismatched'):
        release.run()
    assert calls == [plan] and render.calls == []
    assert release.record['schema_migration']['confirmed'] is False
    assert 'unexpected-private-data' not in release.receipt.read_text()


def test_new_run_observes_committed_migration_without_replaying_it(online, tmp_path):
    release, render, github, plan, calls = online
    original = release.migrate
    def lost(*args):
        original(*args)
        raise ReleaseError('Synthetic lost migration response')
    release.migrate = lost
    with pytest.raises(ReleaseError): release.run()
    recovery = RollingRelease(render, github, render.probe, tmp_path / 'recovery.json',
        api_service_id=API_ID, contract=release.contract,
        migration_plan=lambda *args: pytest.fail('Committed target must not be planned again'),
        migrate=lost, wait_seconds=1, clock=release.clock, sleep=release.sleep)
    recovery.run()
    assert calls == [plan] and recovery.record['schema_migration'] is None
    assert recovery.record['status'] == 'success'


def test_partial_compatible_release_at_target_schema_resumes_remaining_roles(online):
    release, render, _, plan, calls = online
    render.contracts['schema'] = plan['to_revision']
    for sid in (WORKER, BROKER):
        render.env[sid]['MOYAI_BUILD_SHA'] = NEW
        render.live[sid]['commit']['id'] = NEW
        render.schema_supports[sid] = deepcopy(render.replacement_support)
    release.run()
    assert calls == []
    assert [call[1] for call in render.calls if call[0] == 'POST'] == [COORDINATOR, API_ID]


def test_new_probe_allows_only_exact_schema_on_legacy_source(monkeypatch):
    import builtins
    from app.schema_migrations import SCHEMA_REVISION
    from scripts.release.rolling_probe import schema_capability
    original = builtins.__import__
    def legacy(name, *args, **kwargs):
        if name == 'app.online_migrations':
            raise ModuleNotFoundError('Legacy image has no migration engine', name=name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', legacy)
    assert schema_capability(object(), 'tenant', SCHEMA_REVISION) == {}
    with pytest.raises(ValueError): schema_capability(object(), 'tenant', SCHEMA_REVISION + 1)


def test_missing_engine_dependency_is_not_treated_as_legacy_support(monkeypatch):
    import builtins
    from scripts.release.rolling_probe import schema_capability
    original = builtins.__import__
    def broken(name, *args, **kwargs):
        if name == 'app.online_migrations':
            raise ModuleNotFoundError('An installed engine dependency is absent', name='engine_dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', broken)
    with pytest.raises(ModuleNotFoundError): schema_capability(object(), 'tenant', 1)


@pytest.mark.parametrize('compatible', [False, True])
def test_running_probe_uses_installed_source_compatibility_admission(monkeypatch, compatible):
    import sys
    from types import SimpleNamespace
    from app.schema_migrations import SCHEMA_REVISION
    from scripts.release.rolling_probe import schema_capability
    support = {'protocol': 1, 'required': SCHEMA_REVISION, 'compatibility': SCHEMA_REVISION}
    connection, calls = object(), []
    def verify(*args):
        calls.append(args)
        return compatible
    monkeypatch.setitem(sys.modules, 'app.online_migrations', SimpleNamespace(
        schema_support=lambda: support, schema_is_compatible=verify))
    if compatible:
        assert schema_capability(connection, 'tenant', SCHEMA_REVISION + 1) == {'schema_support': support}
    else:
        with pytest.raises(ValueError): schema_capability(connection, 'tenant', SCHEMA_REVISION + 1)
    assert calls == [(connection, 'tenant', SCHEMA_REVISION, SCHEMA_REVISION)]
