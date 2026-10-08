"""Capture persistence and explicit hosted-media security workflows."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import base64
from io import BytesIO
import logging
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4
from fastapi import HTTPException
from PIL import Image
from app.access_logging import RedactQueryStrings
from app.db import Store
from app.media_shares import MediaShares, TOOLS, validated_mime
from app.security import digest
from storage_fixture import MemoryObjects
from test_spend import active
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import captures
from app.computer import Computer
from app.security import Security
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
    browser.browser_scope = run_id
    machine.computer_request = browser.command
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
    assert not captures.listing(runner.settings, run_id, store=runner.store)
    assert any('could not be saved' in row['message'] for row in runner.store.events(run_id))


@pytest.mark.parametrize('blocked', ['finish', 'copy', 'checkpoint_lock', 'storage'])
async def test_shutdown_gives_browser_auth_and_captures_independent_budgets(runner, monkeypatch, blocked):
    monkeypatch.setattr('app.runner.CAPTURE_RELEASE_TIMEOUT', .15)
    monkeypatch.setattr('app.computer.BROWSER_CHECKPOINT_TIMEOUT', .04)
    monkeypatch.setattr(runtime, 'BROWSER_STORAGE_TIMEOUT', .02)
    run_id = runner.store.create_run('Save browser and recording', '', 'modal', [])['id']
    machine = runner.sandboxes[run_id] = FakeSandbox()
    runner.store.update_run(run_id, status='running', sandbox_id=machine.object_id, token_hash='active-capability')
    browser, hub, _ = recorded_browser(runner, machine, run_id, monkeypatch)
    hub.security = Security(runner.settings)
    browser.saved_browser = {'storage': {'cookies': [{'name': 'session', 'value': 'old-login'}], 'origins': []},
                             'pages': [], 'active': 0}
    await hub.checkpoint(machine, run_id)
    previous = runner.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (run_id,))[0]['encrypted']
    fresh = {'cookies': [{'name': 'session', 'value': 'new-login'}], 'origins': []}
    browser.context = SimpleNamespace(pages=[], storage_state=AsyncMock(return_value=fresh))
    cancelled = []
    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    lock = None
    if blocked == 'finish':
        browser.stop_recording = stalled
    elif blocked == 'storage':
        browser.context.storage_state.side_effect = stalled
    else:
        owner = hub.capture_locks if blocked == 'copy' else hub.checkpoint_locks
        lock = owner.setdefault(run_id, asyncio.Lock())
        await lock.acquire()
    async def terminate():
        encrypted = runner.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (run_id,))[0]['encrypted']
        if blocked in {'finish', 'copy'}:
            assert json.loads(hub.security.decrypt(encrypted))['state']['storage'] == fresh
        else:
            assert encrypted == previous
            assert (captures.directory(runner.settings, run_id) / 'human.webm').read_bytes() == WEBM
        await machine.terminate_sandbox()
    machine.terminate = aio(terminate)
    try:
        await asyncio.wait_for(runner.cancel(run_id), 1)
    finally:
        if lock:
            lock.release()
    assert machine.terminated and runner.store.run(run_id)['token_hash'] == ''
    if blocked in {'finish', 'storage'}:
        assert cancelled == [True]
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


def png(color='red'):
    output = BytesIO()
    Image.new('RGB', (4, 4), color).save(output, 'PNG')
    return output.getvalue()


def call(client, run, name='media_list', **args):
    return client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
                       json={'name': name, 'arguments': args})


def outcome_status(response):
    if response.headers.get('content-type', '').startswith('application/json'):
        payload = response.json()
        if isinstance(payload, dict):
            return payload.get('status_code', response.status_code)
    return response.status_code


def capture(app, run, raw=None, name='fixture.png'):
    raw = png() if raw is None else raw
    path = run['id'] + '-captures/' + name
    app.state.store.artifacts.save(path, raw)
    return {'source': 'capture:' + name, 'revision': app.state.store.artifacts.info(path)['revision']}


def share(client, run, source, key='fixture-share'):
    return call(client, run, 'media_share', **source, request_key=key)


@pytest.fixture
def rig(workspace):
    app, client = workspace
    app.state.store.objects = MemoryObjects()
    run = active(app)
    return app, client, run


@pytest.mark.parametrize('harness', ['hermes', 'claude-agent-sdk', 'codex', 'opencode', 'deepagents', 'tool-loop'])
def test_default_broker_discovery_and_resume_are_harness_independent(rig, harness):
    app, client, run = rig
    run = app.state.store.create_run('Harness media fixture', '', 'modal', [], harness=harness)
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    for resumed in (False, True):
        if resumed:
            app.state.store.update_run(run['id'], status='completed', token_hash='')
            assert outcome_status(call(client, run)) == 401
            app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
        response = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'})
        assert outcome_status(response) == 200
        names = {t['name'] for t in response.json()}
        assert set(TOOLS) <= names
        assert not any(n.startswith(('github_', 'slack_', 'linear_', 'notion_')) for n in names)
        assert outcome_status(call(client, run)) == 200
    assert run['plugins'] == []


def test_share_read_revoke_workflow_is_explicit_and_private(rig):
    app, client, run = rig
    source = capture(app, run)
    original = f"/api/runs/{run['id']}/computer/captures/fixture.png"
    assert client.get(original).status_code == 200
    listing = call(client, run).json()
    assert listing['sources'] == [{**source, 'size': len(png())}]
    assert listing['shares'] == []
    assert not app.state.store.rows('SELECT * FROM media_shares')
    response = share(client, run, source)
    assert outcome_status(response) == 200, response.text
    receipt = response.json()
    assert receipt['markdown'].startswith('![Shared image](')
    assert 'Anyone with this link' in receipt['notice']
    assert 'reference' not in receipt and 'token_ciphertext' not in receipt
    token = parse_qs(urlsplit(receipt['url']).query)['token'][0]
    row = app.state.store.rows('SELECT * FROM media_shares')[0]
    assert len(token) == 43 and row['token_hash'] == digest(token)
    assert token not in str(row)
    client.cookies.clear()
    assert client.get(original).status_code == 401
    response = client.get(receipt['url'])
    assert outcome_status(response) == 200 and response.content == png()
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert client.post(receipt['url']).status_code == 405
    for url in (receipt['url'].split('?')[0], receipt['url'] + 'x', '/media/' + 'f'*32 + '?token=' + token):
        bad = client.get(url)
        assert bad.status_code == 404 and bad.headers['cache-control'] == 'no-store'
        assert token not in bad.text
    assert call(client, run, 'media_revoke', share_id=receipt['id']).json()['status'] == 'revoked'
    assert outcome_status(call(client, run, 'media_revoke', share_id=receipt['id'])) == 200
    assert client.get(receipt['url']).status_code == 404
    assert client.head(receipt['url']).status_code == 404
    retry = share(client, run, source).json()
    assert retry['status'] == 'revoked' and 'url' not in retry
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1


def test_immutable_snapshot_retry_conflicts_and_restart(rig):
    app, client, run = rig
    source = capture(app, run)
    receipt = share(client, run, source).json()
    other = capture(app, run, png('blue'))
    assert other['revision'] != source['revision']
    assert client.get(receipt['url']).content == png()
    assert share(client, run, source).json() == receipt
    assert outcome_status(share(client, run, other)) == 409
    assert outcome_status(share(client, run, source, key='stale-source')) == 409
    reopened = Store(app.state.settings.data_dir, object_storage=app.state.store.objects)
    service = MediaShares(reopened, app.state.settings, app.state.security)
    assert service.call(reopened.run(run['id']), 'media_list', {})['shares'] == [receipt]
    service.call(reopened.run(run['id']), 'media_revoke', {'share_id': receipt['id']})
    assert client.get(receipt['url']).status_code == 404


def test_concurrent_retries_publish_one_receipt(rig):
    app, client, run = rig
    source = capture(app, run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: share(client, run, source), range(4)))
    assert all(r.status_code == 200 for r in replies)
    assert len({r.json()['url'] for r in replies}) == 1
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1


def test_cross_session_capabilities_and_deleted_run_fail_closed(rig):
    app, client, run = rig
    source = capture(app, run)
    receipt = share(client, run, source).json()
    other = active(app, 'google:bob')
    assert call(client, other).json()['sources'] == []
    assert call(client, other).json()['shares'] == []
    assert outcome_status(share(client, other, source)) == 404
    assert outcome_status(call(client, other, 'media_revoke', share_id=receipt['id'])) == 404
    for name, args in [('media_list', {}), ('media_share', {**source, 'request_key': 'invalid'}),
                       ('media_revoke', {'share_id': receipt['id']})]:
        response = client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer wrong'},
                               json={'name': name, 'arguments': args})
        assert outcome_status(response) == 401
    app.state.store.execute('UPDATE runs SET deleted_at=? WHERE id=?', ('2026-10-08T12:00:00Z', run['id']))
    assert client.get(receipt['url']).status_code == 404
    assert outcome_status(call(client, run)) == 401


def attach(app, run, raw, *, status='completed', name='fixture.png'):
    store = app.state.store
    from app.attachments import inspect_file
    identity = run['owner_id']
    aid = uuid4().hex
    store.attachments.save(aid, identity, name, raw, inspect_file(raw), 100 * 1024 * 1024)
    message, _ = store.enqueue_message(run['id'], 'Attached fixture', uuid4().hex, user_id=identity, attachment_ids=[aid])
    store.execute('UPDATE messages SET status=? WHERE id=?', (status, message['id']))
    return {'source': 'attachment:' + aid, 'revision': hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize('change,expected', [('turn', 409), ('requester', 409), ('attachment', 404)])
def test_publication_rechecks_authority_after_upload(rig, monkeypatch, change, expected):
    app, client, run = rig
    source = attach(app, run, png())
    store = app.state.store
    put = store.objects.put
    def changed_during_upload(raw):
        reference = put(raw)
        if change == 'turn':
            store.execute('UPDATE runs SET active_message_id=? WHERE id=?', (run['active_message_id'] + 1, run['id']))
        elif change == 'requester':
            store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
        else:
            store.execute("UPDATE messages SET status='deleted' WHERE id=(SELECT message_id FROM attachments WHERE id=?)",
                          (source['source'].split(':')[1],))
        return reference
    monkeypatch.setattr(store.objects, 'put', changed_during_upload)
    assert outcome_status(share(client, run, source)) == expected
    assert not store.rows('SELECT * FROM media_shares')


def test_attachment_scope_and_queued_deleted_rules(rig):
    app, client, run = rig
    allowed = attach(app, run, png())
    queued = attach(app, run, png('blue'), status='queued')
    deleted = attach(app, run, png('green'), status='deleted')
    other = active(app, 'google:bob')
    foreign = attach(app, other, png('yellow'))
    draft_id = uuid4().hex
    from app.attachments import inspect_file
    app.state.store.attachments.save(draft_id, run['owner_id'], 'draft.png', png(), inspect_file(png()), 100000)
    draft = {'source': 'attachment:' + draft_id, 'revision': hashlib.sha256(png()).hexdigest()}
    assert {r['source'] for r in call(client, run).json()['sources']} == {allowed['source']}
    assert outcome_status(share(client, run, allowed)) == 200
    for index, selection in enumerate([queued, deleted, foreign, draft]):
        assert outcome_status(share(client, run, selection, key=str(index))) == 404


@pytest.mark.parametrize('raw,name', [(b'<svg><script>bad()</script></svg>', 'bad.png'),
                                      (b'\x89PNG\r\n\x1a\nhtml', 'bad.png'),
                                      (b'\x1aE\xdf\xa3webm<html>', 'bad.webm'),
                                      (b'<!DOCTYPE html>', 'bad.webm')])
def test_mime_spoofs_never_become_shares(rig, raw, name):
    app, client, run = rig
    response = share(client, run, capture(app, run, raw, name))
    assert outcome_status(response) == 415
    assert not app.state.store.rows('SELECT * FROM media_shares')


@pytest.mark.parametrize('format,mime', [('PNG','image/png'), ('JPEG','image/jpeg'), ('GIF','image/gif'), ('WEBP','image/webp')])
def test_supported_attachment_mimes(format, mime):
    out = BytesIO()
    Image.new('RGB', (2,2), 'red').save(out, format)
    assert validated_mime(out.getvalue()) == mime


@pytest.mark.parametrize('header,status,expected', [('bytes=0-3',206,lambda b:b[:4]),
    ('bytes=-8',206,lambda b:b[-8:]), ('bytes=4-',206,lambda b:b[4:]), ('bytes=0-999999',206,lambda b:b),
    ('bytes=9-4',416,None), ('bytes=-0',416,None), ('bytes=999999-',416,None), ('bytes=0-1,4-5',416,None),
    pytest.param('bytes=' + '9'*5000 + '-',416,None,id='oversized-range')])
def test_video_ranges_and_head(rig, header, status, expected):
    app, client, run = rig
    raw = base64.b64decode((Path(__file__).parent / 'fixtures/media-share.webm.b64').read_text())
    receipt = share(client, run, capture(app, run, raw, 'fixture.webm')).json()
    assert receipt['mime'] == 'video/webm' and receipt['markdown'].startswith('[Shared video](')
    response = client.get(receipt['url'], headers={'Range': header})
    assert outcome_status(response) == status
    if expected:
        assert response.content == expected(raw)
        assert response.headers['accept-ranges'] == 'bytes'
    head = client.head(receipt['url'])
    assert head.status_code == 200 and not head.content and int(head.headers['content-length']) == len(raw)
    assert client.get(receipt['url'], headers={'If-None-Match': '*'}).status_code == 200
    assert client.get(receipt['url'], headers={'Range': 'bytes=0-3', 'If-Range': 'unknown'}).status_code == 200


def test_missing_configuration_storage_failure_limits_and_races(rig, monkeypatch):
    app, client, run = rig
    source = capture(app, run)
    app.state.store.objects.enabled = False
    response = share(client, run, source)
    assert outcome_status(response) == 503 and 'OBJECT_STORAGE_BUCKET' in response.text
    app.state.store.objects.enabled = True
    for origin in ('', 'https://example.com/path', 'https://user:pass@example.com', 'http://example.com', 'https://example.com?query=1'):
        app.state.settings.public_url = origin
        response = share(client, run, source)
        assert outcome_status(response) == 503 and 'PUBLIC_URL' in response.text
    app.state.settings.public_url = 'http://127.0.0.1:8787'
    app.state.store.objects.fail = True
    assert outcome_status(share(client, run, source)) == 503
    app.state.store.objects.fail = False
    with monkeypatch.context() as m:
        m.setattr('app.media_shares.MAX_SESSION_BYTES', 1)
        assert outcome_status(share(client, run, source)) == 413
    with monkeypatch.context() as m:
        m.setattr('app.media_shares.MAX_FILE', 1)
        assert outcome_status(share(client, run, source)) == 413
    put = app.state.store.objects.put
    def cancel_during_upload(raw):
        result = put(raw)
        app.state.store.update_run(run['id'], token_hash='')
        return result
    monkeypatch.setattr(app.state.store.objects, 'put', cancel_during_upload)
    assert outcome_status(share(client, run, source)) == 401
    assert not app.state.store.rows('SELECT * FROM media_shares')


@pytest.mark.parametrize('argument', [{'source': '/etc/passwd'}, {'source': 'capture:../secret.png'},
    {'source': 'https://example.com/image.png'}, {'data': 'aGVsbG8='}, {'run_id': 'a'*32}, {'mime': 'text/html'}])
def test_schema_rejects_arbitrary_sources_and_overrides(rig, argument):
    app, client, run = rig
    source = capture(app, run)
    assert outcome_status(call(client, run, 'media_share', **{**source, 'request_key': 'bad', **argument})) == 422


def test_revocation_during_read_and_storage_integrity_fail_closed(rig, monkeypatch):
    app, client, run = rig
    receipt = share(client, run, capture(app, run)).json()
    read = app.state.store.objects.read
    def revoke_during_read(reference, limit):
        raw = read(reference, limit)
        app.state.store.execute('UPDATE media_shares SET revoked_at=? WHERE id=?', ('revoked', receipt['id']))
        return raw
    monkeypatch.setattr(app.state.store.objects, 'read', revoke_during_read)
    assert client.get(receipt['url']).status_code == 404
    monkeypatch.setattr(app.state.store.objects, 'read', lambda *_: b'wrong data')
    app.state.store.execute("UPDATE media_shares SET revoked_at='' WHERE id=?", (receipt['id'],))
    assert client.get(receipt['url']).status_code == 503


def test_access_logs_never_record_bearer_query(rig):
    app, client, run = rig
    receipt = share(client, run, capture(app, run)).json()
    token = parse_qs(urlsplit(receipt['url']).query)['token'][0]
    for status in (200, 206, 404, 416):
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1, '%s - "%s %s HTTP/%s" %d',
                                   ('client', 'GET', receipt['url'], '1.1', status), None)
        assert RedactQueryStrings().filter(record)
        assert token not in record.getMessage() and '?token' not in record.getMessage()


@pytest.mark.parametrize('harness', ['stdio', 'deepagents', 'tool-loop'])
def test_mcp_transport_discovers_and_executes_media_with_actionable_errors(rig, harness):
    import asyncio
    from http.server import BaseHTTPRequestHandler
    import json
    import os
    import subprocess
    import sys
    from test_broker_transport import diagnostic_relay
    app, client, run = rig
    source = capture(app, run)
    app.state.store.update_run(run['id'], token_hash=digest('private-capability'))
    # The same stdio MCP is consumed by Hermes, Claude, Codex and OpenCode;
    # in-process harnesses wrap it with workspace_tools/workspace_call.
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, response):
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            self.reply(client.get('/broker/' + run['id'] + self.path,
                       headers={'Authorization': self.headers['Authorization']}))

        def do_POST(self):
            self.reply(client.post('/broker/' + run['id'] + self.path,
                       content=self.rfile.read(int(self.headers['Content-Length'])),
                       headers={key: self.headers[key] for key in ('Authorization', 'Content-Type')}))

    script = Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py'
    with diagnostic_relay(Edge) as (relay, _, diagnostics):
        env = {'PATH': os.environ['PATH'], 'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': 'private-capability'}
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                     'params': {'name': 'media_share', 'arguments': {**source, 'request_key': 'mcp-share'}}}]
        if harness == 'stdio':
            def invoke():
                result = subprocess.run([sys.executable, str(script)], env=env, text=True, capture_output=True,
                    input='\n'.join(json.dumps(m) for m in messages) + '\n', timeout=20)
                assert result.returncode == 0, result.stderr
                return [json.loads(line)['result'] for line in result.stdout.splitlines()]
        else:
            from sandbox.harness_bindings import RUNTIME_BINDINGS
            config = {'mcp_servers': {'workspace': {'command': sys.executable, 'args': [str(script)], 'env': env}}}
            discover, execute = RUNTIME_BINDINGS[harness].tools('/workspace', config)[:2]
            async def run_calls():
                return [json.loads(await discover()), json.loads(await execute('media_share', json.dumps(messages[1]['params']['arguments'])))]
            def invoke():
                return asyncio.run(run_calls())
        app.state.store.objects.enabled = False
        catalog, failure = invoke()
        assert 'media_share' in json.dumps(catalog)
        assert failure['isError'] and 'OBJECT_STORAGE_BUCKET' in json.dumps(failure)
        assert not relay.last_error and not relay.uncertain_tool and not diagnostics
        app.state.store.objects.enabled = True
        _, success = invoke()
        assert not success['isError']
        receipt = json.loads(success['content'][0]['text'])
        assert client.get(receipt['url']).content == png()


def test_lifetime_count_and_byte_quotas_survive_revocation(rig, monkeypatch):
    app, client, run = rig
    source = capture(app, run)
    receipt = share(client, run, source).json()
    call(client, run, 'media_revoke', share_id=receipt['id'])
    for name, limit in [('MAX_SESSION_SHARES', 1), ('MAX_SESSION_BYTES', len(png()))]:
        with monkeypatch.context() as m:
            m.setattr('app.media_shares.' + name, limit)
            assert outcome_status(share(client, run, source, 'another-share')) == 413
    for name, value in [('media_share_receipt_limit', 1), ('media_share_storage_limit_mb', 0)]:
        with monkeypatch.context() as m:
            m.setattr(app.state.settings, name, value)
            assert outcome_status(share(client, run, source, 'another-share')) == 413
    assert share(client, run, source).json()['status'] == 'revoked'
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1


def test_legacy_capture_symlinks_and_source_replacement_fail_closed(workspace, monkeypatch, tmp_path):
    app, client = workspace
    run = active(app)
    source = capture(app, run)
    path = app.state.store.artifacts.path(run['id'] + '-captures/fixture.png')
    outside = tmp_path / 'outside.png'
    outside.write_bytes(png())
    path.unlink()
    path.symlink_to(outside)
    app.state.store.objects = MemoryObjects()
    assert outcome_status(share(client, run, source)) == 404
    path.unlink()
    path.write_bytes(png())
    source['revision'] = app.state.store.artifacts.info(run['id'] + '-captures/fixture.png')['revision']
    read = app.state.store.artifacts.read
    def replacing_read(*args, **kwargs):
        raw = read(*args, **kwargs)
        path.write_bytes(png('blue'))
        return raw
    monkeypatch.setattr(app.state.store.artifacts, 'read', replacing_read)
    assert outcome_status(share(client, run, source)) == 409
    assert not app.state.store.rows('SELECT * FROM media_shares')


async def test_checkpoint_restores_receipt_and_revocation_with_original_storage_and_key(rig, tmp_path):
    from app.persistence import Checkpoints, restore_checkpoint
    from app.security import Security
    app, client, run = rig
    receipt = share(client, run, capture(app, run)).json()
    settings = app.state.settings.model_copy(update={'checkpoint_dir': tmp_path / 'checkpoint',
        'encryption_key': (app.state.settings.data_dir / 'encryption.key').read_text()})
    async def commit():
        pass
    checkpoints = Checkpoints(app.state.store, settings, commit=commit)
    await checkpoints.flush()
    restored_settings = settings.model_copy(update={'data_dir': tmp_path / 'restored'})
    restore_checkpoint(restored_settings)
    restored = Store(restored_settings.data_dir, object_storage=app.state.store.objects)
    service = MediaShares(restored, restored_settings, Security(restored_settings))
    restored_run = restored.run(run['id'])
    assert service.call(restored_run, 'media_list', {})['shares'] == [receipt]
    service.call(restored_run, 'media_revoke', {'share_id':receipt['id']})
    await Checkpoints(restored, restored_settings, commit=commit).flush()
    final_settings = settings.model_copy(update={'data_dir': tmp_path / 'final'})
    restore_checkpoint(final_settings)
    final_store = Store(final_settings.data_dir, object_storage=app.state.store.objects)
    final_service = MediaShares(final_store, final_settings, Security(final_settings))
    assert final_service.call(final_store.run(run['id']), 'media_list', {})['shares'][0]['status'] == 'revoked'


def test_checkpoint_failure_is_not_acknowledged_and_retry_recovers_receipt(rig, monkeypatch):
    app, client, run = rig
    source = capture(app, run)
    from app.persistence import Checkpoints
    original = Checkpoints.flush
    async def fail(self):
        raise RuntimeError('fixture checkpoint unavailable')
    monkeypatch.setattr(Checkpoints, 'flush', fail)
    # The boundary middleware converts unconfirmed persistence to a 503.
    with pytest.raises(RuntimeError, match='fixture checkpoint unavailable'):
        share(client, run, source)
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1
    monkeypatch.setattr(Checkpoints, 'flush', original)
    response = share(client, run, source)
    assert response.status_code == 200
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1


@pytest.mark.parametrize('mutation', ['truncated', 'doctype', 'codec', 'empty-elements'])
def test_invalid_webm_structures_are_rejected(mutation):
    raw = base64.b64decode((Path(__file__).parent / 'fixtures/media-share.webm.b64').read_text())
    if mutation == 'truncated':
        raw = raw[:-1]
    elif mutation == 'doctype':
        raw = raw.replace(b'webm', b'html')
    elif mutation == 'codec':
        raw = raw.replace(b'V_VP8', b'V_BAD')
    else:
        raw = bytes.fromhex('1a45dfa387428284') + b'webm' + bytes.fromhex('185380678f1549a966801654ae6b801f43b67580')
    with pytest.raises(HTTPException) as rejected:
        validated_mime(raw)
    assert rejected.value.status_code == 415


def test_concurrent_new_shares_obey_quota_and_listing_paginates(rig, monkeypatch):
    app, client, run = rig
    first = capture(app, run, name='a.png')
    second = capture(app, run, name='b.png')
    page = call(client, run, limit=1).json()
    assert page['sources'][0]['source'] == first['source'] and page['next_offset'] == 1
    last = call(client, run, limit=1, offset=page['next_offset']).json()
    assert last['sources'][0]['source'] == second['source'] and last['next_offset'] is None
    monkeypatch.setattr('app.media_shares.MAX_SESSION_SHARES', 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: share(client, run, first, str(n)), range(4)))
    assert sorted(outcome_status(r) for r in results) == [200, 413, 413, 413]
    assert len(app.state.store.rows('SELECT * FROM media_shares')) == 1
