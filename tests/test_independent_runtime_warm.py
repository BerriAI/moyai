"""Independent control guests survive clean turns, never changes of private scope."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import MODEL_CATALOG
from app.db import Store
from app.security import digest
from app.temporal_runtime import TemporalRunManager
from test_durable import durable, drive  # noqa: F401
from test_idle_window import answers, clock
from test_independent_runtime import request, runtime_durable  # noqa: F401


@pytest.fixture
def warm_runtime(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    manager.settings.sandbox_idle_seconds = 300
    tick = clock(monkeypatch)
    monkeypatch.setattr('app.independent_runtime.time', SimpleNamespace(time=lambda: tick.now))
    return manager, cloud, run_id, tick


@pytest.mark.parametrize('queued_before_finish', [False, True])
@pytest.mark.parametrize('restart', [False, True])
async def test_followup_reuses_control_guest_with_fresh_turn_capability(warm_runtime, monkeypatch,
                                                                       queued_before_finish, restart):
    manager, cloud, run_id, tick = warm_runtime
    manager.settings.max_concurrent_runs = 1
    async def forbidden(*args, **kwargs):
        pytest.fail('An app-only turn acquired a full computer')
    monkeypatch.setattr(manager, 'provision', forbidden)
    await drive(manager, run_id, phase='monitor')
    first = dict(manager.state(run_id))
    first_token = cloud.launch_tokens[0]
    if not queued_before_finish:
        await drive(manager, run_id, phase='warm')
        tick.now += 10
    manager.store.enqueue_message(run_id, 'Follow up', 'second')
    if queued_before_finish:
        await drive(manager, run_id, phase='warm')
        assert manager.state(run_id)['idle_until'] is None
        tick.now += 400  # Pending work is not idle time, even across restart.
    assert manager.store.run(run_id)['token_hash'] == ''
    if restart:
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        monkeypatch.setattr(manager, 'provision', forbidden)
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    assert state['independent_runtime'] and state['reused_machine']
    assert state['sandbox_id'] == first['sandbox_id'] and state['sandbox_name'] == first['sandbox_name']
    assert state['machine_started'] == first['machine_started']
    assert len(cloud.machines) == 1 and len(set(cloud.launches)) == 2
    assert cloud.machines[0].spec['independent_runtime']
    assert 'Saved answer' in json.dumps(cloud.machines[0].spec['history_fallback'])
    assert cloud.launch_tokens[1] != first_token
    assert manager.store.run(run_id)['token_hash'] == digest(cloud.launch_tokens[1])
    assert cloud.launch_tokens[1] not in json.dumps(cloud.machines[0].spec)
    await drive(manager, run_id, phase='warm')
    assert manager.has_capacity() and cloud.snapshots == 0 and not cloud.terminations
    assert manager.store.run(run_id)['sandbox_id'] == ''
    assert len(answers(manager, run_id)) == 2
    assert manager.store.run(run_id)['token_hash'] == ''


async def test_warm_guest_expires_without_republishing_and_restart_does_not_extend(warm_runtime):
    manager, cloud, run_id, tick = warm_runtime
    manager.settings.sandbox_idle_seconds = 3600
    await drive(manager, run_id, phase='warm')
    deadline = manager.state(run_id)['idle_until']
    assert deadline == tick.now + 300
    for _ in range(3):
        tick.now += 50
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        assert await manager.advance(run_id) == {'idle_seconds': deadline - tick.now}
        assert manager.state(run_id)['idle_until'] == deadline
    tick.now = deadline
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert cloud.terminations == [cloud.machines[0].object_id]
    assert len(answers(manager, run_id)) == 1
    manager.store.enqueue_message(run_id, 'After expiration', 'second')
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.machines) == len(cloud.launches) == 2


@pytest.mark.parametrize('change', ['actor', 'model', 'build'])
async def test_changed_scope_gets_a_fresh_control_filesystem(warm_runtime, monkeypatch, change):
    manager, cloud, run_id, _ = warm_runtime
    monkeypatch.setitem(MODEL_CATALOG, 'second-model', 'Second test model')
    await drive(manager, run_id, phase='warm')
    kwargs = {'user_id': 'new-requester'} if change == 'actor' else {'model': 'second-model'} if change == 'model' else {}
    manager.store.enqueue_message(run_id, 'New scope', 'second', **kwargs)
    if change == 'build':
        manager.prepared.build = 'another-runtime-image'
    await drive(manager, run_id, phase='monitor')
    assert not cloud.machines[0].alive and cloud.machines[1].alive
    assert len(cloud.machines) == len(cloud.launches) == 2
    assert cloud.machines[1].spec['independent_runtime']
    if change == 'model':
        assert cloud.machines[1].spec['model'] == 'second-model'
    if change == 'actor':
        assert manager.state(run_id)['runtime_scope'][1] == 'new-requester'


@pytest.mark.parametrize('setting', ['codex_runtime_reuse', 'sandbox_idle_seconds', 'independent_agent_runtime'])
async def test_disabling_reuse_releases_an_existing_idle_guest(warm_runtime, setting):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='warm')
    setattr(manager.settings, setting, 0 if setting == 'sandbox_idle_seconds' else False)
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle' and not cloud.machines[0].alive


@pytest.mark.parametrize('phase', ['finish', 'warm'])
async def test_stop_releases_control_guest_without_duplicate_answer(warm_runtime, phase):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase=phase)
    await manager.cancel(run_id)
    await drive(manager, run_id)
    assert not cloud.machines[0].alive
    assert manager.store.run(run_id)['status'] == 'cancelled'
    assert len(answers(manager, run_id)) == 1
    assert not manager.store.run(run_id)['token_hash']


@pytest.mark.parametrize('lost_at', ['warm', 'install', 'monitor'])
async def test_lost_control_guest_is_replaced_only_before_launch(warm_runtime, lost_at):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='warm')
    manager.store.enqueue_message(run_id, 'Next turn', 'second')
    if lost_at != 'warm':
        await drive(manager, run_id, phase=lost_at)
    cloud.machines[0].alive = False
    if lost_at == 'monitor':
        await drive(manager, run_id)
        assert manager.store.run(run_id)['status'] == 'interrupted'
        assert len(cloud.machines) == 1 and len(cloud.launches) == 2
    else:
        await drive(manager, run_id, phase='monitor')
        assert len(cloud.machines) == len(cloud.launches) == 2
        assert cloud.machines[1].spec['independent_runtime']


async def test_ui_computer_wake_releases_control_guest_before_normal_admission(warm_runtime, monkeypatch):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='warm')
    monkeypatch.setattr(type(manager.settings), 'missing_sandbox', lambda *a: [])
    assert manager.computer_state(run_id)['wake_action'] == 'start'
    await manager.wake_computer(run_id)
    state = manager.state(run_id)
    assert not cloud.machines[0].alive
    assert state['computer_only'] and state['phase'] == 'provision'
    assert not state.get('independent_runtime') and not state['sandbox_id']
    assert len(manager.store.messages(run_id)) == 2


async def test_lost_cleanup_ack_survives_restart(warm_runtime, monkeypatch):
    manager, cloud, run_id, tick = warm_runtime
    await drive(manager, run_id, phase='warm')
    original = cloud.machines[0].terminate.aio
    async def lost():
        await original()
        raise ConnectionError('Lost termination acknowledgment')
    monkeypatch.setattr(cloud.machines[0].terminate, 'aio', lost)
    tick.now += 300
    with pytest.raises(ConnectionError):
        await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'warm_cleanup'
    monkeypatch.setattr(cloud.machines[0].terminate, 'aio', original)
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert len(answers(manager, run_id)) == 1


async def test_concurrent_wakes_launch_one_followup(warm_runtime):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='warm')
    manager.store.enqueue_message(run_id, 'Next turn', 'second')
    await asyncio.gather(*(manager.advance(run_id) for _ in range(6)))
    await drive(manager, run_id, phase='warm')
    assert len(cloud.machines) == 1 and len(set(cloud.launches)) == 2
    assert len(answers(manager, run_id)) == 2


async def test_waiting_for_children_releases_retained_control_guest(warm_runtime, monkeypatch):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='warm')
    manager.store.enqueue_message(run_id, 'Delegate followup', 'second')
    manager.coordinator = SimpleNamespace()
    monkeypatch.setattr(manager.swarms, 'initial_group', lambda *a: {'status': 'running', 'id': 'group'})
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'waiting_children'
    assert not manager.state(run_id)['sandbox_id'] and not cloud.machines[0].alive


async def test_computer_checkpoint_and_release_precede_control_guest_reuse(warm_runtime, monkeypatch):
    manager, cloud, run_id, _ = warm_runtime
    await drive(manager, run_id, phase='monitor')
    caller = manager.store.run(run_id)
    operation = request()
    manager.agent_runtime.operations.exchange(caller, operation)
    original_command = cloud.command
    async def command(machine, action, directory, value, **kwargs):
        if '/computer-operations/' in directory and action == 'read':
            result = ({'workspace': '/workspace'} if machine.spec.get('prepare_computer') else
                      {'saved': True} if machine.spec.get('finish_computer') else
                      {'content': [{'type': 'text', 'text': 'verified result'}], 'isError': False})
            return json.dumps({'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 0,
                               'final': {'tool_result': result}})
        return await original_command(machine, action, directory, value, **kwargs)
    monkeypatch.setattr(manager, 'command', command)
    for _ in range(4):
        async with manager.session_guard(run_id):
            await manager.agent_runtime.computer_step(run_id, manager.state(run_id))
    await drive(manager, run_id, phase='warm')
    assert cloud.machines[0].alive and not cloud.machines[1].alive
    assert cloud.snapshots == 1 and manager.store.run(run_id)['snapshot_id'] == 'im-1'
    assert not manager.state(run_id)['computer'] and not manager.store.run(run_id)['sandbox_id']
    assert manager.has_capacity()
    assert manager.store.rows('SELECT status FROM computer_operations')[0]['status'] == 'completed'
    manager.store.enqueue_message(run_id, 'Continue with saved files', 'second')
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.machines) == 2 and manager.state(run_id)['snapshot_id'] == 'im-1'
    assert manager.state(run_id)['sandbox_id'] == cloud.machines[0].object_id
