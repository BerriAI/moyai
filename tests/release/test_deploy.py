"""Release failure/recovery boundaries with an in-memory Render control plane."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from scripts.release.deploy import COORDINATOR, WORKER, GitHub, Release, ReleaseError, MUTABLE

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

    def call(self, method, path):
        if 'docker.yml' in path:
            return {'workflow_runs': [self.docker] if self.docker else []}
        if 'check-runs' in path:
            return {'total_count': len(self.checks), 'check_runs': self.checks}
        return self.status


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
