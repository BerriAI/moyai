"""Saved top-level chats reuse machines; cleanup and attribution remain durable."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import modal
from fastapi import HTTPException

from app.db import Store
from app.computer import Computer
from app.config import MODEL_CATALOG
from app.durable_runner import DurableRunner
from app.runner import RunManager
from app.security import Security, digest
from app.session_lifecycle import SessionLifecycle
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
    monkeypatch.setitem(MODEL_CATALOG, 'second-model', 'Second test model')
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
    async def lost_ack(machine, session_id):
        await terminate(machine, session_id)
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
    finalized = []
    async def save_captures(machine, run_id, *, releasing):
        assert machine.alive and releasing is True
        finalized.append(run_id)
    manager.computer = SimpleNamespace(locks={}, touched=lambda _: 0, save_captures=save_captures)
    await manager.cancel(root)
    await drive(manager, root)
    assert finalized == [root]
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


@pytest.mark.parametrize('idle_seconds', [0, 300])
async def test_computer_capture_scope_follows_turn_and_workspace_lifetime(durable, monkeypatch, idle_seconds):
    manager, cloud, root = durable
    manager.settings.sandbox_idle_seconds = idle_seconds
    tick = clock(monkeypatch)
    touched = tick.now + 250
    calls = []
    async def execute(machine, action, payload=None):
        assert machine.alive
        if action == 'captures':
            return []
        assert action == 'request'
        body = json.loads(payload)
        assert body['action'] == 'finish'
        calls.append(body['args']['all'])
        return {}
    manager.computer = Computer(manager.settings, manager.store, None, manager, None)
    manager.computer.execute = execute
    manager.computer.touched = lambda _: touched
    manager.save_artifact = DurableRunner.save_artifact.__get__(manager)
    await drive(manager, root, phase='checkpointed')
    assert calls == [False]
    assert cloud.machines[0].alive
    if idle_seconds:
        await drive(manager, root, phase='warm')
        tick.now += 300
        assert (await manager.advance(root))['idle_seconds'] == 250
        assert calls == [False] and cloud.machines[0].alive
        tick.now += 250
        assert await manager.advance(root) is False
    else:
        await drive(manager, root)
    assert calls == [False, True] and not cloud.machines[0].alive


async def test_agent_restore_precedes_launch_on_durable_install(durable):
    manager, cloud, run_id = durable
    restored = []
    async def restore(machine, scope, *, required):
        assert not cloud.launches and machine.alive and required is False
        restored.append((machine.object_id, scope))
    manager.computer = SimpleNamespace(restore=restore)
    await drive(manager, run_id, phase='monitor')
    assert restored == [(cloud.machines[0].object_id, run_id)]


@pytest.mark.parametrize('block_delivery', ['confirmed', 'unavailable'])
async def test_corrupt_browser_checkpoint_does_not_abort_nonbrowser_agent_work(durable, monkeypatch, block_delivery):
    from test_durable import Machine
    manager, cloud, run_id = durable
    hub = manager.computer = Computer(manager.settings, manager.store, Security(manager.settings), manager, None)
    encrypted = 'unreadable-browser-checkpoint'
    manager.store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (run_id, encrypted))
    calls = []
    async def request(machine, body):
        calls.append(body)
        if block_delivery == 'unavailable':
            raise ConnectionError('Private transport unavailable')
        return {'scope': run_id, 'restored': True}
    monkeypatch.setattr(Machine, 'computer_request', request)
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.launches) == 1
    assert calls == [{'action': 'state', 'args': {'browser': 'restore', 'scope': run_id, 'blocked': True}}]
    assert manager.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (run_id,))[0]['encrypted'] == encrypted
    assert any('browser' in event['message'].lower() for event in manager.store.events(run_id))


@pytest.mark.parametrize('boundary', ['save', 'capture_failure', 'idle', 'failed'])
async def test_browser_auth_is_saved_by_artifact_and_shutdown_lifecycles(durable, monkeypatch, boundary):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    tick = clock(monkeypatch)
    await drive(manager, run_id, phase='warm')
    machine = cloud.machines[0]
    state = {'storage': {'cookies': [{'name': 'session', 'value': 'signed-in-after-answer'}], 'origins': []},
             'pages': [], 'active': 0}
    machine.computer_request = AsyncMock(return_value={'scope': run_id, 'state': state})
    hub = manager.computer = Computer(manager.settings, manager.store, Security(manager.settings), manager, None)
    hub.execute = AsyncMock(return_value={'recording': False})
    hub.sync = AsyncMock()
    if boundary == 'capture_failure':
        hub.execute.side_effect = OSError('Capture finalization unavailable')
    if boundary in {'save', 'capture_failure'}:
        await RunManager.save_artifact(manager, machine, run_id)
    else:
        if boundary == 'failed':
            manager.fail(run_id, manager.state(run_id), 'Fixture cloud failure')
            await drive(manager, run_id)
        else:
            tick.now += 300
            await manager.advance(run_id)
        assert not machine.alive
    encrypted = manager.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (run_id,))[0]['encrypted']
    assert 'signed-in-after-answer' not in encrypted
    assert json.loads(hub.security.decrypt(encrypted)) == {'scope': run_id, 'state': state}


async def sleeping_computer(durable):
    manager, cloud, run_id = durable
    await drive(manager, run_id)
    manager.settings.modal_token_id = 'test-token-id'
    manager.settings.modal_token_secret = 'test-token-secret'
    manager.computer = SimpleNamespace(touched=lambda _: 0, restore=AsyncMock(),
        wake=AsyncMock(return_value={'available': True}), save_captures=AsyncMock())
    return manager, cloud, run_id


@pytest.mark.parametrize('status', ['idle', 'cancelled', 'interrupted'])
async def test_computer_wake_preserves_chat_and_deduplicates_without_agent_capability(durable, monkeypatch, status):
    manager, cloud, run_id = await sleeping_computer(durable)
    manager.store.update_run(run_id, status=status)
    before = manager.store.messages(run_id)
    revision = manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,))[0]['revision']
    restored, provisions = [], []
    monkeypatch.setattr('app.sandboxes.modal.modal.Image.from_id',
                        lambda identity, **_: restored.append(identity) or 'image')
    async def create(**kwargs):
        provisions.append(kwargs)
        return await cloud.create(**kwargs)
    monkeypatch.setattr('app.sandboxes.modal.modal.Sandbox.create', aio(create))
    await asyncio.gather(manager.wake_computer(run_id), manager.wake_computer(run_id))
    operation = manager.state(run_id)['message_id']
    await drive(manager, run_id, phase='warm')
    await manager.wake_computer(run_id)
    assert manager.state(run_id)['message_id'] == operation
    assert manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,))[0]['revision'] == revision + 1
    assert restored == ['im-1'] and len(provisions) == 1 and provisions[0]['secrets'] == []
    assert manager.store.run(run_id)['token_hash'] == ''
    assert manager.store.run(run_id)['status'] == status
    assert manager.store.messages(run_id) == before and len(cloud.launches) == 1
    manager.computer.restore.assert_awaited_once_with(cloud.machines[-1], run_id)
    manager.computer.wake.assert_awaited_once_with(cloud.machines[-1])


async def test_computer_wake_reconnects_lost_create_ack_after_restart(durable, monkeypatch):
    manager, cloud, run_id = await sleeping_computer(durable)
    async def create(**kwargs):
        await cloud.create(**kwargs)
        raise ConnectionError('Create acknowledgement lost')
    monkeypatch.setattr('app.sandboxes.modal.modal.Sandbox.create', aio(create))
    await manager.wake_computer(run_id)
    operation = manager.state(run_id)['message_id']
    with pytest.raises(ConnectionError):
        await manager.advance(run_id)
    restored = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    restored.computer = manager.computer
    await drive(restored, run_id, phase='warm')
    assert restored.state(run_id)['message_id'] == operation
    assert len(cloud.machines) == 2 and len(cloud.launches) == 1
    assert restored.store.run(run_id)['sandbox_id'] == cloud.machines[-1].object_id
    assert len(answers(restored, run_id)) == 1


@pytest.mark.parametrize('lose_ack', [False, True])
async def test_stop_during_computer_provision_cleans_unacknowledged_machine(durable, monkeypatch, lose_ack):
    manager, cloud, run_id = await sleeping_computer(durable)
    async def create(**kwargs):
        machine = await cloud.create(**kwargs)
        await manager.cancel(run_id)
        if lose_ack:
            raise ConnectionError('Create acknowledgement lost after Stop')
        return machine
    monkeypatch.setattr('app.sandboxes.modal.modal.Sandbox.create', aio(create))
    await manager.wake_computer(run_id)
    if lose_ack:
        with pytest.raises(ConnectionError):
            await manager.advance(run_id)
    else:
        await manager.advance(run_id)
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'cancelled'
    assert manager.store.run(run_id)['sandbox_id'] == ''
    assert not cloud.machines[-1].alive and len(cloud.launches) == 1
    manager.computer.wake.assert_not_awaited()
    manager.computer.save_captures.assert_awaited_once_with(cloud.machines[-1], run_id, releasing=True)


@pytest.mark.parametrize('idle_seconds,stop', [(0, False), (300, False), (0, True)])
async def test_computer_wake_has_bounded_idle_cleanup_without_changing_answer(durable, monkeypatch, idle_seconds, stop):
    manager, cloud, run_id = await sleeping_computer(durable)
    tick = clock(monkeypatch)
    manager.settings.sandbox_idle_seconds = idle_seconds
    await manager.wake_computer(run_id)
    await drive(manager, run_id, phase='warm')
    assert (await manager.advance(run_id))['idle_seconds'] == 300
    if stop:
        await manager.cancel(run_id)
    else:
        tick.now += 300
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert not cloud.machines[-1].alive and len(cloud.launches) == 1
    assert [message['content'] for message in answers(manager, run_id)] == ['Saved answer']
    manager.computer.save_captures.assert_awaited_once_with(cloud.machines[-1], run_id, releasing=True)


async def test_real_message_reuses_awakened_computer_with_real_turn_capability(durable):
    manager, cloud, run_id = await sleeping_computer(durable)
    manager.store.update_run(run_id, status='cancelled')
    await manager.wake_computer(run_id)
    await drive(manager, run_id, phase='warm')
    machine = cloud.machines[-1]
    manager.store.enqueue_message(run_id, 'Continue from the desktop', 'desktop-followup', user_id='desktop-person')
    await drive(manager, run_id, phase='monitor')
    assert len(cloud.machines) == len(cloud.launches) == 2
    assert manager.state(run_id)['sandbox_id'] == machine.object_id
    assert not manager.state(run_id).get('computer_only')
    assert manager.store.run(run_id)['active_user_id'] == 'desktop-person'
    assert manager.store.run(run_id)['token_hash'] == digest(cloud.launch_tokens[-1])


async def test_computer_wake_reserves_capacity_and_blocks_delete_until_cleanup(durable):
    manager, cloud, run_id = await sleeping_computer(durable)
    manager.settings.max_concurrent_runs = 1
    lifecycle = SessionLifecycle(manager.store, None, manager, None)
    await manager.wake_computer(run_id)
    other = new_chat(manager)
    assert await manager.advance(other) == 'capacity'
    with pytest.raises(HTTPException) as deleting:
        lifecycle.delete(run_id, '', True)
    assert deleting.value.status_code == 409
    await drive(manager, run_id, phase='warm')
    await manager.cancel(run_id)
    await drive(manager, run_id)
    assert lifecycle.delete(run_id, '', True)['deleted']
    with pytest.raises(HTTPException) as waking:
        await manager.wake_computer(run_id)
    assert waking.value.status_code == 404
    assert len(cloud.machines) == 2 and len(cloud.launches) == 1


@pytest.mark.parametrize('failure', ['exception', 'unavailable'])
async def test_computer_start_failure_cleans_up_and_retry_preserves_chat(durable, failure):
    manager, cloud, run_id = await sleeping_computer(durable)
    before = manager.store.messages(run_id)
    if failure == 'exception':
        manager.computer.wake.side_effect = HTTPException(503, 'private runtime detail')
    else:
        manager.computer.wake.return_value = {'available': False}
    await manager.wake_computer(run_id)
    await drive(manager, run_id)
    assert manager.state(run_id)['computer_error'] == 'The computer could not start. Try waking it again.'
    assert not cloud.machines[-1].alive
    assert manager.store.messages(run_id) == before
    manager.computer.wake.side_effect = None
    manager.computer.wake.return_value = {'available': True}
    await manager.wake_computer(run_id)
    await drive(manager, run_id, phase='warm')
    assert not manager.state(run_id).get('computer_error')
    assert manager.store.messages(run_id) == before and len(cloud.launches) == 1


async def test_invalid_computer_provision_preserves_and_drains_queued_chat(durable, monkeypatch):
    manager, cloud, run_id = await sleeping_computer(durable)
    before = manager.store.messages(run_id)
    await manager.wake_computer(run_id)
    manager.store.enqueue_message(run_id, 'Continue the saved task', 'wake-followup')
    create = AsyncMock(side_effect=modal.exception.InvalidError('private provider detail'))
    monkeypatch.setattr('app.sandboxes.modal.modal.Sandbox.create', aio(create))
    assert await manager.advance(run_id) is True
    assert manager.state(run_id)['phase'] == 'warm_cleanup'
    assert 'private provider detail' not in json.dumps(manager.state(run_id))
    assert await manager.advance(run_id) is True
    assert manager.state(run_id)['phase'] == 'idle'
    assert manager.store.messages(run_id)[:-1] == before
    assert manager.store.messages(run_id)[-1]['status'] == 'queued'
    monkeypatch.setattr('app.sandboxes.modal.modal.Sandbox.create', aio(cloud.create))
    await drive(manager, run_id)
    assert len(answers(manager, run_id)) == 2
    assert len(cloud.launches) == 2
    assert all(message['status'] == 'completed' for message in manager.store.messages(run_id))


async def test_recover_dispatches_computer_intent_on_a_cancelled_session(durable):
    manager, cloud, run_id = await sleeping_computer(durable)
    manager.store.update_run(run_id, status='cancelled')
    await manager.wake_computer(run_id)
    restored = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    restored.serve = AsyncMock()
    restored.computer = manager.computer
    revision = restored.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,))[0]['revision']
    try:
        await restored.recover()
        assert restored.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,))[0]['revision'] == revision + 1
        await drive(restored, run_id, phase='warm')
        assert restored.store.run(run_id)['status'] == 'cancelled'
    finally:
        await restored.shutdown()


@pytest.mark.parametrize('other_action', ['delete', 'enqueue'])
async def test_computer_admission_rechecks_writes_committed_during_capacity_wait(durable, monkeypatch, other_action):
    manager, _, run_id = await sleeping_computer(durable)
    other_store = Store(manager.settings.data_dir)
    lifecycle = SessionLifecycle(other_store, None, manager, None)
    before = manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,))
    capacity = manager.make_capacity
    async def competing_write(identity):
        available = await capacity(identity)
        with ThreadPoolExecutor(1) as pool:
            if other_action == 'delete':
                pool.submit(lifecycle.delete, identity, '', True).result(timeout=5)
            else:
                pool.submit(other_store.enqueue_message, identity, 'Real follow-up', 'admission-race').result(timeout=5)
        return available
    monkeypatch.setattr(manager, 'make_capacity', competing_write)
    with pytest.raises(HTTPException) as rejected:
        await manager.wake_computer(run_id)
    assert rejected.value.status_code in {404, 409}
    assert manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,)) == before
    assert bool(manager.store.run(run_id)['deleted_at']) == (other_action == 'delete')
    assert manager.store.has_queued_messages(run_id) == (other_action == 'enqueue')


@pytest.mark.parametrize('other_action', ['delete', 'enqueue'])
async def test_computer_admission_holds_writer_lock_until_intent_and_wake_commit(durable, monkeypatch, other_action):
    manager, _, run_id = await sleeping_computer(durable)
    other_store = Store(manager.settings.data_dir)
    lifecycle = SessionLifecycle(other_store, None, manager, None)
    submit = manager.submit_in
    started, competing = Event(), []
    def write():
        try:
            with other_store.connect() as conn:
                conn.execute('PRAGMA busy_timeout=0')
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    conn.execute('BEGIN IMMEDIATE')
        finally:
            started.set()
        if other_action == 'delete':
            try:
                lifecycle.delete(run_id, '', True)
                return 'deleted'
            except HTTPException as exc:
                assert exc.status_code == 409
                return 'busy'
        other_store.enqueue_message(run_id, 'Real follow-up', 'admission-race')
        return 'queued'
    with ThreadPoolExecutor(1) as pool:
        def concurrent_submit(conn, run):
            competing.append(pool.submit(write))
            assert started.wait(5)
            return submit(conn, run)
        monkeypatch.setattr(manager, 'submit_in', concurrent_submit)
        await manager.wake_computer(run_id)
        assert competing[0].result(timeout=5) == ('busy' if other_action == 'delete' else 'queued')
    assert manager.store.run(run_id)['deleted_at'] == ''
    assert manager.state(run_id)['phase'] == 'provision'
    assert manager.store.has_queued_messages(run_id) == (other_action == 'enqueue')


async def test_computer_admission_rolls_back_intent_and_dispatch_together(durable, monkeypatch):
    manager, _, run_id = await sleeping_computer(durable)
    before = manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,))
    connect, failed = manager.store.connect, []
    @contextmanager
    def fail_before_commit():
        with connect() as conn:
            yield conn
            if conn.in_transaction and not failed:
                state = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,)).fetchone()
                if state and json.loads(state['state']).get('computer_only'):
                    failed.append(True)
                    raise RuntimeError('Simulated admission commit failure')
    monkeypatch.setattr(manager.store, 'connect', fail_before_commit)
    with pytest.raises(RuntimeError, match='admission commit'):
        await manager.wake_computer(run_id)
    assert failed
    assert manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,)) == before
    assert manager.has_capacity()


@pytest.mark.parametrize('phase,status,computer_only,pending,action,starting,shutting_down', [
    *[('idle', status, False, False, 'start', False, False)
      for status in ['idle', 'completed', 'failed', 'cancelled', 'interrupted']],
    ('warm', 'idle', False, False, 'reuse', False, False),
    ('warm', 'cancelled', False, False, 'blocked', False, True),
    ('warm', 'interrupted', False, False, 'blocked', False, True),
    ('warm', 'cancelled', True, False, 'reuse', False, False),
    ('warm', 'interrupted', True, False, 'reuse', False, False),
    ('warm', 'idle', True, True, 'blocked', True, False),
    ('idle', 'idle', False, True, 'blocked', True, False),
    *[(phase, 'provisioning', False, False, 'blocked', True, False)
      for phase in ['prepare', 'provision', 'install', 'launch', 'waiting_environment', 'startup_wait']],
    *[(phase, 'cancelled', True, False, 'pending', False, False)
      for phase in ['provision', 'install', 'waiting_environment']],
    ('provision', 'queued', True, True, 'pending', False, False),
    ('monitor', 'running', False, False, 'blocked', False, False),
    ('monitor', 'reconnecting', False, False, 'blocked', True, False),
    *[(phase, 'queued', False, True, 'blocked', False, False)
      for phase in ['waiting_children', 'waiting_credential', 'save', 'checkpointed', 'finish']],
    *[(phase, 'idle', True, False, 'blocked', False, True) for phase in ['cleanup', 'warm_cleanup']],
    ('idle', 'stopping', False, False, 'blocked', False, True),
    ('provision', 'stopping', True, False, 'blocked', False, True),
    ('unrecognized_phase', 'idle', False, False, 'blocked', False, False),
])
async def test_computer_lifecycle_matrix_agrees_with_wake_admission(
        durable, phase, status, computer_only, pending, action, starting, shutting_down):
    manager, _, run_id = await sleeping_computer(durable)
    manager.store.update_run(run_id, status=status)
    state = {'phase': phase, 'computer_only': computer_only}
    manager.save(run_id, state)
    policy = manager.computer_wake_state(manager.store.run(run_id), state, pending)
    assert policy['wake_action'] == action
    assert policy['can_wake'] == (action in {'start', 'reuse'})
    assert policy['waking'] == (action == 'pending')
    assert policy['starting'] == starting
    assert policy['shutting_down'] == shutting_down
    if not pending and action == 'blocked':
        with pytest.raises(HTTPException) as blocked:
            await manager.wake_computer(run_id)
        assert blocked.value.status_code == 409
        assert blocked.value.detail == policy['wake_notice']
    elif not pending and action == 'pending':
        before = manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,))
        await manager.wake_computer(run_id)
        assert manager.store.rows('SELECT state,revision FROM durable_sessions WHERE run_id=?', (run_id,)) == before


@pytest.mark.parametrize('message_status', ['queued', 'running', 'injected'])
async def test_computer_state_and_admission_share_pending_inbox_scope(durable, message_status):
    manager, _, run_id = await sleeping_computer(durable)
    manager.store.execute("UPDATE messages SET status=? WHERE run_id=? AND role='user'", (message_status, run_id))
    policy = manager.computer_state(run_id)
    assert policy['can_wake'] is False and policy['starting'] is True
    assert policy['shutting_down'] is False
    with pytest.raises(HTTPException) as busy:
        await manager.wake_computer(run_id)
    assert busy.value.status_code == 409 and busy.value.detail == policy['wake_notice']


@pytest.mark.parametrize('unavailable', ['closing', 'missing', 'deleted', 'demo', 'configuration'])
async def test_computer_lifecycle_honors_runtime_and_session_availability(durable, unavailable):
    manager, _, run_id = await sleeping_computer(durable)
    if unavailable == 'closing':
        manager.closing = True
    elif unavailable == 'missing':
        run_id = 'missing'
    elif unavailable == 'configuration':
        manager.settings.modal_token_secret = ''
    elif unavailable == 'deleted':
        manager.store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (run_id,))
    else:
        manager.store.execute("UPDATE runs SET mode='demo' WHERE id=?", (run_id,))
    policy = manager.computer_state(run_id)
    assert not any(policy[key] for key in ['can_wake', 'waking', 'starting'])
    assert policy['shutting_down'] is (unavailable != 'configuration')
    with pytest.raises(HTTPException) as blocked:
        await manager.wake_computer(run_id)
    assert blocked.value.detail == policy['wake_notice']
    assert blocked.value.status_code == {'closing': 409, 'configuration': 503}.get(unavailable, 404)
