import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import captures
from app.computer import Computer
from sandbox import computer as runtime
from test_durable import durable, drive
from test_runner import FakeSandbox, aio, runner
from test_workspace import workspace

WEBM = b'\x1aE\xdf\xa3\x42\x82\x84webm' + bytes(range(100))
PR = 'https://github.com/example/repo/pull/1'


def recorded_browser(manager, machine, run_id, monkeypatch):
    """Use production finalization and copying with only provider/ffmpeg replaced."""
    monkeypatch.setattr(runtime, 'CAPTURES', manager.settings.data_dir / 'sandbox-captures')
    path = runtime.capture_directory() / 'human.recording'
    browser = runtime.Computer()
    process = SimpleNamespace(returncode=None)
    def interrupt(_):
        path.write_bytes(WEBM)
        process.returncode = 255
    process.send_signal = interrupt
    process.wait = AsyncMock(side_effect=lambda: process.returncode)
    browser.recording = {'actor': 'google:owner', 'tab': PR, 'path': path,
                         'process': process, 'started': time.time()}
    hub = manager.computer = Computer(manager.settings, manager.store, None, manager, None)
    calls = []
    async def execute(sandbox, action, value=None, **kwargs):
        assert sandbox is machine
        assert not getattr(machine, 'terminated', False) and getattr(machine, 'alive', True)
        calls.append(action)
        if action == 'request':
            return await browser.command(json.loads(value))
        if action == 'captures':
            return runtime.capture_list()
        return runtime.capture_read(value)
    hub.execute = execute
    return browser, hub, calls


async def test_stop_finalizes_and_copies_human_recording_before_provider_shutdown(runner, monkeypatch):
    row = runner.store.create_run('Stop recording', '', 'modal', [])
    run_id = row['id']
    runner.store.update_run(run_id, status='running', token_hash='active-capability')
    machine = runner.sandboxes[run_id] = FakeSandbox()
    browser, hub, calls = recorded_browser(runner, machine, run_id, monkeypatch)
    lock = hub.locks[run_id] = asyncio.Lock()
    await lock.acquire()
    async def terminate():
        assert runner.store.run(run_id)['token_hash'] == ''
        assert browser.recording is None
        assert (captures.directory(runner.settings, run_id) / 'human.webm').read_bytes() == WEBM
        await machine.terminate_sandbox()
    machine.terminate = aio(terminate)
    try:
        await asyncio.wait_for(runner.cancel(run_id), 1)
        await runner.terminate(machine, run_id)  # execute finally joins the same release
    finally:
        lock.release()
    assert calls == ['request', 'captures', 'capture']
    assert machine.terminated


@pytest.mark.parametrize('blocked', ['runtime', 'copy'])
async def test_stalled_capture_release_is_bounded_and_reports_loss(runner, monkeypatch, blocked):
    monkeypatch.setattr('app.runner.CAPTURE_RELEASE_TIMEOUT', .02)
    row = runner.store.create_run('Stop stalled recording', '', 'modal', [])
    run_id = row['id']
    runner.store.update_run(run_id, status='running', token_hash='active-capability')
    machine = runner.sandboxes[run_id] = FakeSandbox()
    browser, hub, _ = recorded_browser(runner, machine, run_id, monkeypatch)
    lock = browser.lock if blocked == 'runtime' else hub.capture_locks.setdefault(run_id, asyncio.Lock())
    await lock.acquire()
    try:
        await asyncio.wait_for(runner.cancel(run_id), 1)
    finally:
        lock.release()
    assert machine.terminated and runner.store.run(run_id)['token_hash'] == ''
    assert not captures.listing(runner.settings, run_id)
    assert any('could not be saved' in row['message'] for row in runner.store.events(run_id))


async def test_concurrent_release_waiters_share_cleanup_and_waiter_cancellation_isolated(runner):
    row = runner.store.create_run('Concurrent cleanup', '', 'modal', [])
    run_id = row['id']
    started, finish = asyncio.Event(), asyncio.Event()
    async def save(*args, **kwargs):
        started.set()
        await finish.wait()
    runner.computer = SimpleNamespace(save_captures=AsyncMock(side_effect=save))
    machine = FakeSandbox()
    machine.terminate = aio(AsyncMock(side_effect=machine.terminate_sandbox))
    first = asyncio.create_task(runner.terminate(machine, run_id))
    await started.wait()
    second = asyncio.create_task(runner.terminate(machine, run_id))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    await second
    await runner.terminate(machine, run_id)
    runner.computer.save_captures.assert_awaited_once()
    machine.terminate.aio.assert_awaited_once()
    replacement = FakeSandbox()
    replacement.object_id = 'sb-replacement'
    await runner.terminate(replacement, run_id)
    assert replacement.terminated and runner.computer.save_captures.await_count == 2
    await runner.terminate(machine, run_id)  # A late old caller cannot replace the newer release.
    await runner.terminate(replacement, run_id)
    assert runner.computer.save_captures.await_count == 2


async def test_failed_provider_termination_remains_retryable(runner):
    row = runner.store.create_run('Retry cleanup', '', 'modal', [])
    machine = FakeSandbox()
    attempts = 0
    async def terminate():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError('Unknown provider outcome')
        await machine.terminate_sandbox()
    machine.terminate = aio(terminate)
    with pytest.raises(ConnectionError):
        await runner.terminate(machine, row['id'])
    await runner.terminate(machine, row['id'])
    assert attempts == 2 and machine.terminated


@pytest.mark.parametrize('phase', ['monitor', 'warm'])
async def test_temporal_stop_preserves_human_capture_in_active_and_warm_session(durable, monkeypatch, phase):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, run_id, phase=phase)
    machine = cloud.machines[0]
    browser, _, calls = recorded_browser(manager, machine, run_id, monkeypatch)
    await manager.cancel(run_id)
    assert manager.store.run(run_id)['token_hash'] == ''
    await drive(manager, run_id)
    assert not machine.alive and browser.recording is None
    assert (captures.directory(manager.settings, run_id) / 'human.webm').read_bytes() == WEBM
    assert calls == ['request', 'captures', 'capture']


@pytest.mark.parametrize('durable_mode', [False, True])
async def test_root_capability_revoked_before_child_cleanup_wait(runner, durable, durable_mode):
    if durable_mode:
        manager, _, run_id = durable
        await drive(manager, run_id, phase='monitor')
    else:
        manager = runner
        run_id = manager.store.create_run('Root stop', '', 'modal', [])['id']
        manager.store.update_run(run_id, status='running', token_hash='active-capability')
    started, finish = asyncio.Event(), asyncio.Event()
    async def children(identity):
        assert manager.store.run(identity)['status'] == 'stopping'
        assert manager.store.run(identity)['token_hash'] == ''
        started.set()
        await finish.wait()
    manager.coordinator = SimpleNamespace(cancel_children=children)
    stopped = asyncio.create_task(manager.cancel(run_id))
    await asyncio.wait_for(started.wait(), 1)
    finish.set()
    await stopped


@pytest.mark.parametrize('closing', ['stopping', 'releasing'])
async def test_computer_mutation_waiting_for_ui_lock_rechecks_shutdown(workspace, closing):
    app, client = workspace
    hub, store = app.state.computer, app.state.store
    row = store.create_run('Late input', '', 'modal', [], chat_enabled=True)
    run_id = row['id']
    store.update_run(run_id, status='running', sandbox_id='sb-first')
    hub.sandbox = AsyncMock(return_value=None)
    lock = hub.locks[run_id] = asyncio.Lock()
    await lock.acquire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=app.state.settings.public_url,
                                cookies=client.cookies, headers=client.headers) as browser:
        post = asyncio.create_task(browser.post(f'/api/runs/{run_id}/computer', json={'action': 'record_start'}))
        await asyncio.sleep(0)
        if closing == 'stopping':
            store.update_run(run_id, status='stopping')
        else:
            hub.releasing[run_id] = 'sb-first'
        lock.release()
        response = await post
        assert response.status_code == 409 and 'shutting down' in response.json()['detail']
        hub.sandbox.assert_not_awaited()
        store.update_run(run_id, status='running', sandbox_id='sb-new')
        response = await browser.post(f'/api/runs/{run_id}/computer', json={'action': 'claim'})
        assert response.status_code == 409 and 'asleep' in response.json()['detail']
        hub.sandbox.assert_awaited_once()


@pytest.mark.parametrize('closing', ['stopping', 'releasing'])
@pytest.mark.parametrize('presence', ['missing_sandbox', 'missing_tab', 'legacy', 'live', 'recording_without_page'])
def test_stopping_close_acknowledges_only_verified_absence(workspace, closing, presence):
    app, client = workspace
    hub, store = app.state.computer, app.state.store
    row = store.create_run('Close after Stop', '', 'modal', [], chat_enabled=True)
    run_id = row['id']
    store.update_run(run_id, status='stopping' if closing == 'stopping' else 'cancelled', sandbox_id='sb-stopped')
    if closing == 'releasing':
        hub.releasing[run_id] = 'sb-stopped'
    scope = {'available': presence == 'live', 'recording': presence == 'recording_without_page'}
    if presence != 'legacy':
        scope['tab'] = PR
    request = AsyncMock(return_value=scope)
    hub.sandbox = AsyncMock(return_value=None if presence == 'missing_sandbox' else
                            SimpleNamespace(computer_request=request))
    response = client.post(f'/api/runs/{run_id}/computer', json={'action': 'close_tab', 'tab': PR})
    if presence in {'live', 'recording_without_page'}:
        assert response.status_code == 409 and 'shutting down' in response.json()['detail']
    else:
        assert response.status_code == 200 and response.json()['ok'] is True
    assert all(call.args[0]['action'] == 'state' for call in request.await_args_list)


async def test_child_stop_reservations_do_not_wait_for_sibling_cleanup(durable):
    from test_agents import launch
    manager, _, root = durable
    coordinator, result, _ = await launch(durable, count=3)
    children = [row['id'] for row in coordinator.children(result['group_id'])]
    stopped, finish = asyncio.Event(), asyncio.Event()
    reserved = set()
    original = manager.cancel
    async def delayed(run_id):
        await original(run_id)
        reserved.add(run_id)
        if reserved == set(children):
            stopped.set()
        await finish.wait()
    manager.cancel = delayed
    task = asyncio.create_task(coordinator.cancel_children(root))
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        assert all(manager.store.run(child)['status'] == 'stopping' for child in children)
        assert all(manager.store.run(child)['token_hash'] == '' for child in children)
    finally:
        finish.set()
        await task


async def test_release_cache_bounds_completed_tasks_without_evicting_active_cleanup(runner, monkeypatch):
    monkeypatch.setattr('app.runner.RETAINED_RELEASES', 3)
    run_id = runner.store.create_run('Many rotated machines', '', 'modal', [])['id']
    entered, finish = asyncio.Event(), asyncio.Event()
    pending = FakeSandbox()
    pending.object_id = 'sb-in-flight'
    async def slow_stop():
        entered.set()
        await finish.wait()
        await pending.terminate_sandbox()
    pending.terminate = aio(slow_stop)
    waiting = asyncio.create_task(runner.terminate(pending, run_id))
    await entered.wait()
    for number in range(8):
        machine = FakeSandbox()
        machine.object_id = f'sb-rotation-{number}'
        await runner.terminate(machine, run_id)
    assert len(runner.releases) == 4
    assert not runner.releases[run_id, pending.object_id].done()
    finish.set()
    await waiting
    assert len(runner.releases) == 3


def test_stop_during_provider_lookup_blocks_the_later_computer_mutation(workspace):
    app, client = workspace
    hub, store = app.state.computer, app.state.store
    row = store.create_run('Stop during lookup', '', 'modal', [], chat_enabled=True)
    run_id = row['id']
    store.update_run(run_id, status='running', sandbox_id='sb-active')
    request = AsyncMock(return_value={})
    async def sandbox(run):
        store.update_run(run_id, status='stopping')
        return SimpleNamespace(computer_request=request)
    hub.sandbox = sandbox
    response = client.post(f'/api/runs/{run_id}/computer', json={'action': 'record_start'})
    assert response.status_code == 409 and 'shutting down' in response.json()['detail']
    request.assert_not_awaited()
