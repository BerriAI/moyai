"""Saved top-level chats reuse machines; cleanup and attribution remain durable."""
import asyncio
from types import SimpleNamespace

import pytest

from app.db import Store
from app.durable_runner import DurableRunner
from app.security import digest
from app.temporal_runtime import TemporalRunManager
from test_durable import aio, durable, drive
from test_agents import launch, pause_parent
from test_workspace import workspace


def clock(monkeypatch):
    tick = SimpleNamespace(now=100000.0)
    monkeypatch.setattr('app.durable_runner.time', SimpleNamespace(time=lambda: tick.now))
    return tick


def new_chat(manager, label='Another session'):
    row = manager.store.create_run(label, '', 'modal', [], chat_enabled=True, model='test-model')
    manager.submit(row)
    return row['id']


def answers(manager, run_id):
    return [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']


@pytest.mark.parametrize('queued_before_finish', [True, False])
async def test_followup_reuses_machine_with_new_user_model_and_capability(durable, monkeypatch, queued_before_finish):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    manager.settings.agent_models = 'test-model,second-model'
    tick = clock(monkeypatch)
    await drive(manager, run_id, phase='monitor')
    first_token = cloud.launch_tokens[0]
    if not queued_before_finish:
        await drive(manager, run_id, phase='warm')
        assert manager.state(run_id)['idle_until'] == tick.now + 300
        assert manager.store.run(run_id)['token_hash'] == ''
        tick.now += 100
    manager.store.enqueue_message(run_id, 'Follow-up', 'second', model='second-model', user_id='another-user')
    if queued_before_finish:
        await drive(manager, run_id, phase='warm')
        assert manager.state(run_id)['idle_until'] is None
        # Offline/slow workers must drain the inbox before the idle clock starts.
        tick.now += 400
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.machines) == 1 and not cloud.terminations
    assert cloud.machines[0].spec['model'] == 'second-model'
    assert manager.store.run(run_id)['active_user_id'] == 'another-user'
    assert cloud.launch_tokens[1] != first_token
    assert manager.store.run(run_id)['token_hash'] == digest(cloud.launch_tokens[1])
    assert cloud.launch_tokens[1] not in str(cloud.machines[0].spec)
    await drive(manager, run_id, phase='warm')
    assert manager.state(run_id)['idle_until'] == tick.now + 300
    assert manager.store.run(run_id)['snapshot_id'] == 'im-2'
    assert len(answers(manager, run_id)) == 2
    assert manager.store.run(run_id)['token_hash'] == ''


async def test_wakes_and_restart_do_not_extend_expiry_and_next_turn_restores(durable, monkeypatch):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    tick = clock(monkeypatch)
    await drive(manager, run_id, phase='warm')
    deadline = manager.state(run_id)['idle_until']
    for _ in range(3):
        tick.now += 50
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        result = await manager.advance(run_id)
        assert result['idle_seconds'] == deadline - tick.now
        assert manager.state(run_id)['idle_until'] == deadline
    tick.now = deadline
    assert await manager.advance(run_id) is False
    assert not cloud.machines[0].alive
    assert manager.state(run_id)['phase'] == 'idle'
    assert manager.store.run(run_id)['sandbox_id'] == ''
    assert len(answers(manager, run_id)) == 1
    manager.store.enqueue_message(run_id, 'After expiry', 'after')
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.machines) == 2
    assert manager.state(run_id)['snapshot_id'] == 'im-1'


async def test_lost_termination_ack_recovers_without_duplicate_answer(durable, monkeypatch):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    tick = clock(monkeypatch)
    await drive(manager, run_id, phase='warm')
    terminate = manager.terminate
    async def lost_ack(machine):
        await terminate(machine)
        raise ConnectionError('Lost termination acknowledgement')
    manager.terminate = lost_ack
    tick.now += 300
    with pytest.raises(ConnectionError):
        await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'warm_cleanup'
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert len(answers(manager, run_id)) == len(cloud.terminations) == 1


@pytest.mark.parametrize('missing_during_install', [False, True])
async def test_missing_idle_machine_replaced_only_before_launch(durable, missing_during_install):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, run_id, phase='warm')
    manager.store.enqueue_message(run_id, 'Follow up', 'second')
    if missing_during_install:
        await drive(manager, run_id, phase='install')
    cloud.machines[0].alive = False
    await drive(manager, run_id, phase='monitor')
    assert manager.state(run_id)['snapshot_id'] == 'im-1'
    assert len(cloud.machines) == len(cloud.launches) == 2
    assert len(answers(manager, run_id)) == 1


async def test_capacity_reclaims_warm_oldest_first_without_overbooking(durable, monkeypatch):
    manager, cloud, first = durable
    manager.settings.sandbox_idle_seconds = 300
    manager.settings.max_concurrent_runs = 2
    cloud.saving_before_answer = False
    tick = clock(monkeypatch)
    await drive(manager, first, phase='warm')
    tick.now += 10
    second = new_chat(manager)
    await drive(manager, second, phase='warm')
    third, fourth, fifth = [new_chat(manager, str(i)) for i in range(3)]
    results = await asyncio.gather(*(manager.advance(i) for i in [third, fourth, fifth]))
    assert results.count('capacity') == 1
    assert cloud.terminations == ['sb-0', 'sb-1']
    assert not manager.has_capacity()
    assert len(answers(manager, first)) == len(answers(manager, second)) == 1


async def test_queued_warm_chat_is_protected_from_capacity_eviction(durable):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    manager.settings.max_concurrent_runs = 1
    await drive(manager, root, phase='warm')
    manager.store.enqueue_message(root, 'Follow-up', 'second')
    other = new_chat(manager)
    assert await manager.advance(other) == 'capacity'
    assert cloud.machines[0].alive and not cloud.terminations
    await drive(manager, root, phase='monitor')
    assert len(cloud.machines) == 1


async def test_lost_warm_machine_must_reacquire_capacity(durable):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    manager.settings.max_concurrent_runs = 1
    await drive(manager, root, phase='warm')
    manager.store.enqueue_message(root, 'Follow-up', 'second')
    other = new_chat(manager)
    cloud.machines[0].alive = False
    release = manager.release_warm
    async def release_then_competing_admission(*args):
        await release(*args)
        assert await manager.advance(other) is True
    manager.release_warm = release_then_competing_admission
    assert await manager.advance(root) == 'capacity'
    assert manager.state(root)['phase'] == 'idle'
    assert len(cloud.machines) == 1
    assert manager.store.has_queued_messages(root)


@pytest.mark.parametrize('phase', ['finish', 'warm'])
async def test_stop_at_finish_or_during_idle_releases_without_losing_answer(durable, phase):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, root, phase=phase)
    await manager.cancel(root)
    await drive(manager, root)
    assert not cloud.machines[0].alive
    assert manager.store.run(root)['status'] == 'cancelled'
    assert manager.store.run(root)['token_hash'] == ''
    assert [m['content'] for m in answers(manager, root)] == ['Saved answer']


async def test_stop_during_reuse_poll_never_launches_queued_message(durable):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, root, phase='warm')
    manager.store.enqueue_message(root, 'Follow-up', 'second')
    sandbox = manager.sandbox
    async def stop_while_polling(state):
        await manager.cancel(root)
        return await sandbox(state)
    manager.sandbox = stop_while_polling
    await drive(manager, root)
    assert len(cloud.launches) == 1
    assert not cloud.machines[0].alive
    assert manager.store.run(root)['status'] == 'cancelled'


async def test_idle_window_never_extends_absolute_machine_lifetime(durable, monkeypatch):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    tick = clock(monkeypatch)
    await drive(manager, root, phase='warm')
    state = manager.state(root)
    state['machine_started'] = tick.now - manager.settings.sandbox_rotation_seconds + 30
    manager.save(root, state)
    assert (await manager.advance(root))['idle_seconds'] == 30
    manager.store.enqueue_message(root, 'Follow-up', 'second')
    tick.now += 30
    await drive(manager, root, phase='monitor')
    assert not cloud.machines[0].alive and len(cloud.machines) == 2


async def test_children_and_waiting_parent_still_release_immediately(durable):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    coordinator, result, _ = await launch(durable, count=2)
    await pause_parent(manager, root, result['group_id'])
    assert not cloud.machines[0].alive
    for child in coordinator.children(result['group_id']):
        await drive(manager, child['id'])
        assert not cloud.machines[-1].alive
    await drive(manager, root, phase='warm')
    assert sum(m.alive for m in cloud.machines) == 1


@pytest.mark.parametrize('failure', ['save', 'agent', 'active_machine'])
async def test_failed_turns_do_not_stay_warm(durable, failure):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, root, phase='monitor')
    if failure == 'agent':
        cloud.machines[0].operations[cloud.launches[0]]['completed'] = False
    elif failure == 'active_machine':
        cloud.machines[0].alive = False
    else:
        cloud.save_failures = 3
    for _ in range(30):
        try:
            await manager.advance(root)
        except TimeoutError:
            pass
        if manager.state(root)['phase'] == 'idle':
            break
    assert manager.state(root)['phase'] == 'idle'
    assert manager.store.run(root)['status'] in {'failed', 'interrupted'}
    assert not cloud.machines[0].alive and len(cloud.launches) == 1


async def test_launch_capability_uses_exec_environment_not_command_args(durable):
    manager, cloud, root = durable
    calls = []
    async def read():
        return ''
    async def wait():
        return 0
    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=SimpleNamespace(read=aio(read)), stderr=SimpleNamespace(read=aio(read)), wait=aio(wait))
    machine = SimpleNamespace(exec=aio(execute))
    await DurableRunner.command(manager, machine, 'start', '/execution', '/spec', token='fresh-turn-capability')
    assert calls[0][1]['env'] == {'WORKSPACE_RUN_TOKEN': 'fresh-turn-capability'}
    assert 'fresh-turn-capability' not in str(calls[0][0])


def test_runtime_reports_effective_idle_setting(workspace):
    app, client = workspace
    assert client.get('/api/config').json()['sandbox_idle_seconds'] == 0
    app.state.settings.temporal_enabled = True
    app.state.manager.ready = asyncio.Event()
    assert client.get('/api/config').json()['sandbox_idle_seconds'] == 300
