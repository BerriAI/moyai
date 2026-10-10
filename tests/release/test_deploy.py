"""Release failure/recovery boundaries with an in-memory Render control plane."""
from copy import deepcopy
from pathlib import Path
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, GitHub, Release, ReleaseError, MUTABLE

from scripts.release.rehearse import OLD, NEW, Render, Candidate


@pytest.fixture
def setup(tmp_path):
    render, github = Render(), Candidate()
    elapsed = [0]
    def sleep(seconds):
        elapsed[0] += seconds
    release = Release(render, github, render.probe, tmp_path / 'receipt.json', wait_seconds=1,
                      drain_timeout=3, deploy_timeout=3, clock=lambda: elapsed[0], sleep=sleep)
    return release, render, github


@pytest.fixture
def split_setup(setup):
    release, _, github = setup
    render = Render(split=True)
    release.render, release.probe = render, render.probe
    return release, render, github


def test_split_release_stages_old_broker_and_verifies_new_broker_before_execution(split_setup):
    release, render, _ = split_setup
    original = deepcopy(render.env)
    render.owner_delay, render.broker_owner_delay = 3, 3
    release.run()
    assert [call for call in render.calls if call[0] == 'POST'] == [
        ('POST', WORKER, OLD, 'false'), ('POST', WORKER, NEW, 'true'),
        ('POST', BROKER, NEW, 'true'), ('POST', COORDINATOR, NEW, 'false'),
        ('POST', BROKER, NEW, 'false'), ('POST', WORKER, NEW, 'false')]
    assert render.owner_delay == render.broker_owner_delay == 0
    assert release.record['topology'] == 'separate_broker'
    for sid in original:
        assert render.env[sid] == {**original[sid], 'MOYAI_BUILD_SHA': NEW}


@pytest.mark.parametrize('brokers', [None, False, 0, 2])
def test_split_preflight_requires_exact_broker_ownership(split_setup, brokers):
    release, render, _ = split_setup
    probe = render.probe
    release.probe = lambda service: {**probe(service), 'brokers': brokers}
    with pytest.raises(ReleaseError, match='Preflight'):
        release.run()
    assert render.calls == []


@pytest.mark.parametrize('key,value', [
    ('MOYAI_SEPARATE_BROKER', 'false'), ('ENCRYPTION_KEY', 'different'),
    ('MOYAI_BUILD_SHA', NEW), ('RENDER_MIGRATION_STAGE', 'true'),
    ('MAX_CONCURRENT_MODEL_REQUESTS', '64')])
def test_split_preflight_refuses_inconsistent_broker_without_writes(split_setup, key, value):
    release, render, _ = split_setup
    render.env[BROKER][key] = value
    with pytest.raises(ReleaseError):
        release.run()
    assert render.calls == []


@pytest.mark.parametrize('service', [WORKER, BROKER])
@pytest.mark.parametrize('key,value', [('SANDBOX_PREPARED_POOL_SIZE', '1'), ('SANDBOX_PREPARED_IDLE_SECONDS', '600')])
def test_prepared_pool_drift_is_rejected_before_deployment(split_setup, service, key, value):
    release, render, _ = split_setup
    render.env[service][key] = value
    with pytest.raises(ReleaseError, match='same build and runtime configuration'):
        release.run()
    assert render.calls == []


def test_explicit_prepared_pool_defaults_match_omitted_settings(split_setup):
    release, render, _ = split_setup
    render.env[WORKER].update(SANDBOX_PREPARED_POOL_SIZE='0', SANDBOX_PREPARED_IDLE_SECONDS='300')
    release.preflight_only = True
    release.run()
    assert release.record['status'] == 'preflight_passed'
    assert render.calls == []


@pytest.mark.parametrize('counter', ['owners', 'brokers', 'model_requests', 'live_leases'])
def test_old_broker_must_relinquish_ownership_and_requests_before_app_update(split_setup, counter):
    release, render, _ = split_setup
    probe = render.probe
    def stale_broker(service):
        state = probe(service)
        if service.id == COORDINATOR and not render.broker_running:
            state[counter] += 1
        return state
    release.probe = stale_broker
    with pytest.raises(ReleaseError, match='Old broker ownership did not clear'):
        release.run()
    assert render.live[COORDINATOR]['commit']['id'] == OLD
    assert not render.worker_running
    assert not any(call[1] == COORDINATOR for call in render.calls)


@pytest.mark.parametrize('activation', [False, True])
def test_broker_deployment_failure_never_resumes_worker(split_setup, activation):
    release, render, _ = split_setup
    call = render.call
    def fail_broker(method, path, body=None):
        if method == 'POST' and BROKER in path:
            if (render.env[BROKER]['RENDER_MIGRATION_STAGE'] == 'false') == activation:
                render.fail_service = BROKER
        return call(method, path, body)
    render.call = fail_broker
    with pytest.raises(ReleaseError, match='Render deployment failed'):
        release.run()
    assert not render.worker_running
    assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
    assert render.live[COORDINATOR]['commit']['id'] == (NEW if activation else OLD)
    assert [call for call in render.calls if call[0] == 'POST' and call[1] == WORKER] == [
        ('POST', WORKER, OLD, 'false'), ('POST', WORKER, NEW, 'true')]


def test_broker_http_readiness_is_required_even_when_render_deploy_is_live(split_setup):
    release, render, _ = split_setup
    probe = render.probe
    def unready_broker(service):
        if service.id == BROKER and service.sha == NEW and not service.probe_options()['staged']:
            return {'ok': False}
        return probe(service)
    release.probe = unready_broker
    with pytest.raises(ReleaseError, match='runtime checks'):
        release.run()
    assert render.live[BROKER]['commit']['id'] == NEW
    assert not render.worker_running


@pytest.mark.parametrize('counter', ['owners', 'brokers', 'model_requests', 'live_leases'])
def test_new_broker_aggregate_fence_blocks_worker_resume_even_with_healthy_http(split_setup, counter):
    release, render, _ = split_setup
    probe = render.probe
    def inconsistent_ownership(service):
        state = probe(service)
        if render.broker_running and not render.worker_running and render.live[BROKER]['commit']['id'] == NEW:
            state[counter] += 1
            assert state['ok'] is True
        return state
    release.probe = inconsistent_ownership
    with pytest.raises(ReleaseError, match='Broker runtime checks failed'):
        release.run()
    assert render.live[BROKER]['commit']['id'] == NEW
    assert render.live[COORDINATOR]['commit']['id'] == NEW
    assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
    assert not render.worker_running
    assert [call for call in render.calls if call[0] == 'POST' and call[1] == WORKER] == [
        ('POST', WORKER, OLD, 'false'), ('POST', WORKER, NEW, 'true')]


def test_broker_dashboard_edits_stop_release_without_overwriting_them(split_setup):
    release, render, _ = split_setup
    probe = render.probe
    def changed_broker(service):
        state = probe(service)
        if service.id == WORKER and render.draining_seen:
            render.env[BROKER]['MAX_CONCURRENT_MODEL_REQUESTS'] = '64'
        return state
    release.probe = changed_broker
    with pytest.raises(ReleaseError, match='environment changed outside'):
        release.run()
    assert render.env[BROKER]['MAX_CONCURRENT_MODEL_REQUESTS'] == '64'
    assert not any(call[1] == BROKER for call in render.calls)


@pytest.mark.parametrize('preflight', [False, True])
def test_split_noop_and_preflight_never_redeploy_services(split_setup, preflight):
    release, render, github = split_setup
    release.preflight_only = preflight
    if not preflight:
        github.sha = OLD
    release.run()
    assert render.calls == []
    assert release.record['status'] == ('preflight_passed' if preflight else 'success')


def test_legacy_combined_broker_without_separate_lock_can_upgrade(setup):
    release, render, _ = setup
    probe = render.probe
    def legacy_lock(service):
        state = probe(service)
        if service.sha == OLD and 'brokers' in state:
            state['brokers'] = 0
        return state
    release.probe = legacy_lock
    release.run()
    assert release.record['status'] == 'success'


def test_release_pins_one_commit_and_orders_all_four_deployments(setup):
    release, render, github = setup
    original = deepcopy(render.env)
    render.owner_delay = 3
    release.run()
    posts = [call for call in render.calls if call[0] == 'POST']
    assert posts == [('POST', WORKER, OLD, 'false'), ('POST', WORKER, NEW, 'true'),
                     ('POST', COORDINATOR, NEW, 'false'), ('POST', WORKER, NEW, 'false')]
    assert github.resolutions == 1
    assert render.owner_delay == 0  # Even Render's live marker cannot bypass old ownership.
    for sid in (COORDINATOR, WORKER):
        assert render.env[sid] == {**original[sid], 'MOYAI_BUILD_SHA': NEW}
    text = release.receipt.read_text()
    assert all(secret not in text for secret in ('do-not-log-session', 'do-not-log-encryption', 'postgresql://'))
    assert json.loads(text)['status'] == 'success'


@pytest.mark.parametrize('counter', ['unsafe', 'models'])
def test_drain_timeout_never_stages_worker_or_touches_coordinator(setup, counter):
    release, render, _ = setup
    setattr(render, counter, 1)
    with pytest.raises(ReleaseError, match='Drain timed out'):
        release.run()
    assert [call[1] for call in render.calls] == [WORKER, WORKER]
    assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
    assert render.env[COORDINATOR]['MOYAI_BUILD_SHA'] == OLD
    assert json.loads(release.receipt.read_text())['status'] == 'failed'


def test_failed_coordinator_keeps_worker_staged_and_never_activates_it(setup):
    release, render, _ = setup
    render.fail_service = COORDINATOR
    with pytest.raises(ReleaseError, match='Render deployment failed'):
        release.run()
    assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
    assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
    assert not render.worker_running
    assert [c[1] for c in render.calls if c[0] == 'POST'] == [WORKER, WORKER, COORDINATOR]


def test_stop_requested_after_worker_staging_does_not_block_release(setup):
    release, render, _ = setup
    probe = render.probe
    render.owner_delay = 3
    staged_snapshots = []
    def stop_after_staging(service):
        state = probe(service)
        if not render.worker_running:
            # The coordinator accepts Stop while no worker can finish it.
            render.unsafe = 1
            if service.id == COORDINATOR:
                state['unsafe_sessions'] = 1
                staged_snapshots.append(state)
        return state
    release.probe = stop_after_staging
    release.run()
    assert release.record['status'] == 'success'
    assert staged_snapshots and all(s['unsafe_sessions'] == 1 for s in staged_snapshots)
    assert render.owner_delay == 0  # A late Stop must not bypass old-worker ownership.
    assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'false'
    assert render.env[COORDINATOR]['MOYAI_BUILD_SHA'] == NEW


@pytest.mark.parametrize('counter', ['owners', 'model_requests', 'live_leases'])
def test_staged_wait_still_blocks_active_execution(setup, counter):
    release, render, _ = setup
    probe = render.probe
    def still_active(service):
        state = probe(service)
        if not render.worker_running and service.id == COORDINATOR:
            state[counter] += 1
            state['unsafe_sessions'] = 1
        return state
    release.probe = still_active
    with pytest.raises(ReleaseError, match='Old worker ownership did not clear'):
        release.run()
    assert render.env[COORDINATOR]['MOYAI_BUILD_SHA'] == OLD
    assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
    assert all(c[1] == WORKER for c in render.calls)


def test_ambiguous_post_is_recorded_and_never_retried(setup):
    release, render, _ = setup
    render.unknown_write = True
    with pytest.raises(ReleaseError, match='may have been accepted'):
        release.run()
    assert len([c for c in render.calls if c[0] == 'POST']) == 1
    receipt = json.loads(release.receipt.read_text())
    assert receipt['deploys'][0]['status'] == 'requested'
    assert receipt['deploys'][0]['id'] is None


def test_external_environment_change_is_not_overwritten(setup):
    release, render, _ = setup
    render.external_change = True
    with pytest.raises(ReleaseError, match='environment changed outside'):
        release.run()
    assert render.env[COORDINATOR]['MAX_CONCURRENT_MODEL_REQUESTS'] == '64'
    assert render.env[COORDINATOR]['MOYAI_BUILD_SHA'] == OLD


@pytest.mark.parametrize('bad', ['ssh', 'auto', 'split', 'drained', 'build', 'keys'])
def test_preflight_rejects_unsafe_baselines_without_mutation(setup, bad):
    release, render, _ = setup
    if bad == 'ssh': render.probe_ok = False
    if bad == 'auto': render.auto = 'commit'
    if bad == 'split': render.env[COORDINATOR]['MOYAI_SEPARATE_BROKER'] = 'true'
    if bad == 'drained': render.env[WORKER]['MAINTENANCE_DRAIN'] = 'true'
    if bad == 'build': render.env[WORKER]['MOYAI_BUILD_SHA'] = NEW
    if bad == 'keys': render.env[WORKER]['ENCRYPTION_KEY'] = 'different'
    with pytest.raises(ReleaseError):
        release.run()
    assert render.calls == []


def test_already_live_is_a_verified_noop(setup):
    release, render, github = setup
    github.sha = OLD
    release.run()
    assert release.record['status'] == 'success'
    assert render.calls == []


def test_read_only_preflight_verifies_new_release_without_mutations(setup):
    release, render, _ = setup
    release.preflight_only = True
    release.run()
    assert release.record['status'] == 'preflight_passed'
    assert release.record['commit'] == NEW
    assert render.calls == []


def test_dispatch_sha_is_pinned_without_resolving_moving_main():
    class Offline:
        def call(self, *args):
            pytest.fail('Candidate selection must not resolve main again')
    assert GitHub(Offline(), '123', NEW).candidate() == NEW
    with pytest.raises(ReleaseError, match='full commit SHA'):
        GitHub(Offline(), '123', 'main').candidate()


def test_stale_queued_release_cannot_roll_production_back(setup):
    release, render, _ = setup
    class OlderCommitAPI:
        def call(self, method, path):
            assert '/compare/' in path
            return {'status': 'behind'}
    release.github.forward_from = GitHub(OlderCommitAPI(), '123', NEW).forward_from
    with pytest.raises(ReleaseError, match='older than'):
        release.run()
    assert render.calls == []


def test_ambiguous_environment_write_preserves_intent_without_retry(setup):
    release, render, _ = setup
    call = render.call
    def lose_response(method, path, body=None):
        result = call(method, path, body)
        if method == 'PUT':
            raise ReleaseError('Write response lost')
        return result
    render.call = lose_response
    with pytest.raises(ReleaseError, match='response lost'):
        release.run()
    assert len(render.calls) == 1
    assert release.record['changes'] == [{'service': WORKER, 'key': 'MAINTENANCE_DRAIN',
                                        'value': 'true', 'confirmed': False}]


@pytest.mark.parametrize('status', ['build_failed', 'pre_deploy_failed', 'update_failed', 'canceled', 'deactivated', 'unknown'])
def test_failed_or_unknown_deployment_never_advances(setup, status):
    release, render, _ = setup
    call = render.call
    def failed_state(method, path, body=None):
        result = call(method, path, body)
        if method == 'GET' and '/deploys/' in path:
            result['status'] = status
        return result
    render.call = failed_state
    with pytest.raises(ReleaseError):
        release.run()
    assert [c[1] for c in render.calls if c[0] == 'POST'] == [WORKER]


def test_live_but_unhealthy_worker_stops_before_coordinator(setup):
    release, render, _ = setup
    probe = render.probe
    release.probe = lambda service: {'ok': False} if render.calls else probe(service)
    with pytest.raises(ReleaseError, match='runtime checks'):
        release.run()
    assert [c[1] for c in render.calls if c[0] == 'POST'] == [WORKER]


def test_cancellation_preserves_receipt_and_does_not_unpause(setup):
    release, render, _ = setup
    probe = render.probe
    def interrupted(service):
        if render.calls:
            raise KeyboardInterrupt
        return probe(service)
    release.probe = interrupted
    with pytest.raises(KeyboardInterrupt):
        release.run()
    assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
    assert release.record['status'] == 'failed'


def test_unknown_probe_shape_never_passes():
    assert not Release.settled({'ok': True}, 2)
    assert not Release.settled({'ok': True, 'owners': True, 'coordinators': 1,
        'unsafe_sessions': 0, 'legacy_active_sessions': 0, 'model_requests': 0, 'live_leases': 0}, 1)


class CheckAPI:
    def __init__(self):
        self.docker = {'id': 9, 'status': 'completed', 'conclusion': 'success'}
        self.checks = [{'status': 'completed', 'conclusion': 'success', 'details_url': 'https://github.com/job'}]
        self.status = {'total_count': 0, 'state': 'pending'}
        self.deployments = [{'id': 123, 'check_suite_id': 456, 'head_sha': NEW, 'head_branch': 'main',
                             'event': 'workflow_dispatch', 'path': '.github/workflows/deploy-production.yml'}]
        self.deployment_pages = []

    def call(self, method, path):
        if 'docker.yml' in path:
            return {'workflow_runs': [self.docker] if self.docker else []}
        if 'deploy-production.yml' in path:
            query = parse_qs(urlsplit(path).query)
            assert query['head_sha'] == [NEW] and query['event'] == ['workflow_dispatch']
            page = int(query['page'][0])
            self.deployment_pages.append(page)
            return {'workflow_runs': self.deployments[(page - 1) * 100:page * 100]}
        if 'check-runs' in path:
            return {'total_count': len(self.checks), 'check_runs': self.checks}
        return self.status


class PaginatedChecks(CheckAPI):
    def __init__(self, count):
        super().__init__()
        self.checks = [{**self.checks[0], 'name': f'check-{i}'} for i in range(count)]
        self.pages = []

    def call(self, method, path):
        if 'check-runs' not in path:
            return super().call(method, path)
        page = int(parse_qs(urlsplit(path).query)['page'][0])
        self.pages.append(page)
        # Old reruns are counted even though filter=latest omits them.
        return {'total_count': 5000, 'check_runs': self.checks[(page - 1) * 100:page * 100]}


@pytest.mark.parametrize('count', [0, 1, 100, 101])
def test_ci_pagination_finishes_despite_historical_check_count(count):
    api = PaginatedChecks(count)
    github = GitHub(api, '123', NEW)
    assert github.ready(NEW)
    assert api.pages == list(range(1, count // 100 + 2))
    api.pages.clear()
    assert github.ready(NEW)  # Same-SHA retry must work too.


@pytest.mark.parametrize('status,conclusion', [('completed', 'failure'), ('in_progress', None)])
def test_ci_pagination_includes_checks_on_later_pages(status, conclusion):
    api = PaginatedChecks(101)
    api.checks[-1].update(status=status, conclusion=conclusion)
    github = GitHub(api, '123', NEW)
    if status == 'completed':
        with pytest.raises(ReleaseError, match='A check failed'):
            github.ready(NEW)
    else:
        assert not github.ready(NEW)
    assert api.pages == [1, 2]


def test_ci_pagination_refuses_inventory_over_its_bound():
    api = PaginatedChecks(2001)
    with pytest.raises(ReleaseError, match='inventory was incomplete'):
        GitHub(api, '123', NEW).ready(NEW)
    assert len(api.pages) == 20


@pytest.mark.parametrize('status,conclusion', [('completed', 'failure'), ('completed', 'cancelled'), ('in_progress', None)])
def test_ci_ignores_verified_previous_deployment_attempts(status, conclusion):
    api = CheckAPI()
    api.checks.append({'name': 'deploy-production', 'status': status, 'conclusion': conclusion,
                       'check_suite': {'id': 456},
                       'details_url': 'https://github.com/BerriAI/moyai/actions/runs/123/job/456'})
    assert GitHub(api, '789', NEW).ready(NEW)
    # A real test failure still blocks the release, even with a deployment name
    # and a details URL pointing at the current deployment run.
    api.checks.append({**api.checks[-1], 'status': 'completed', 'conclusion': 'failure',
                       'check_suite': {'id': 999},
                       'details_url': 'https://github.com/BerriAI/moyai/actions/runs/789/job/456'})
    with pytest.raises(ReleaseError, match='A check failed'):
        GitHub(api, '789', NEW).ready(NEW)


@pytest.mark.parametrize('key,value', [('head_sha', OLD), ('head_branch', 'feature'),
                                     ('event', 'push'), ('path', '.github/workflows/docker.yml')])
def test_ci_never_excludes_checks_from_another_workflow_or_commit(key, value):
    api = CheckAPI()
    api.deployments[0][key] = value
    api.checks = [{'status': 'completed', 'conclusion': 'failure', 'check_suite': {'id': 456}}]
    with pytest.raises(ReleaseError, match='A check failed'):
        GitHub(api, '789', NEW).ready(NEW)


@pytest.mark.parametrize('suite_id', [None, True, 0])
def test_ci_refuses_missing_or_invalid_deployment_suite_identity(suite_id):
    api = CheckAPI()
    api.deployments[0]['check_suite_id'] = suite_id
    with pytest.raises(ReleaseError, match='deployment check inventory was incomplete'):
        GitHub(api, '789', NEW).ready(NEW)


def test_ci_paginates_previous_deployments_before_excluding_their_checks():
    api = CheckAPI()
    api.deployments = [{**api.deployments[0], 'id': i, 'check_suite_id': i + 1} for i in range(101)]
    api.checks = [{'status': 'completed', 'conclusion': 'failure', 'check_suite': {'id': 101}}]
    assert GitHub(api, '789', NEW).ready(NEW)
    assert api.deployment_pages == [1, 2]
    api.deployments *= 20
    with pytest.raises(ReleaseError, match='deployment check inventory was incomplete'):
        GitHub(api, '789', NEW).ready(NEW)


def test_ci_requires_docker_and_every_reported_check():
    api = CheckAPI()
    github = GitHub(api, '123', NEW)
    assert github.ready(NEW)
    api.docker = None
    assert not github.ready(NEW)
    api.docker = {'id': 9, 'status': 'completed', 'conclusion': 'failure'}
    with pytest.raises(ReleaseError, match='Docker'):
        github.ready(NEW)
    api.docker['conclusion'] = 'success'
    api.checks[0]['conclusion'] = 'failure'
    with pytest.raises(ReleaseError, match='check failed'):
        github.ready(NEW)
    api.checks[0] = {'status': 'in_progress', 'conclusion': None, 'details_url': 'https://github.com/job'}
    assert not github.ready(NEW)
    api.checks[0]['details_url'] = 'https://github.com/BerriAI/moyai/actions/runs/123/job/456'
    assert not github.ready(NEW)  # A details URL alone does not identify our check.
    api.checks[0]['check_suite'] = {'id': 456}
    assert github.ready(NEW)
    api.status = {'total_count': 1, 'state': 'failure'}
    with pytest.raises(ReleaseError, match='commit status'):
        github.ready(NEW)


def test_workflow_is_manual_serialized_main_only_and_keeps_secrets_scoped():
    workflow = Path('.github/workflows/deploy-production.yml').read_text()
    assert 'workflow_dispatch:' in workflow and 'push:' not in workflow and 'pull_request:' not in workflow
    assert 'cancel-in-progress: false' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert 'environment: moyai-production' in workflow
    assert 'if: always()' in workflow and 'release-receipt.json' in workflow
