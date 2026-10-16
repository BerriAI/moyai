"""Controller fault injection; actual application qualification is separate."""
from copy import deepcopy

import pytest
from test_native_cutover import configure_native_source
from test_native_cutover import cutover as cutover_fixture

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.rehearse_api import API_ID

cutover = cutover_fixture


@pytest.fixture
def concurrent(cutover):
    release, render = configure_native_source(cutover)
    original_call, original_probe = render.call, release.probe
    pending, events = {}, []
    release.deploy_timeout = release.readiness_timeout = release.retirement_timeout = 12

    def call(method, path, body=None):
        sid = path.split('/')[2]
        if method == 'POST' and release.batch:
            previous = {name: deepcopy(getattr(render, name)[sid])
                        for name in ('live', 'installed', 'owners')}
            previous_retirement = deepcopy(render.retirements.get(sid))
            result = original_call(method, path, body)
            if result['status'] != 'live':
                return result
            replacement = {name: deepcopy(getattr(render, name)[sid])
                           for name in ('live', 'installed', 'owners')}
            retiring = deepcopy(render.retirements[sid])
            for name, value in previous.items():
                getattr(render, name)[sid] = value
            if previous_retirement is None:
                render.retirements.pop(sid, None)
            else:
                render.retirements[sid] = previous_retirement
            # Worker finishes last; other roles must remain accounted for while
            # their live/retirement observations arrive in a different order.
            pending[result['id']] = {
                'sid': sid, 'replacement': replacement, 'retiring': retiring,
                'polls': 3 if sid == WORKER else 1,
            }
            result['status'] = render.deployments[result['id']]['status'] = 'build_in_progress'
            events.append(('requested', sid, len(render.history)))
            return result
        identity = path.rsplit('/', 1)[-1]
        if method == 'GET' and identity in pending:
            item = pending[identity]
            item['polls'] -= 1
            if item['polls'] == 0:
                for name, value in item['replacement'].items():
                    getattr(render, name)[sid] = value
                render.retirements[sid] = item['retiring']
                render.deployments[identity]['status'] = 'live'
                events.append(('live', sid, len(render.history)))
                del pending[identity]
        return original_call(method, path, body)

    def probe(service):
        if any(item['sid'] == service.id for item in pending.values()):
            return {'ok': False, 'reason': 'runtime_validation_failed', 'stage': 'environment'}
        return original_probe(service)

    render.call, release.probe = call, probe
    return release, render, events


def test_native_release_overlaps_independent_replacements_and_keeps_pause_until_resume(concurrent):
    release, render, events = concurrent
    render.counts['native_queued'] = 3
    render.counts['native_parked'] = 2
    original_control = release.native_control
    resumed = []

    def control(service, command, body):
        if command == 'resume':
            assert len(release.record['replacements']) == 8
            assert all(group['status'] == 'verified' for group in release.record['batches'])
            assert len({identities[0] for identities in render.owners.values()}) == 4
            assert all(len(identities) == 1 for identities in render.owners.values())
            resumed.append(True)
        return original_control(service, command, body)

    release.native_control = control
    release.run()

    assert release.record['status'] == 'success' and resumed == [True]
    assert len(render.history) == 8
    assert all(item['env']['MAINTENANCE_DRAIN'] == 'false' for item in render.history)
    assert not any(call[:1] == ('PUT',) and call[2] == 'MAINTENANCE_DRAIN' for call in render.calls)
    assert render.counts['native_queued'] == 3 and render.counts['native_parked'] == 2
    assert [item[0] for item in events[:3]] == ['requested'] * 3
    assert {item[1] for item in events[:3]} == {WORKER, BROKER, COORDINATOR}
    assert all(item[2] == 4 for item in events[3:6])
    assert [item[0] for item in events[6:8]] == ['requested', 'requested']
    assert {item[1] for item in events[6:8]} == {WORKER, API_ID}
    assert all(item[2] == 8 for item in events[8:])


@pytest.mark.parametrize('index', range(8))
def test_each_unknown_deploy_outcome_stops_without_resubmission(cutover, index):
    release, render = configure_native_source(cutover)

    def after(method, sid, body):
        if method == 'POST' and len(render.history) == index + 1:
            render.unknown_write = True

    render.after_write = after
    with pytest.raises(ReleaseError, match='lost write response'):
        release.run()
    assert len(render.history) == index + 1
    assert release.record['deploys'][-1]['id'] is None
    assert release.record['deploys'][-1]['status'] == 'requested'
    assert [item['command'] for item in release.record['native_control']] == ['request']
    assert release.test_control['receipt']['paused']


@pytest.mark.parametrize('sid', [WORKER, BROKER, COORDINATOR])
def test_failed_staging_response_does_not_launch_later_roles_or_migrate(cutover, sid):
    release, render = configure_native_source(cutover)
    render.fail_service = sid
    migrations = []
    release.native_migrate = lambda *args: migrations.append(args)
    with pytest.raises(ReleaseError, match='concurrent deployment failed'):
        release.run()
    assert len(render.history) == 2 + [WORKER, BROKER, COORDINATOR].index(sid)
    assert not migrations and release.test_control['receipt']['paused']
    group = release.record['batches'][0]
    assert len(group['deployments']) == 3
    assert group['deployments'][[WORKER, BROKER, COORDINATOR].index(sid)]['status'] == 'update_failed'
    assert all(item['id'] is None for item in group['deployments'] if item['status'] == 'prepared')


@pytest.mark.parametrize('sid', [WORKER, BROKER, COORDINATOR])
def test_every_old_background_owner_must_retire_before_migration(concurrent, sid):
    release, render, _ = concurrent
    held = render.owners[sid][0]
    original_probe = release.probe
    migrations = []
    release.native_migrate = lambda *args: migrations.append(args)

    def probe(service):
        state = original_probe(service)
        if release.batch and state.get('ok'):
            if held not in render.owners[sid]:
                render.owners[sid].append(held)
            state['owners'] = sorted(set(state['owners']) | {held})
            if sid == COORDINATOR:
                state['coordinators'] = [held]
            if sid == BROKER:
                state['brokers'] = state['broker_leaders'] = [held]
        return state

    release.probe = probe
    with pytest.raises(ReleaseError, match='old-owner retirement'):
        release.run()
    assert not migrations and len(render.history) == 4
    assert release.test_control['receipt']['paused']


@pytest.mark.parametrize('fault', ['unknown_owner', 'source', 'policy', 'effect', 'environment'])
def test_drift_during_batch_stops_before_migration_or_resume(concurrent, fault):
    release, render, _ = concurrent
    original_probe = release.probe

    def probe(service):
        state = original_probe(service)
        if release.batch and len(render.history) == 4 and state.get('ok'):
            if fault == 'unknown_owner':
                state['owners'].append('999@2026-10-16T00:00:00+00:00')
            elif fault == 'source':
                state['source_policy'] = '9' * 64
            elif fault == 'policy':
                state['policy'] = '9' * 64
            elif fault == 'effect':
                state['native_effects'] = 1
            else:
                render.env[API_ID]['SESSION_SECRET'] = 'synthetic-drift'
        return state

    release.probe = probe
    with pytest.raises(ReleaseError):
        release.run()
    assert len(render.history) == 4
    assert not release.record.get('zero_owner_boundary_verified')
    assert release.test_control['receipt']['paused']
