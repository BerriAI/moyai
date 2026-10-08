import base64
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from modal.exception import NotFoundError

from app import captures
from app.computer import BROWSER_PARTIAL_NOTICE, DesktopConnection
from sandbox import computer
from test_workspace import workspace
from storage_fixture import MemoryObjects

PNG = b'\x89PNG\r\n\x1a\n' + b'capture bytes'
WEBM = b'\x1aE\xdf\xa3\x42\x82\x84webm' + bytes(range(100))


def cloud(workspace):
    app, client = workspace
    run = app.state.store.create_run('Browser test', '', 'modal', [], chat_enabled=True,
                                    user_id='google:owner')
    return app, client, run['id'], f"/api/runs/{run['id']}/computer"


def browser_sandbox(run_id, transport):
    async def request(body):
        operation = body.get('args', {}).get('browser')
        if operation:
            assert body['action'] == 'state' and body['args']['scope'] == run_id
            return {'scope': run_id, **({'restored': True} if operation == 'restore' else {'state': None})}
        return await transport(body)
    return SimpleNamespace(object_id='test-sandbox', computer_request=request)


def test_preview_control_requires_login_csrf_and_requester(workspace, monkeypatch):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    hub.sandbox = AsyncMock(return_value=None)
    assert client.get(url).json()['available'] is False
    assert client.post(url, json={'action': 'claim'}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post(url, json={'action': 'claim'}).status_code == 409
    assert client.post(url, json={'action': 'claim', 'actor': 'google:owner'}).status_code == 422
    assert client.post(url, json={'action': 'capture', 'args': {}}).status_code == 422
    monkeypatch.setattr(app.state.security, 'role', lambda _: 'member')
    assert client.get(url).status_code == 403
    monkeypatch.setattr(app.state.store, 'identity', lambda _: 'google:owner')
    assert client.get(url).status_code == 200
    client.cookies.clear()
    assert client.get(url).status_code == 401


@pytest.mark.parametrize('remote', [False, True])
def test_saved_media_survives_sleep_supports_ranges_and_blocks_paths(workspace, remote):
    app, client, rid, url = cloud(workspace)
    store = app.state.store
    if remote:
        store.objects = MemoryObjects()
    directory = captures.directory(app.state.settings, rid)
    directory.mkdir(parents=True)
    for name, raw in [('image-a.png', PNG), ('flow-b.webm', WEBM), ('evil.png', b'<html><script>alert(1)</script>')]:
        store.artifacts.save(rid + '-captures/' + name, raw)
    (directory/'link.png').symlink_to(directory/'image-a.png')
    listing = client.get(f'/api/runs/{rid}/files').json()['files']
    assert {f['name'] for f in listing} == {'image-a.png','flow-b.webm','evil.png'}
    assert client.get(f'/api/runs/{rid}').json()['has_captures']
    if remote:
        assert store.objects.reads == 0
        assert not (directory / 'flow-b.webm').exists()
    media = url + '/captures/flow-b.webm'
    response = client.get(media)
    assert response.content == WEBM
    assert response.headers['content-type'] == 'video/webm'
    assert response.headers['cache-control'] == 'no-store'
    assert client.get(media+'?download=true').headers['content-disposition'].startswith('attachment')
    piece = client.get(media, headers={'Range':'bytes=10-19'})
    assert piece.status_code == 206 and piece.content == WEBM[10:20]
    assert piece.headers['content-range'] == f'bytes 10-19/{len(WEBM)}'
    assert client.get(media, headers={'Range':'bytes=-7'}).content == WEBM[-7:]
    for value in ['bytes=99999-', 'bytes=20-10', 'bytes=-0', 'bytes=0-2,4-8', 'garbage']:
        assert client.get(media, headers={'Range':value}).status_code == 416
    assert client.get(url+'/captures/evil.png').status_code == 415
    assert client.get(url+'/captures/link.png').status_code == 404
    assert client.get(url+'/captures/..%2Fworkspace.db').status_code == 404
    other = store.create_run('Other capture scope', '', 'demo', [])['id']
    assert client.get(f'/api/runs/{other}/computer/captures/flow-b.webm').status_code == 404
    if remote:
        store.objects.fail = True
        assert client.get(media).status_code == 503
        assert len(client.get(f'/api/runs/{rid}/files').json()['files']) == 3
    client.cookies.clear()
    assert client.get(media).status_code == 401


@pytest.mark.asyncio
async def test_browser_checkpoint_is_encrypted_private_and_scoped_to_its_run(workspace):
    app, client, rid, url = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    state = {'storage': {'cookies': [{'name': 'session', 'value': 'private-login-marker'}], 'origins': []},
             'pages': [{'url': 'https://example.test/account', 'session_storage': {}}], 'active': 0}
    async def exchange(body):
        return {'scope': rid, **({'state': state} if body['args']['browser'] == 'checkpoint' else {'restored': True})}
    transport = AsyncMock(side_effect=exchange)
    sandbox = SimpleNamespace(object_id='sb-browser', computer_request=transport)
    store.update_run(rid, sandbox_id=sandbox.object_id)
    await hub.checkpoint(sandbox, rid)
    encrypted = store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    assert 'private-login-marker' not in encrypted
    assert json.loads(hub.security.decrypt(encrypted)) == {'scope': rid, 'state': state}
    await hub.restore(sandbox, rid)
    assert transport.call_args.args[0] == {'action': 'state', 'args': {'browser': 'restore', 'scope': rid, 'state': state}}
    child = store.create_run('Child browser', '', 'modal', [], chat_enabled=True)['id']
    store.execute('UPDATE runs SET parent_run_id=?,snapshot_id=?,sandbox_id=? WHERE id=?',
                  (rid, 'parent-snapshot', 'sb-child', child))
    child_transport = AsyncMock(return_value={'scope': child, 'restored': True})
    await hub.restore(SimpleNamespace(object_id='sb-child', computer_request=child_transport), child)
    assert child_transport.call_args.args[0]['args'] == {'browser': 'restore', 'scope': child, 'state': None}
    hub.sandbox = AsyncMock(return_value=None)
    for response in (client.get(url), client.get('/api/runs/' + rid), client.get('/api/runs')):
        assert response.status_code == 200
        assert 'private-login-marker' not in response.text and encrypted not in response.text
    for action in ('checkpoint', 'restore'):
        assert client.post(url, json={'action': action}).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['absent', 'late', 'deleted', 'logout'])
async def test_browser_checkpoint_keeps_good_state_until_current_browser_explicitly_clears_it(workspace, reply):
    app, _, rid, _ = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    state = {'storage': {'cookies': [{'name': 'session', 'value': 'keep-login'}], 'origins': []},
             'pages': [], 'active': 0}
    original = hub.security.encrypt(json.dumps({'scope': rid, 'state': state}))
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, original))
    store.update_run(rid, sandbox_id='sb-browser')
    empty = {'storage': {'cookies': [], 'origins': []}, 'pages': [], 'active': 0}
    async def checkpoint(body):
        assert body == {'action': 'state', 'args': {'browser': 'checkpoint', 'scope': rid}}
        if reply == 'late':
            store.update_run(rid, sandbox_id='sb-replacement')
        elif reply == 'deleted':
            store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (rid,))
        return {'scope': rid, 'state': None if reply == 'absent' else empty}
    await hub.checkpoint(SimpleNamespace(object_id='sb-browser', computer_request=checkpoint), rid)
    saved = store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    if reply == 'logout':
        assert json.loads(hub.security.decrypt(saved)) == {'scope': rid, 'state': empty}
    else:
        assert saved == original


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['transport', 'error_reply'])
async def test_browser_checkpoint_failure_preserves_previous_login(workspace, failure):
    app, _, rid, _ = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    state = {'storage': {'cookies': [{'name': 'session', 'value': 'keep-login'}], 'origins': []},
             'pages': [], 'active': 0}
    original = hub.security.encrypt(json.dumps({'scope': rid, 'state': state}))
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, original))
    store.update_run(rid, sandbox_id='sb-browser')
    transport = AsyncMock(side_effect=ConnectionError('lost reply')) if failure == 'transport' else AsyncMock(
        return_value={'error': 'private runtime diagnostic'})
    sandbox = SimpleNamespace(object_id='sb-browser', computer_request=transport)
    with pytest.raises(HTTPException) as error:
        await hub.checkpoint(sandbox, rid)
    assert 'private runtime diagnostic' not in str(error.value)
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['ciphertext', 'json', 'scope'])
async def test_corrupt_browser_restore_fails_without_sending_fresh_state(workspace, fault):
    app, _, rid, _ = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    state = {'storage': {'cookies': [], 'origins': []}, 'pages': [], 'active': 0}
    raw = 'not-json' if fault == 'json' else json.dumps({'scope': 'another-run', 'state': state})
    encrypted = 'invalid-ciphertext' if fault == 'ciphertext' else hub.security.encrypt(raw)
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, encrypted))
    store.update_run(rid, sandbox_id='sb-browser')
    transport = AsyncMock(return_value={'scope': rid, 'restored': True})
    with pytest.raises(HTTPException) as error:
        await hub.restore(SimpleNamespace(object_id='sb-browser', computer_request=transport), rid)
    assert error.value.status_code == 503
    assert transport.call_args.args[0] == {'action': 'state',
        'args': {'browser': 'restore', 'scope': rid, 'blocked': True}}
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == encrypted


def test_browser_restore_get_preserves_error_until_row_or_sandbox_changes(workspace, monkeypatch):
    app, client, rid, url = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    clock = [100]
    monkeypatch.setattr('app.computer.time', SimpleNamespace(monotonic=lambda: clock[0]))
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, 'invalid-ciphertext'))
    store.update_run(rid, sandbox_id='sb-first')
    state = {'storage': {'cookies': [], 'origins': []}, 'pages': [], 'active': 0}
    async def exchange(body):
        operation = body.get('args', {}).get('browser')
        if operation == 'restore':
            return {'scope': rid, 'restored': True}
        if operation == 'checkpoint':
            return {'scope': rid, 'state': None}
        return {'available': True, 'frame': 'recovered'}
    transport = AsyncMock(side_effect=exchange)
    sandbox = SimpleNamespace(object_id='sb-first', computer_request=transport)
    hub.sandbox = AsyncMock(return_value=sandbox)
    detail = {'detail': 'Saved browser access could not be restored. The checkpoint is preserved.'}
    for _ in range(3):
        response = client.get(url)
        assert response.status_code == 503 and response.json() == detail
        clock[0] += .1
    transport.assert_awaited_once_with({'action': 'state',
        'args': {'browser': 'restore', 'scope': rid, 'blocked': True}})
    assert hub.cache == {}
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == 'invalid-ciphertext'

    sandbox.object_id = 'sb-replacement'
    store.update_run(rid, sandbox_id=sandbox.object_id)
    response = client.get(url)
    assert response.status_code == 503 and response.json() == detail
    assert transport.await_count == 2  # A replacement must receive its own block marker.

    encrypted = hub.security.encrypt(json.dumps({'scope': rid, 'state': state}))
    store.execute('UPDATE browser_sessions SET encrypted=? WHERE run_id=?', (encrypted, rid))
    response = client.get(url)
    assert response.status_code == 200 and response.json()['frame'] == 'recovered'
    assert transport.await_args_list[2].args[0] == {'action': 'state',
        'args': {'browser': 'restore', 'scope': rid, 'state': state}}
    assert rid not in hub.restore_failures
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == encrypted


@pytest.mark.parametrize('failure', ['legacy', 'error', 'transport'])
def test_browser_restore_get_delays_only_known_failures(workspace, monkeypatch, failure):
    app, client, rid, url = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    clock = [100]
    monkeypatch.setattr('app.computer.time', SimpleNamespace(monotonic=lambda: clock[0]))
    state = {'storage': {'cookies': [], 'origins': []}, 'pages': [], 'active': 0}
    encrypted = hub.security.encrypt(json.dumps({'scope': rid, 'state': state}))
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, encrypted))
    store.update_run(rid, sandbox_id='sb-browser')
    reply = {'available': True} if failure == 'legacy' else {'error': 'private runtime diagnostic'}
    transport = AsyncMock(return_value=reply, side_effect=ConnectionError('lost reply') if failure == 'transport' else None)
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(object_id='sb-browser', computer_request=transport))
    expected = ('The workspace browser needs an update to restore saved access.' if failure == 'legacy' else
                'Browser access could not be restored. The checkpoint is preserved.')
    if failure == 'transport':
        expected = 'Computer is reconnecting or this sandbox has shut down. Saved captures are still available.'
    response = client.get(url)
    assert response.status_code == 503 and response.json() == {'detail': expected}
    if failure == 'transport':
        assert rid not in hub.restore_failures
    else:
        assert encrypted not in hub.restore_failures[rid]
        clock[0] += 9.9
        assert client.get(url).json() == {'detail': expected}
        clock[0] += .2
    assert transport.await_count == 1 and hub.cache == {}

    transport.side_effect = [{'scope': rid, 'restored': True}, {'available': True, 'frame': 'recovered'},
                             {'scope': rid, 'state': None}]
    response = client.get(url)
    assert response.status_code == 200 and response.json()['frame'] == 'recovered'
    assert transport.await_count == 4 and rid not in hub.restore_failures
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == encrypted


@pytest.mark.asyncio
async def test_wrong_scope_checkpoint_cannot_replace_saved_browser_auth(workspace):
    app, _, rid, _ = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    state = {'storage': {'cookies': [{'name': 'session', 'value': 'keep-login'}], 'origins': []},
             'pages': [], 'active': 0}
    original = hub.security.encrypt(json.dumps({'scope': rid, 'state': state}))
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, original))
    store.update_run(rid, sandbox_id='sb-browser')
    transport = AsyncMock(return_value={'scope': 'another-run',
        'state': {'storage': {'cookies': [], 'origins': []}, 'pages': [], 'active': 0}})
    await hub.checkpoint(SimpleNamespace(object_id='sb-browser', computer_request=transport), rid)
    assert store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == original


@pytest.mark.parametrize('owner', ['poll', 'shutdown'])
async def test_partial_checkpoint_is_saved_and_reported_to_its_owner(workspace, owner):
    app, client, rid, url = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    saved = {'storage': {'cookies': [{'name': 'session', 'value': 'partial-login'}], 'origins': []}, 'pages': [], 'active': 0}
    async def exchange(body):
        operation = body.get('args', {}).get('browser')
        if operation == 'checkpoint':
            assert body['args'].get('releasing', False) == (owner == 'shutdown')
            return {'scope': rid, 'state': saved, 'partial': True}
        if operation == 'restore':
            return {'scope': rid, 'restored': True}
        return {'available': True, 'frame': 'live'}
    sandbox = SimpleNamespace(object_id='sb-partial', computer_request=exchange)
    store.update_run(rid, sandbox_id=sandbox.object_id)
    hub.sandbox = AsyncMock(return_value=sandbox)
    hub.execute, hub.sync = AsyncMock(return_value={}), AsyncMock()
    if owner == 'poll':
        response = client.get(url)
        assert response.status_code == 200 and response.json()['frame'] == 'live'
        assert response.json()['notice'] == BROWSER_PARTIAL_NOTICE
    else:
        await hub.save_captures(sandbox, rid, releasing=True)
        assert any(event['message'] == BROWSER_PARTIAL_NOTICE for event in store.events(rid))
    encrypted = store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    assert json.loads(hub.security.decrypt(encrypted)) == {'scope': rid, 'state': saved}


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [{'error': 'Browser checkpoint belongs to another session.'}, {'available': True}])
async def test_restore_without_saved_row_distinguishes_explicit_fault_from_legacy_state(workspace, reply):
    app, _, rid, _ = cloud(workspace)
    hub = app.state.computer
    sandbox = SimpleNamespace(object_id='sb-browser', computer_request=AsyncMock(return_value=reply))
    if 'error' in reply:
        with pytest.raises(HTTPException) as error:
            await hub.restore(sandbox, rid)
        assert error.value.status_code == 503
    else:
        await hub.restore(sandbox, rid)
    assert not app.state.store.rows('SELECT * FROM browser_sessions WHERE run_id=?', (rid,))


def test_deleting_session_removes_browser_authentication_for_parent_and_children(workspace):
    from test_agent_sidebar import seeded_group
    app, client = workspace
    parent, child, _ = seeded_group(app)
    for rid in (parent, child):
        app.state.store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)', (rid, 'encrypted-auth'))
    assert client.delete('/api/runs/' + parent).status_code == 200
    assert not app.state.store.rows('SELECT * FROM browser_sessions WHERE run_id IN (?,?)', (parent, child))


@pytest.mark.asyncio
@pytest.mark.parametrize('remote', [False, True])
async def test_sync_is_idempotent_and_rejects_bad_or_oversized_media(workspace, monkeypatch, remote):
    app, _, rid, _ = cloud(workspace)
    if remote:
        app.state.store.objects = MemoryObjects()
    hub = app.state.computer
    hub.execute = AsyncMock(return_value={'data': base64.b64encode(PNG).decode()})
    hub.manager.persist = AsyncMock()
    rows = [{'name':'proof.png','size':len(PNG)}]
    await hub.sync(object(), rid, rows)
    await hub.sync(object(), rid, rows)
    assert hub.execute.await_count == 1
    assert hub.manager.persist.await_count == 1
    assert captures.read(captures.directory(app.state.settings, rid) / 'proof.png', store=app.state.store)[0] == PNG
    if remote:
        assert not captures.directory(app.state.settings, rid).exists()
    await hub.sync(object(),rid,[{'name':'../escape.png','size':1}, {'name':'huge.webm','size':captures.MAX_FILE+1}])
    assert hub.execute.await_count == 1
    hub.execute.return_value = {'data':base64.b64encode(b'not an image').decode()}
    with pytest.raises(Exception,match='supported image'):
        await hub.sync(object(),rid,[{'name':'invalid.png','size':len(b'not an image')}])
    assert not (captures.directory(app.state.settings,rid)/'invalid.png').exists()
    if remote:
        app.state.store.objects.fail = True
        hub.execute.return_value = {'data': base64.b64encode(PNG).decode()}
        with pytest.raises(HTTPException, match='unavailable'):
            await hub.sync(object(), rid, [{'name': 'retry.png', 'size': len(PNG)}])
        assert app.state.store.artifacts.info(rid + '-captures/retry.png') is None
        assert not captures.directory(app.state.settings, rid).exists()
        app.state.store.objects.fail = False
    monkeypatch.setattr(captures, 'MAX_TOTAL', len(PNG))
    hub.execute.return_value = {'data': base64.b64encode(PNG).decode()}
    with pytest.raises(HTTPException, match='saved capture budget'):
        await hub.sync(object(), rid, [{'name': 'over-budget.png', 'size': len(PNG)}])


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('same_name', [False, True])
async def test_cancelled_capture_save_cannot_overwrite_or_exceed_shared_budget(workspace, monkeypatch, remote, same_name):
    from app import blob_storage
    app, _, rid, _ = cloud(workspace)
    store, hub = app.state.store, app.state.computer
    if remote:
        store.objects = MemoryObjects()
    first_raw, second_raw = PNG + b'first', PNG + b'other'
    first_name, second_name = 'first.png', 'first.png' if same_name else 'second.png'
    prefix = rid + '-captures/'
    monkeypatch.setattr(captures, 'MAX_TOTAL', len(first_raw))
    hub.manager.persist = AsyncMock()
    hub.execute = AsyncMock(side_effect=[{'data': base64.b64encode(raw).decode()} for raw in (first_raw, second_raw)])
    started, release, second_started, finished = (threading.Event() for _ in range(4))
    outcomes = {}
    real_save = store.artifacts.save
    real_listing = store.artifacts.listing
    second_thread = None

    def observed_save(name, raw, **kwargs):
        nonlocal second_thread
        if raw == second_raw:
            second_thread = threading.get_ident()
        try:
            outcomes[raw] = real_save(name, raw, **kwargs)
            return outcomes[raw]
        except HTTPException as exc:
            outcomes[raw] = exc
            raise
        finally:
            if raw == first_raw:
                finished.set()

    def observed_listing(prefix, conn=None):
        result = real_listing(prefix, conn)
        if conn is not None and threading.get_ident() == second_thread:
            second_started.set()
        return result

    monkeypatch.setattr(store.artifacts, 'save', observed_save)
    monkeypatch.setattr(store.artifacts, 'listing', observed_listing)
    if remote:
        real_put = store.objects.put

        def blocked_put(raw):
            if raw == first_raw:
                started.set()
                assert release.wait(5), 'Cancelled upload was never released'
            return real_put(raw)

        monkeypatch.setattr(store.objects, 'put', blocked_put)
    else:
        real_fsync = blob_storage.os.fsync

        def blocked_fsync(descriptor):
            if not started.is_set():
                started.set()
                assert release.wait(5), 'Cancelled local save was never released'
            return real_fsync(descriptor)

        monkeypatch.setattr(blob_storage.os, 'fsync', blocked_fsync)
    first = asyncio.create_task(hub.sync(object(), rid, [{'name': first_name, 'size': len(first_raw)}]))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert store.artifacts.listing(prefix) == [], 'Unpublished staging bytes are not capture inventory'
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(hub.sync(object(), rid, [{'name': second_name, 'size': len(second_raw)}]))
        assert await asyncio.to_thread(second_started.wait, 5)
        if remote:
            # A blocked remote upload must not hold SQLite's writer lock.
            await asyncio.wait_for(second, 5)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    if not remote:
        if same_name:
            await second
        else:
            with pytest.raises(HTTPException, match='saved capture budget'):
                await second
    rejected = outcomes[first_raw if remote else second_raw]
    assert rejected is False if same_name else isinstance(rejected, HTTPException) and rejected.status_code == 413
    rows = store.artifacts.listing(prefix)
    assert len(rows) == 1 and sum(row['size'] for row in rows) == len(first_raw)
    winner = second_raw if remote else first_raw
    assert store.artifacts.read(rows[0]['name'], captures.MAX_FILE) == winner


@pytest.mark.asyncio
async def test_human_control_lease_blocks_agent_and_expires(monkeypatch,tmp_path):
    monkeypatch.setattr(computer,'CAPTURES',tmp_path/'captures')
    c = computer.Computer()
    c.open=AsyncMock()
    c.desktop_image=lambda image_format='JPEG': PNG
    c.page=SimpleNamespace(is_closed=lambda:False,url='about:blank',screenshot=AsyncMock(return_value=PNG))
    clock=[100]
    monkeypatch.setattr(computer.time,'monotonic',lambda:clock[0])
    await c.command({'action':'claim','actor':'google:owner'})
    with pytest.raises(ValueError,match='Another person'):
        await c.command({'action':'claim','actor':'google:other'})
    with pytest.raises(ValueError,match='person has control'):
        await c.command({'action':'screenshot','args':{'name':'agent'}})
    result=await c.command({'action':'screenshot','actor':'google:owner','args':{'name':'../../capture'}})
    assert Path(result['path']).parent == tmp_path/'captures'
    assert Path(result['path']).read_bytes() == PNG
    await c.command({'action':'release','actor':'google:other'})
    assert c.controls() == 'google:owner'
    clock[0]=161
    assert c.controls() == ''
    await c.command({'action':'screenshot','args':{'name':'agent'}})
    with pytest.raises(ValueError,match='signed-in'):
        await c.command({'action':'claim'})


@pytest.mark.asyncio
async def test_recording_only_appears_after_finalize_and_is_idempotent(monkeypatch,tmp_path):
    monkeypatch.setattr(computer,'CAPTURES',tmp_path)
    c=computer.Computer()
    path=tmp_path/'flow.partial'
    path.write_bytes(WEBM)
    proc=SimpleNamespace(returncode=0,wait=AsyncMock(return_value=0))
    c.recording={'path':path,'process':proc,'started':computer.time.time()}
    assert c.media() == []
    result=await c.command({'action':'finish'})
    assert result['name']=='flow.webm'
    assert c.media()[0]['name']=='flow.webm'
    assert (await c.command({'action':'finish'})) == {'recording':False}
    bad=tmp_path/'bad.partial';bad.write_bytes(b'broken')
    c.recording={'path':bad,'process':proc,'started':computer.time.time()}
    with pytest.raises(RuntimeError,match='did not finalize'):
        await c.command({'action':'finish'})
    assert not (tmp_path/'bad.webm').exists()


def test_capture_paths_and_urls_cannot_escape_workspace(monkeypatch,tmp_path):
    root=tmp_path/'captures'
    outside=tmp_path/'outside';outside.mkdir()
    monkeypatch.setattr(computer,'CAPTURES',root)
    root.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):computer.capture_list()
    root.unlink();root.mkdir()
    (outside/'secret.png').write_bytes(PNG)
    (root/'link.png').symlink_to(outside/'secret.png')
    with pytest.raises(OSError):computer.capture_read('link.png')
    with pytest.raises(ValueError):computer.capture_read('../outside/secret.png')
    for url in ['file:///etc/passwd','javascript:alert(1)','https://user:pass@example.com']:
        with pytest.raises(ValueError):computer.valid_url(url)
    assert computer.valid_url('http://localhost:3000') == 'http://localhost:3000'


@pytest.mark.asyncio
async def test_restored_display_removes_stale_files_but_preserves_live_server(monkeypatch):
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix='moyai-x-',dir='/tmp') as folder:
        endpoint=Path(folder)/'X99'
        lock=Path(folder)/'.X99-lock'
        monkeypatch.setattr(computer,'DISPLAY_SOCKET',endpoint)
        monkeypatch.setattr(computer,'DISPLAY_LOCK',lock)
        # A filesystem snapshot includes a dead socket and the old PID lock.
        stale=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        stale.bind(str(endpoint));stale.close()
        lock.write_text('old-pid')
        servers=[]
        def launch(*args,**kwargs):
            assert not endpoint.exists() and not lock.exists()
            server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            server.bind(str(endpoint));server.listen(5);servers.append(server)
            return SimpleNamespace(poll=lambda:None)
        monkeypatch.setattr(computer.subprocess,'Popen',launch)
        try:
            c=computer.Computer()
            await c.ensure_display()
            assert len(servers)==1 and computer.display_alive()
            # A service restart must reuse a live display, not unlink it.
            await computer.Computer().ensure_display()
            assert len(servers)==1 and endpoint.exists()
        finally:
            for server in servers:server.close()


@pytest.mark.asyncio
async def test_desktop_batches_validate_before_execution_and_preserve_native_order(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    c.ensure_desktop = AsyncMock()
    c.open = AsyncMock()
    c.ensure_desktop = AsyncMock()
    c.refresh_frame = AsyncMock()
    sent = []
    async def launch(*args, **kwargs):
        assert args[0] == 'xdotool' and kwargs['env']['DISPLAY'] == ':99'
        assert kwargs['stdin'] == computer.asyncio.subprocess.PIPE
        async def communicate(text):
            sent.append((list(args[1:]), text))
        return SimpleNamespace(communicate=communicate, returncode=0)
    monkeypatch.setattr(computer.asyncio, 'create_subprocess_exec', launch)
    events = [{'type': 'key', 'key': 'Control+l'}, {'type': 'text', 'text': 'person@example.com'},
              {'type': 'key', 'key': 'Enter'}, {'type': 'pointer', 'phase': 'down', 'button': 0, 'x': 22, 'y': 88},
              {'type': 'pointer', 'phase': 'move', 'x': 45, 'y': 90}, {'type': 'pointer', 'phase': 'up', 'button': 0}]
    with pytest.raises(ValueError, match='Take control'):
        await c.command({'actor': 'owner', 'action': 'input', 'args': {'events': events}})
    await c.command({'actor': 'owner', 'action': 'claim'})
    with pytest.raises(ValueError, match='person has control'):
        await c.command({'action': 'input', 'args': {'events': events}})
    for invalid in [{'type': 'key', 'key': 'exec+curl'}, {'type': 'pointer', 'phase': 'move', 'x': float('nan'), 'y': 3},
                    {'type': 'text', 'text': 'x'*10001}, {'type': 'scroll', 'dy': '1'}, {'type': 'unknown'}]:
        with pytest.raises(ValueError):
            await c.command({'actor': 'owner', 'action': 'input', 'args': {'events': [events[0], invalid]}})
    assert sent == [] and c.ensure_desktop.await_count == 0
    result = await c.command({'actor': 'owner', 'action': 'input', 'args': {'events': events}})
    assert result['surface'] == 'desktop'
    assert sent == [(['key', '--clearmodifiers', 'ctrl+l'], None),
                    (['type', '--clearmodifiers', '--delay', '0', '--file', '-'], b'person@example.com'),
                    (['key', '--clearmodifiers', 'Return'], None),
                    (['mousemove', '22', '88', 'mousedown', '1'], None),
                    (['mousemove', '45', '90'], None), (['mouseup', '1'], None)]
    # Lost/expired control must release an in-flight drag before another actor acts.
    c.desktop = SimpleNamespace(poll=lambda: None)
    c.lease_until = 0
    await c.command({'actor': 'next-owner', 'action': 'claim'})
    assert sent[-3:] == [(['mouseup', str(button)], None) for button in [1, 2, 3]]


@pytest.mark.asyncio
@pytest.mark.parametrize('frame_mode', [False, True, None])
async def test_native_input_frame_opt_in_preserves_default_state(monkeypatch, tmp_path, frame_mode):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    c.desktop = SimpleNamespace(poll=lambda: None)
    c.controller, c.lease_until = 'owner', computer.time.monotonic()+60
    c.frame, c.frame_at = 'cached-screen', 7
    c.ensure_desktop, c.desktop_input = AsyncMock(), AsyncMock()
    async def capture():
        assert frame_mode is not False, 'Native input must not wait for image capture'
        c.frame, c.frame_at = 'fresh-screen', 8
    c.refresh_frame = AsyncMock(side_effect=capture)
    events = [{'type': 'text', 'text': 'ordered input'}]
    args = {'events': events, **({'frame': frame_mode} if frame_mode is not None else {})}
    reply = await c.command({'actor': 'owner', 'action': 'input', 'args': args})
    c.desktop_input.assert_awaited_once_with(events)
    assert reply['controller'] == 'owner' and reply['surface'] == 'desktop' and reply['available']
    if frame_mode is False:
        c.refresh_frame.assert_not_awaited()
        assert 'frame' not in reply and 'frame_at' not in reply
    else:
        c.refresh_frame.assert_awaited_once()
        assert reply['frame'] == 'fresh-screen' and reply['frame_at'] == 8
    state = await c.state()
    assert state['frame'] == c.frame and state['frame_at'] == c.frame_at


@pytest.mark.asyncio
async def test_agent_follows_human_selected_tab_and_closed_tabs():
    c = computer.Computer()
    def page(visible, focused, closed=False):
        return SimpleNamespace(is_closed=lambda: closed, evaluate=AsyncMock(return_value={'visible': visible, 'focused': focused}))
    first, second = page(True, True), page(False, False)
    c.page, c.context = second, SimpleNamespace(pages=[first, second])
    await c.active_page()
    assert c.page is first
    c.context.pages = [page(False, False, True), second]
    await c.active_page()
    assert c.page is second
    c.context.pages = []
    await c.active_page()
    assert c.page is None


def test_desktop_input_proxy_preserves_server_identity_and_csrf(workspace):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    transport = AsyncMock(return_value={'surface': 'desktop', 'available': True, 'controller': 'owner', 'frame': 'live'})
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=transport))
    body = {'action': 'input', 'args': {'events': [{'type': 'text', 'text': 'test@example.com'}]}}
    assert client.post(url, json=body, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert transport.await_count == 0
    response = client.post(url, json=body)
    assert response.status_code == 200 and response.json()['surface'] == 'desktop'
    forwarded = transport.call_args.args[0]
    assert forwarded['actor'] == response.json()['actor']
    assert forwarded['args'] == body['args']
    assert client.post(url, json={**body, 'actor': 'victim'}).status_code == 422


@pytest.mark.asyncio
async def test_private_desktop_pipe_reuses_process_and_never_replays_a_lost_reply():
    sent, ended = [], []
    async def responses():
        yield '{"available":true}\n'
        yield '{"controller":"owner"}\n'
        raise OSError('reply lost after input was delivered')
    stdin = SimpleNamespace(write=lambda data: sent.append(json.loads(data)),
                            write_eof=lambda: ended.append(True), drain=SimpleNamespace(aio=AsyncMock()))
    launch = AsyncMock(return_value=SimpleNamespace(stdin=stdin, stdout=responses()))
    connection = DesktopConnection(SimpleNamespace(exec=SimpleNamespace(aio=launch)), 'sandbox-one')
    assert (await connection.request({'action':'state'}))['available']
    assert (await connection.request({'action':'claim','actor':'owner'}))['controller'] == 'owner'
    assert launch.await_count == 1
    assert launch.call_args.args[-1] == 'bridge'
    with pytest.raises(Exception, match='interrupted'):
        await connection.request({'action':'input','args':{'events':[{'type':'text','text':'once'}]}})
    assert [body['action'] for body in sent] == ['state','claim','input']
    assert connection.failed and connection.process is None and ended == [True]
    await connection.close()
    assert ended == [True]


@pytest.mark.parametrize('native', [False, True])
async def test_wake_shares_startup_budget_and_closes_its_own_transport(workspace, monkeypatch, native):
    import asyncio
    app, _, _, _ = cloud(workspace)
    hub = app.state.computer
    deadlines, sent, ended = [], [], []
    timeout = asyncio.timeout
    def timed(seconds):
        deadlines.append(seconds)
        return timeout(seconds)
    monkeypatch.setattr('app.computer.asyncio.timeout', timed)
    async def responses():
        yield '{"available":true,"frame":"live"}\n'
    stdin = SimpleNamespace(write=lambda value: sent.append(json.loads(value)),
                            write_eof=lambda: ended.append(True), drain=SimpleNamespace(aio=AsyncMock()))
    launch = AsyncMock(return_value=SimpleNamespace(stdin=stdin, stdout=responses()))
    rpc = AsyncMock(return_value={'available': True, 'frame': 'live'})
    sandbox = SimpleNamespace(object_id='startup', exec=SimpleNamespace(aio=launch))
    if native:
        sandbox.computer_request = rpc
    assert (await hub.wake(sandbox))['frame'] == 'live'
    # apt update + install + Pillow can take 420 seconds on old checkpoints.
    assert deadlines and min(deadlines) > 420
    assert hub.connections == {}  # UI polling never shares this startup pipe.
    if native:
        rpc.assert_awaited_once_with({'action': 'wake'})
        launch.assert_not_awaited()
    else:
        assert sent == [{'action': 'wake'}] and ended == [True]
        assert launch.call_args.args[-1] == 'bridge'
        assert launch.call_args.kwargs['timeout'] > 420


@pytest.mark.asyncio
async def test_desktop_connections_follow_sandbox_identity_idle_expiry_and_shutdown(workspace, monkeypatch):
    app, _, rid, _ = cloud(workspace)
    hub = app.state.computer
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=AsyncMock(return_value={})))
    clock = [100]
    monkeypatch.setattr('app.computer.time', SimpleNamespace(monotonic=lambda: clock[0]))
    run = {**app.state.store.run(rid), 'sandbox_id':'first'}
    async with hub.connection(run) as first:
        first.close = AsyncMock()
    async with hub.connection(run) as same:
        assert same is first
        await same.request({'action':'state'})
        # Copying captures after a reply must not make an idle pipe look fresh.
        clock[0] += 21
    assert hub.sandbox.await_count == 1
    async with hub.connection(run) as expired:
        assert expired is not first
        expired.close = AsyncMock()
    first.close.assert_awaited_once()
    async with hub.connection({**run,'sandbox_id':'replacement'}) as replaced:
        assert replaced is not expired
        replaced.close = AsyncMock()
    expired.close.assert_awaited_once()
    await hub.close()
    replaced.close.assert_awaited_once()
    assert not hub.connections


def test_loopback_request_does_not_retry_unknown_input_delivery(monkeypatch):
    attempts = []
    def lost_reply(*args, **kwargs):
        attempts.append(True)
        raise computer.urllib.error.URLError(ConnectionResetError('reply lost'))
    monkeypatch.setattr(computer.urllib.request, 'urlopen', lost_reply)
    def unexpected_restart(*args, **kwargs):
        raise AssertionError('Unknown delivery must not restart or replay the command')
    monkeypatch.setattr(computer.subprocess, 'Popen', unexpected_restart)
    with pytest.raises(computer.urllib.error.URLError):
        computer.request({'action':'input','args':{'events':[{'type':'text','text':'once'}]}})
    assert attempts == [True]


PR_TAB = 'https://github.com/BerriAI/moyai/pull/145'
OTHER_TAB = 'https://github.com/BerriAI/moyai/pull/151'


@pytest.mark.parametrize('tab', ['', PR_TAB])
def test_failed_refresh_keeps_last_successful_cache_and_retries(workspace, monkeypatch, tab):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    clock = [100]
    monkeypatch.setattr('app.computer.time', SimpleNamespace(monotonic=lambda: clock[0]))
    state = {'available': True, 'tab': tab, 'controller': 'owner', 'frame': 'first'}
    transport = AsyncMock(side_effect=[state, ConnectionError('private provider error'), {**state, 'frame': 'recovered'}])
    hub.sandbox = AsyncMock(return_value=browser_sandbox(rid, transport))
    assert client.get(url, params={'tab': tab}).json()['frame'] == 'first'
    cached = hub.cache[rid, tab]
    assert client.get(url, params={'tab': tab}).json()['frame'] == 'first'
    assert transport.await_count == 1
    clock[0] += .1
    response = client.get(url, params={'tab': tab})
    assert response.status_code == 503
    assert response.json() == {'detail': 'Computer is reconnecting or this sandbox has shut down. Saved captures are still available.'}
    assert hub.cache[rid, tab] is cached and cached[2]['frame'] == 'first'
    recovered = client.get(url, params={'tab': tab}).json()
    assert recovered['available'] and recovered['has_sandbox'] and recovered['frame'] == 'recovered'
    assert client.get(url, params={'tab': tab}).json()['frame'] == 'recovered'
    assert transport.await_count == 3


def test_named_computer_routes_scope_frames_and_invalidate_shared_lease(workspace):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    async def execute(body):
        assert body['actor'] != 'agent'
        return {'available': True, 'tab': body.get('tab', ''), 'frame': body.get('tab', '')}
    transport = AsyncMock(side_effect=execute)
    hub.sandbox = AsyncMock(return_value=browser_sandbox(rid, transport))
    for tab in ['', PR_TAB, OTHER_TAB]:
        result = client.get(url, params={'tab': tab}).json()
        assert result['tab'] == result['frame'] == tab
    assert len(hub.cache) == 3
    assert client.post(url, json={'action': 'claim', 'tab': OTHER_TAB}).status_code == 200
    assert not hub.cache
    assert transport.call_count == 5
    assert transport.call_args.args[0]['tab'] == OTHER_TAB
    client.cookies.clear()
    assert client.get(url, params={'tab': PR_TAB}).status_code == 401


def test_old_computer_runtime_cannot_render_or_mutate_the_agent_page(workspace):
    app, client, _, url = cloud(workspace)
    hub = app.state.computer
    transport = AsyncMock(return_value={'available': True, 'frame': 'agent-secret'})
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(object_id='legacy-sandbox', computer_request=transport))
    result = client.get(url, params={'tab': PR_TAB}).json()
    assert result['available'] is False and not result.get('frame')
    assert result['tab'] == PR_TAB
    assert 'needs a restart' in result['notice']
    transport.reset_mock()
    result = client.post(url, json={'action': 'open', 'tab': PR_TAB})
    assert result.status_code == 503
    assert 'needs a restart' in result.json()['detail']
    assert transport.call_count == 1
    assert transport.call_args.args[0]['action'] == 'state'


@pytest.mark.parametrize('state', ['no-id', 'stopped', 'missing', 'unknown'])
def test_named_close_acknowledges_absence_but_not_provider_failure(workspace, monkeypatch, state):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    sandbox = SimpleNamespace(poll=SimpleNamespace(aio=AsyncMock(return_value=0)))
    provider = SimpleNamespace(get=AsyncMock(return_value=sandbox))
    monkeypatch.setattr(hub.manager, 'provider', lambda _: provider)
    if state != 'no-id':
        app.state.store.update_run(rid, sandbox_id='disposable-sandbox')
    if state == 'missing':
        provider.get.side_effect = NotFoundError('Sandbox expired')
    elif state == 'unknown':
        provider.get.side_effect = HTTPException(503, 'Provider temporarily unavailable')
    transport = AsyncMock()
    hub.cache[rid, PR_TAB] = (0, 'disposable-sandbox', {'available': True})
    result = client.post(url, json={'action': 'close_tab', 'tab': PR_TAB})
    assert result.status_code == (503 if state == 'unknown' else 200)
    assert not hub.cache
    transport.assert_not_awaited()
    if state == 'no-id':
        provider.get.assert_not_awaited()
    assert client.post(url, json={'action': 'open', 'tab': PR_TAB}).status_code == (503 if state == 'unknown' else 409)
    assert client.post(url, json={'action': 'close_tab', 'tab': PR_TAB}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403


@pytest.mark.parametrize('recording', [False, True])
def test_named_close_on_healthy_legacy_runtime_never_mutates_the_agent_browser(workspace, recording):
    app, client, _, url = cloud(workspace)
    hub = app.state.computer
    transport = AsyncMock(return_value={'available': True, 'recording': recording, 'frame': 'agent-frame'})
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=transport))
    result = client.post(url, json={'action': 'close_tab', 'tab': PR_TAB})
    assert result.status_code == 200 and result.json()['tab'] == PR_TAB
    assert transport.await_count == 2
    assert all(call.args[0]['action'] == 'state' for call in transport.await_args_list)
    assert transport.call_args.args[0]['action'] == 'state'


@pytest.mark.parametrize('scope', [{}, {'available': False}, {'available': False, 'recording': 'invalid'},
    {'available': False, 'recording': False, 'error': 'State failed'},
    {'available': False, 'recording': False, 'tab': OTHER_TAB}])
def test_named_close_does_not_treat_malformed_or_wrong_scope_as_absent(workspace, scope):
    app, client, _, url = cloud(workspace)
    hub = app.state.computer
    transport = AsyncMock(return_value=scope)
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=transport))
    assert client.post(url, json={'action': 'close_tab', 'tab': PR_TAB}).status_code == 503
    assert transport.await_count == (1 if scope.get('error') else 2)
    assert all(call.args[0]['action'] == 'state' for call in transport.await_args_list)
    assert transport.call_args.args[0]['action'] == 'state'


def test_named_close_preserves_live_runtime_recording_rejection(workspace):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    transport = AsyncMock(side_effect=[{'available': False, 'tab': PR_TAB, 'recording': True},
                                        {'error': 'Stop the recording before closing this tab.'}])
    hub.sandbox = AsyncMock(return_value=browser_sandbox(rid, transport))
    result = client.post(url, json={'action': 'close_tab', 'tab': PR_TAB})
    assert result.status_code == 409 and 'Stop the recording' in result.json()['detail']
    assert transport.await_count == 2



@pytest.mark.parametrize('tab', ['javascript:alert(1)', 'https://github.com.evil/a/b/pull/1',
    'https://user:pass@github.com/a/b/pull/1', 'https://github.com/a/b/pull/0', PR_TAB + '?token=secret',
    PR_TAB + '/files', PR_TAB + '#comment', 'https://github.com/' + 'a' * 500 + '/b/pull/1'])
def test_named_computer_tab_validation_agrees_at_api_and_runtime(workspace, tab):
    _, client, _, url = cloud(workspace)
    assert client.get(url, params={'tab': tab}).status_code == 422
    assert client.post(url, json={'action': 'open', 'tab': tab}).status_code == 422
    with pytest.raises(ValueError, match='canonical'):
        computer.valid_tab(tab)


class BrowserPage:
    """Page interface fixture: assertions exercise Computer's production routing."""
    def __init__(self, name):
        self.url, self.closed, self.events = name, False, {}
        self.mouse = SimpleNamespace(click=AsyncMock(), wheel=AsyncMock(), move=AsyncMock(), down=AsyncMock(), up=AsyncMock())
        self.keyboard = SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock())
        self.bring_to_front = AsyncMock()
        self.screenshot = AsyncMock(return_value=PNG + name.encode())
        self.goto = AsyncMock(side_effect=self.navigate)
        self.title = AsyncMock(return_value=name)
        self.go_back = AsyncMock()
    async def navigate(self, url, **kwargs):
        self.url = url
    async def close(self):
        self.closed = True
    def is_closed(self):
        return self.closed
    def set_default_timeout(self, value):
        pass
    def on(self, event, callback):
        self.events[event] = callback
    def locator(self, selector):
        return SimpleNamespace(inner_text=AsyncMock(return_value=self.url), evaluate_all=AsyncMock(return_value=[]))


def restoring_context(desktop, *, navigation='', failure=''):
    context = SimpleNamespace(pages=[], originals=[], events={})
    context.on = lambda event, callback: context.events.update({event: callback})
    async def new_page():
        page = BrowserPage('about:blank')
        context.pages.append(page)
        context.originals.append(page)
        context.events['page'](page)
        async def navigate(url, **kwargs):
            page.url = url
            if len(context.originals) == 1 and navigation == 'popup':
                popup = BrowserPage('https://example.test/popup')
                context.pages.append(popup)
                context.events['page'](popup)
            if ((len(context.originals) == 1 and navigation == 'close_first') or
                    (len(context.originals) == 2 and navigation == 'close_active')):
                await page.close()
                context.pages.remove(page)
        page.goto = AsyncMock(side_effect=navigate)
        return page
    async def cdp(page):
        if failure == 'setup' and len(context.originals) == 2:
            raise RuntimeError('CDP setup failed')
        async def send(method, args=None):
            if failure == 'remove' and method == 'Page.removeScriptToEvaluateOnNewDocument':
                raise RuntimeError('CDP removal failed')
            return {'identifier': 'restore-script'}
        return SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock(
            side_effect=RuntimeError('CDP detach failed') if failure == 'detach' else None))
    async def close():
        for page in context.pages:
            await page.close()
        context.events['close'](context)
    context.new_page, context.new_cdp_session, context.close = AsyncMock(side_effect=new_page), AsyncMock(side_effect=cdp), AsyncMock(side_effect=close)
    return context


def saved_browser_pages():
    return {'storage': {'cookies': [], 'origins': []}, 'active': 1,
        'pages': [{'url': 'https://example.test/' + page, 'session_storage': {'tab': page}} for page in ('first', 'second')]}


@pytest.fixture
def checkpoint_browser(workspace, monkeypatch):
    app, _, rid, _ = cloud(workspace)
    desktop = computer.Computer()
    desktop.browser_scope = rid
    desktop.context = SimpleNamespace(pages=[], storage_state=AsyncMock(return_value={
        'cookies': [{'name': 'session', 'value': 'fresh-login'}], 'origins': []}))
    sandbox = SimpleNamespace(object_id='sb-export', computer_request=desktop.command)
    app.state.store.update_run(rid, sandbox_id=sandbox.object_id)
    for name in ('BROWSER_ACTIVE_TIMEOUT', 'BROWSER_STORAGE_TIMEOUT', 'BROWSER_PAGE_TIMEOUT'):
        monkeypatch.setattr(computer, name, .02)
    return app.state.computer, desktop, sandbox, rid


def exporting_page(desktop, url, values):
    page = BrowserPage(url)
    page.session_storage = dict(values)
    async def evaluate(script):
        if 'document.visibilityState' in script:
            return {'visible': True, 'focused': page is desktop.page}
        return {'url': page.url, 'session_storage': dict(page.session_storage)}
    page.evaluate = AsyncMock(side_effect=evaluate)
    return page


@pytest.mark.parametrize('fault', ['error', 'hang'])
async def test_partial_export_keeps_only_same_page_and_url_state(checkpoint_browser, fault):
    hub, desktop, sandbox, rid = checkpoint_browser
    first = desktop.page = exporting_page(desktop, 'https://example.test/account', {'session': 'first-login'})
    second = exporting_page(desktop, first.url, {'session': 'second-login'})
    desktop.context.pages = [first, second]
    assert await hub.checkpoint(sandbox, rid) is False
    async def failed(script):
        if 'document.visibilityState' in script:
            return {'visible': True, 'focused': True}
        if fault == 'hang':
            await computer.asyncio.Event().wait()
        raise RuntimeError('Tab navigated during export')
    first.evaluate.side_effect = failed
    second.session_storage['session'] = 'second-rotated'
    desktop.context.storage_state.return_value = {'cookies': [{'name': 'session', 'value': 'rotated-login'}], 'origins': []}
    assert await computer.asyncio.wait_for(hub.checkpoint(sandbox, rid), .5) is True
    assert desktop.saved_browser['pages'][0]['session_storage'] == {'session': 'first-login'}
    assert desktop.saved_browser['pages'][1]['session_storage'] == {'session': 'second-rotated'}
    assert desktop.saved_browser['storage']['cookies'][0]['value'] == 'rotated-login'
    first.url = 'https://another.test/account'
    assert await hub.checkpoint(sandbox, rid) is True
    assert desktop.saved_browser['pages'][0] == {'url': first.url, 'session_storage': {}}
    replacement = desktop.page = exporting_page(desktop, second.url, {})
    replacement.evaluate.side_effect = failed
    desktop.context.pages = [replacement, second]
    second.session_storage.clear()
    desktop.context.storage_state.return_value = {'cookies': [], 'origins': []}
    assert await hub.checkpoint(sandbox, rid) is True
    encrypted = hub.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    saved = json.loads(hub.security.decrypt(encrypted))['state']
    assert saved['storage'] == {'cookies': [], 'origins': []}
    assert all(page['session_storage'] == {} for page in saved['pages'])
    assert first not in desktop.saved_pages
    second.evaluate.side_effect = failed
    await hub.checkpoint(sandbox, rid)
    assert all(page['session_storage'] == {} for page in desktop.saved_browser['pages'])
    desktop.context_closed(desktop.context)
    assert not desktop.saved_pages


async def test_export_bounds_focus_probe_and_concurrently_selects_active_tab(checkpoint_browser):
    hub, desktop, sandbox, rid = checkpoint_browser
    pages = [exporting_page(desktop, f'https://example.test/{index}', {}) for index in range(34)]
    desktop.context.pages, desktop.page = pages, pages[-1]
    started, together = set(), computer.asyncio.Event()
    for page in pages:
        async def evaluate(script, page=page):
            if 'document.visibilityState' in script:
                await computer.asyncio.Event().wait()
            started.add(page)
            if len(started) == 32:
                together.set()
            await together.wait()
            return {'url': page.url, 'session_storage': {'tab': page.url}}
        page.evaluate.side_effect = evaluate
    assert await computer.asyncio.wait_for(hub.checkpoint(sandbox, rid), .5) is True
    assert started == set(pages[:31] + [pages[-1]])
    saved = desktop.saved_browser
    assert [page['url'] for page in saved['pages']] == [page.url for page in pages[:31] + [pages[-1]]]
    assert saved['pages'][saved['active']]['url'] == pages[-1].url
    assert saved['storage']['cookies'][0]['value'] == 'fresh-login'
    assert all(page['session_storage'] for page in saved['pages'])


@pytest.mark.parametrize('fault', ['error', 'hang'])
async def test_authoritative_storage_failure_keeps_runtime_and_encrypted_checkpoint(checkpoint_browser, fault):
    hub, desktop, sandbox, rid = checkpoint_browser
    await hub.checkpoint(sandbox, rid)
    saved = desktop.saved_browser
    encrypted = hub.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    async def failed(**kwargs):
        if fault == 'hang':
            await computer.asyncio.Event().wait()
        raise RuntimeError('Storage export unavailable')
    desktop.context.storage_state.side_effect = failed
    with pytest.raises(HTTPException):
        await computer.asyncio.wait_for(hub.checkpoint(sandbox, rid), .5)
    assert desktop.saved_browser is saved
    assert hub.store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted'] == encrypted


async def test_restored_page_cache_survives_first_failed_metadata_export():
    desktop = computer.Computer()
    desktop.browser_scope = 'a' * 32
    saved = desktop.saved_browser = saved_browser_pages()
    context = restoring_context(desktop)
    context.storage_state = AsyncMock(return_value=saved['storage'])
    desktop.start_browser = desktop.refresh_frame = AsyncMock()
    desktop.browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    await desktop.open()
    result = await desktop.command({'action': 'state',
        'args': {'browser': 'checkpoint', 'scope': desktop.browser_scope}})
    assert result['partial'] is True and result['state'] == saved


@pytest.mark.asyncio
@pytest.mark.parametrize('navigation', ['popup', 'close_first', 'close_active'])
async def test_browser_restore_focus_uses_saved_page_identity(navigation):
    desktop = computer.Computer()
    desktop.saved_browser = saved_browser_pages()
    context = restoring_context(desktop, navigation=navigation)
    desktop.start_browser = desktop.refresh_frame = AsyncMock()
    desktop.browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    await desktop.open()
    expected = context.originals[0 if navigation == 'close_active' else 1]
    assert desktop.page is expected and not expected.is_closed()
    expected.bring_to_front.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['setup', 'remove', 'detach'])
async def test_browser_restore_rolls_back_partial_context_and_retries(failure):
    desktop = computer.Computer()
    saved = desktop.saved_browser = saved_browser_pages()
    failed, ready = restoring_context(desktop, failure=failure), restoring_context(desktop)
    desktop.start_browser = desktop.refresh_frame = AsyncMock()
    desktop.browser = SimpleNamespace(new_context=AsyncMock(side_effect=[failed, ready]))
    with pytest.raises(RuntimeError):
        await desktop.open()
    assert desktop.context is None and desktop.page is None
    failed.close.assert_awaited_once()
    assert await desktop.checkpoint() == saved
    await desktop.open()
    assert desktop.context is ready and desktop.page is ready.originals[1]
    assert [page.url for page in ready.originals] == [entry['url'] for entry in saved['pages']]


@pytest.mark.asyncio
@pytest.mark.parametrize('live', [False, True])
async def test_blocked_browser_preserves_checkpoint_and_heals_only_in_same_scope(live):
    desktop = computer.Computer()
    desktop.start_browser = desktop.active_page = desktop.refresh_frame = AsyncMock()
    context = restoring_context(desktop)
    desktop.browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    saved = desktop.saved_browser = saved_browser_pages()
    scope = 'a' * 32
    async def restore(**args):
        return await desktop.command({'action': 'state', 'args': {'browser': 'restore', 'scope': scope, **args}})
    await restore(state=saved)
    if live:
        page = desktop.page = BrowserPage('https://example.test/newer-live-page')
        desktop.context = SimpleNamespace(pages=[page])
    await restore(blocked=True)
    with pytest.raises(ValueError, match='Saved browser access'):
        await desktop.open()
    with pytest.raises(ValueError, match='Saved browser access'):
        await desktop.command({'action': 'state', 'args': {'browser': 'checkpoint', 'scope': scope}})
    assert desktop.saved_browser == saved
    with pytest.raises(ValueError, match='another session'):
        await desktop.command({'action': 'state', 'args': {'browser': 'restore', 'scope': 'b' * 32, 'state': saved}})
    await restore(state=saved)
    await desktop.open()
    assert desktop.page is (page if live else context.originals[1])


@pytest.mark.parametrize('failure', ['unbound', 'unknown_block', 'transport', 'blocked'])
async def test_first_browser_admission_replaces_unbound_live_context_without_losing_saved_auth(workspace, failure):
    app, _, rid, _ = cloud(workspace)
    hub, store = app.state.computer, app.state.store
    desktop = computer.Computer()
    live, restored = restoring_context(desktop), restoring_context(desktop)
    live.storage_state = AsyncMock(return_value={'cookies': [], 'origins': []})
    desktop.start_browser = desktop.refresh_frame = AsyncMock()
    desktop.browser = SimpleNamespace(new_context=AsyncMock(side_effect=[live, restored]))
    sandbox = SimpleNamespace(object_id='sb-admission', computer_request=desktop.command)
    saved = saved_browser_pages()
    saved['storage']['cookies'] = [{'name': 'session', 'value': 'preserved-login'}]
    encrypted = hub.security.encrypt(json.dumps({'scope': rid, 'state': saved}))
    store.update_run(rid, sandbox_id=sandbox.object_id)
    store.execute('INSERT INTO browser_sessions(run_id,encrypted) VALUES(?,?)',
                  (rid, 'corrupt' if failure in {'unknown_block', 'blocked'} else encrypted))
    if failure in {'unbound', 'blocked'}:
        await desktop.open()
    if failure != 'unbound':
        if failure in {'unknown_block', 'transport'}:
            sandbox.computer_request = AsyncMock(side_effect=ConnectionError('Unknown restore delivery'))
        await hub.restore(sandbox, rid, required=False)
        if failure == 'blocked':
            with pytest.raises(ValueError, match='Saved browser access'):
                await desktop.open()
        else:
            await desktop.open()  # The agent can open a fresh context after an unknown restore outcome.
    assert desktop.context is live and not desktop.browser_admitted
    store.execute('UPDATE browser_sessions SET encrypted=? WHERE run_id=?', (encrypted, rid))
    sandbox.computer_request = desktop.command
    await hub.restore(sandbox, rid)
    live.close.assert_awaited_once()
    assert desktop.context is None and desktop.page is None and not desktop.saved_pages
    assert desktop.browser_admitted and not desktop.browser_blocked and desktop.saved_browser == saved
    await hub.checkpoint(sandbox, rid)  # A poll before hydration must not publish the blank old context.
    encrypted = store.rows('SELECT encrypted FROM browser_sessions WHERE run_id=?', (rid,))[0]['encrypted']
    assert json.loads(hub.security.decrypt(encrypted)) == {'scope': rid, 'state': saved}
    await desktop.open()
    assert desktop.context is restored
    assert desktop.browser.new_context.call_args.kwargs['storage_state'] == saved['storage']
    assert [page.url for page in restored.pages] == [entry['url'] for entry in saved['pages']]


@pytest.mark.parametrize('logout', [False, True])
async def test_admitted_live_browser_keeps_newer_auth_and_logout_during_same_scope_healing(logout):
    desktop = computer.Computer()
    page = desktop.page = exporting_page(desktop, 'https://example.test/live', {} if logout else {'session': 'new-tab'})
    storage = {'cookies': [] if logout else [{'name': 'session', 'value': 'new-login'}], 'origins': []}
    context = desktop.context = SimpleNamespace(pages=[page], storage_state=AsyncMock(return_value=storage), close=AsyncMock())
    async def restore(**args):
        return await desktop.command({'action': 'state', 'args': {'browser': 'restore', 'scope': 'a' * 32, **args}})
    await restore(state=None)  # Authoritative absence may adopt a live context.
    assert desktop.browser_admitted
    await restore(state=saved_browser_pages())
    await restore(blocked=True)
    await restore(state=saved_browser_pages())
    context.close.assert_not_awaited()
    assert desktop.context is context and desktop.page is page
    saved = await desktop.checkpoint()
    assert saved['storage'] == storage and saved['pages'][0]['session_storage'] == page.session_storage


@pytest.mark.parametrize('failure', ['error', 'timeout', 'cancel'])
async def test_failed_first_context_close_reserves_scope_and_retries_without_admitting(monkeypatch, failure):
    monkeypatch.setattr(computer, 'BROWSER_CLOSE_TIMEOUT', .02)
    desktop, started = computer.Computer(), computer.asyncio.Event()
    async def close():
        started.set()
        if failure == 'error':
            raise RuntimeError('Close failed')
        await computer.asyncio.Event().wait()
    context = desktop.context = SimpleNamespace(close=AsyncMock(side_effect=close))
    page = desktop.page = BrowserPage('https://example.test/unbound')
    previous = {page: {'url': page.url, 'session_storage': {'token': 'unadmitted'}}}
    desktop.saved_pages = dict(previous)
    saved = saved_browser_pages()
    async def restore(scope='a' * 32):
        return await desktop.command({'action': 'state', 'args': {'browser': 'restore', 'scope': scope, 'state': saved}})
    attempt = computer.asyncio.create_task(restore())
    await computer.asyncio.wait_for(started.wait(), .5)
    if failure == 'cancel':
        attempt.cancel()
    expected = {'error': RuntimeError, 'timeout': TimeoutError, 'cancel': computer.asyncio.CancelledError}[failure]
    try:
        with pytest.raises(expected):
            await computer.asyncio.wait_for(computer.asyncio.shield(attempt), .5)
        assert attempt.done()
    finally:
        if not attempt.done():
            attempt.cancel()
            await computer.asyncio.gather(attempt, return_exceptions=True)
    assert desktop.browser_scope == 'a' * 32 and desktop.browser_blocked and not desktop.browser_admitted
    assert desktop.context is context and desktop.page is page and desktop.saved_pages == previous
    assert desktop.saved_browser is None
    with pytest.raises(ValueError, match='another session'):
        await restore('b' * 32)
    with pytest.raises(ValueError, match='Saved browser access'):
        await desktop.open()
    context.close.side_effect = None
    await restore()
    assert desktop.browser_admitted and not desktop.browser_blocked and desktop.saved_browser == saved
    assert desktop.context is None and desktop.page is None and not desktop.saved_pages


@pytest.fixture
def review_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    native = SimpleNamespace(active=11, windows={11: 'normal', 22: 'maximized'}, inputs=[], changes=[])
    c.desktop = SimpleNamespace(poll=lambda: None)
    c.ensure_desktop = AsyncMock()
    c.desktop_image = lambda image_format='JPEG': f'native-window-{native.active}'.encode()
    async def window_command(*args):
        if args == ('getactivewindow',):
            return str(native.active) if native.active else None
        assert args[:2] == ('windowactivate', '--sync')
        target = int(args[2])
        if target not in native.windows:
            return None
        native.active = target
        return ''
    c.window_command = AsyncMock(side_effect=window_command)
    async def desktop_input(events):
        if any(event['type'] == 'text' for event in events):
            native.inputs.append(native.active)
    c.desktop_input = AsyncMock(side_effect=desktop_input)
    async def new_cdp_session(page):
        async def send(method, args=None):
            identity = page.native_window
            if method == 'Browser.getWindowForTarget':
                return {'windowId': identity, 'bounds': {'windowState': native.windows[identity]}}
            assert method == 'Browser.setWindowBounds' and args['windowId'] == identity
            state = args['bounds']['windowState']
            native.changes.append((identity, state))
            native.windows[identity] = state
            if state == 'minimized' and native.active == identity:
                native.active = 11 if 11 in native.windows else 0
            return {}
        return SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    c.review_context = SimpleNamespace(pages=[], new_cdp_session=AsyncMock(side_effect=new_cdp_session))
    def attach(tab, name, window=22, tracked=True):
        page = BrowserPage(name)
        page.native_window = window
        native.windows.setdefault(window, 'normal')
        async def focus():
            native.active = window
        page.bring_to_front = AsyncMock(side_effect=focus)
        c.review_context.pages.append(page)
        if tracked:
            c.track_review_page(tab, page)
        return page
    review = attach(PR_TAB, 'review')
    return c, native, review, attach


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_action', ['release', 'desktop', 'expiry', 'close'])
async def test_pr_handoff_restores_native_window_before_desktop_input(review_desktop, exit_action):
    c, native, review, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    assert native.active == 22
    if exit_action == 'expiry':
        c.lease_until = 0
    action = 'claim' if exit_action in {'desktop', 'expiry'} else 'close_tab' if exit_action == 'close' else 'release'
    await c.command({'action': action, 'actor': 'owner', 'tab': '' if action == 'claim' else PR_TAB})
    assert native.active == 11
    assert c.page is None and c.context is None
    await c.command({'action': 'claim', 'actor': 'owner'})
    result = await c.command({'action': 'input', 'actor': 'owner', 'args': {'events': [{'type': 'text', 'text': 'terminal input'}]}})
    assert native.inputs == [11]
    assert base64.b64decode(result['frame']) == b'native-window-11'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed_desktop', ['closed', 'new-window'])
async def test_pr_handoff_handles_changed_native_desktop_without_opening_agent_page(review_desktop, changed_desktop):
    c, native, _, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    if changed_desktop == 'closed':
        native.windows.pop(11)
    else:
        native.windows[33] = 'normal'
        native.active = 33
    await c.command({'action': 'release', 'actor': 'owner', 'tab': PR_TAB})
    assert native.active == (0 if changed_desktop == 'closed' else 33)
    assert native.windows[22] == 'minimized'
    assert c.page is None and c.context is None and not c.foreground_tab
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    assert native.active == 22 and native.windows[22] == 'maximized'


@pytest.mark.asyncio
async def test_review_switch_popup_and_shared_window_handoff_preserve_active_owner(review_desktop):
    c, native, first, attach = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    second = attach(OTHER_TAB, 'second', 23)
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': OTHER_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': OTHER_TAB})
    await c.command({'action': 'release', 'actor': 'owner', 'tab': PR_TAB})
    popup = attach(PR_TAB, 'late-shared-popup', 23, tracked=False)
    await first.events['popup'](popup)
    assert native.active == 23 and c.controller_tab == OTHER_TAB
    assert native.windows[23] != 'minimized'
    second.bring_to_front.assert_awaited()
    await c.command({'action': 'close_tab', 'actor': 'owner', 'tab': PR_TAB})
    assert native.active == 23 and c.controller_tab == OTHER_TAB and not second.closed
    # Several live targets can occupy the same browser window.
    attach(OTHER_TAB, 'same-window', 23)
    native.changes.clear()
    await c.command({'action': 'release', 'actor': 'owner', 'tab': OTHER_TAB})
    assert native.active == 11 and native.changes.count((23, 'minimized')) == 1
    late = attach(OTHER_TAB, 'after-release', 24, tracked=False)
    native.active = 24
    await second.events['popup'](late)
    assert native.active == 11 and native.windows[24] == 'minimized'


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_page', [False, True])
@pytest.mark.parametrize('finalizer', ['automatic', 'manual'])
async def test_pr_recording_pins_foreground_until_finalized_even_without_page(review_desktop, tmp_path, missing_page, finalizer):
    c, native, review, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    path = tmp_path / 'pinned.partial'
    path.write_bytes(WEBM)
    recording = {'actor': 'owner', 'tab': PR_TAB, 'path': path, 'started': computer.time.time(),
                 'process': SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))}
    c.recording = recording
    await c.command({'action': 'release', 'actor': 'owner', 'tab': PR_TAB})
    if missing_page:
        await review.close()
    await c.settle_foreground()
    assert native.active == 22 and c.recording is recording and c.foreground_tab == PR_TAB
    with pytest.raises(ValueError, match='recording'):
        await c.command({'action': 'claim', 'actor': 'owner'})
    with pytest.raises(ValueError, match='recording'):
        await c.command({'action': 'close_tab', 'actor': 'owner', 'tab': PR_TAB})
    if finalizer == 'manual':
        await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
        result = await c.command({'action': 'record_stop', 'actor': 'owner', 'tab': PR_TAB})
    else:
        result = await c.stop_recording()
    assert result['name'] == 'pinned.webm' and not c.recording
    assert native.active == (22 if finalizer == 'manual' and not missing_page else 11)


@pytest.mark.asyncio
async def test_uncertain_handoff_blocks_native_input_and_can_retry(review_desktop):
    c, native, _, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    session = c.review_context.new_cdp_session.side_effect
    c.review_context.new_cdp_session.side_effect = RuntimeError('CDP temporarily disconnected')
    with pytest.raises(RuntimeError, match='disconnected'):
        await c.command({'action': 'release', 'actor': 'owner', 'tab': PR_TAB})
    with pytest.raises(RuntimeError, match='disconnected'):
        await c.command({'action': 'input', 'actor': 'owner', 'args': {'events': [{'type': 'text', 'text': 'must not reach PR'}]}})
    assert not native.inputs and c.foreground_tab == PR_TAB
    c.review_context.new_cdp_session.side_effect = session
    await c.command({'action': 'claim', 'actor': 'owner'})
    assert native.active == 11 and not c.foreground_tab


@pytest.mark.asyncio
async def test_handoff_failure_keeps_frame_loop_alive_and_preserves_finalized_capture(review_desktop, tmp_path):
    c, _, _, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    c.controller = c.controller_tab = ''
    path = tmp_path / 'limit.partial'
    path.write_bytes(WEBM)
    c.recording = {'actor': 'owner', 'tab': PR_TAB, 'path': path, 'started': computer.time.time(),
                   'process': SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))}
    c.settle_foreground = AsyncMock(side_effect=RuntimeError('Handoff unavailable'))
    task = computer.asyncio.create_task(c.frames())
    try:
        await computer.asyncio.sleep(.15)
        assert not task.done() and not c.recording
        assert path.with_suffix('.webm').exists()
        assert c.settle_foreground.await_count >= 2
    finally:
        task.cancel()
        await computer.asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('closing', ['finish', 'checkpoint'])
async def test_whole_service_finish_fences_already_queued_and_later_mutations(monkeypatch, tmp_path, closing):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    await c.command({'action': 'finish'})
    assert not c.stopping
    await c.lock.acquire()
    queued = computer.asyncio.create_task(c.command({'action': 'claim', 'actor': 'owner'}))
    await computer.asyncio.sleep(0)
    c.browser_scope = 'a' * 32
    command = ({'action': 'finish', 'args': {'all': True}} if closing == 'finish' else
               {'action': 'state', 'args': {'browser': 'checkpoint', 'scope': c.browser_scope, 'releasing': True}})
    release = computer.asyncio.create_task(c.command(command))
    await computer.asyncio.sleep(0)
    assert c.stopping
    c.lock.release()
    with pytest.raises(ValueError, match='shutting down'):
        await queued
    result = await release
    assert result == ({'recording': False} if closing == 'finish' else
                      {'scope': c.browser_scope, 'state': None, 'partial': False})
    for action in ['claim', 'open', 'record_start']:
        with pytest.raises(ValueError, match='shutting down'):
            await c.command({'action': action, 'actor': 'owner'})
    assert not (await c.command({'action': 'state'}))['recording']
    assert await c.command({'action': 'finish', 'args': {'all': True}}) == {'recording': False}
    (tmp_path / 'saved.webm').write_bytes(WEBM)
    assert base64.b64decode(computer.capture_read('saved.webm')['data']) == WEBM


@pytest.mark.asyncio
async def test_whole_service_finish_drains_active_record_start_without_waiting_for_handoff(review_desktop, monkeypatch):
    c, _, _, _ = review_desktop
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'open', 'actor': 'owner', 'tab': PR_TAB})
    started, resume = computer.asyncio.Event(), computer.asyncio.Event()
    process = SimpleNamespace(returncode=None, send_signal=lambda _: None)
    async def finished():
        process.returncode = 0
        return 0
    process.wait = AsyncMock(side_effect=finished)
    async def spawn(*args, **kwargs):
        assert args[0] == 'ffmpeg'
        Path(args[-1]).write_bytes(WEBM)
        started.set()
        await resume.wait()
        return process
    monkeypatch.setattr(computer.asyncio, 'create_subprocess_exec', spawn)
    recording = computer.asyncio.create_task(c.command({'action': 'record_start', 'actor': 'owner', 'tab': PR_TAB}))
    await started.wait()
    cdp_count = c.review_context.new_cdp_session.await_count
    c.review_context.new_cdp_session.side_effect = AssertionError('Shutdown must not wait on window restoration')
    release = computer.asyncio.create_task(c.command({'action': 'finish', 'args': {'all': True}}))
    await computer.asyncio.sleep(0)
    assert c.stopping and not release.done()
    resume.set()
    assert (await recording)['recording']
    result = await release
    assert Path(result['path']).read_bytes() == WEBM and not c.recording
    assert c.review_context.new_cdp_session.await_count == cdp_count


@pytest.mark.asyncio
async def test_named_pages_keep_agent_context_history_frames_and_lease_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    c.page, first, second = BrowserPage('agent'), BrowserPage('first'), BrowserPage('second')
    c.open = AsyncMock()
    c.ensure_desktop = AsyncMock()
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[first, second]))
    c.ensure_desktop = AsyncMock()
    c.browser = SimpleNamespace(is_connected=lambda: True, new_context=AsyncMock(return_value=context))
    async def command(action, tab=PR_TAB, **args):
        return await c.command({'action': action, 'tab': tab, 'actor': 'google:owner', 'args': args})
    assert not (await command('state'))['available']
    await command('claim')
    assert context.new_page.call_count == 0
    with pytest.raises(ValueError, match='Open this'):
        await command('click', x=1, y=2)
    await command('open')
    await command('open', url='https://github.com/BerriAI/moyai/pull/145/files')
    await command('open')
    assert first.goto.call_count == 2 and first.url.endswith('/files')
    await command('type', text='review comment draft')
    first.keyboard.insert_text.assert_awaited_once_with('review comment draft')
    c.page.keyboard.insert_text.assert_not_awaited()
    first.bring_to_front.assert_awaited()
    first_state = await command('state')
    await command('claim', OTHER_TAB)
    await command('release')  # A delayed release from the old panel must not release B.
    assert c.controls() == 'google:owner' and c.controller_tab == OTHER_TAB
    with pytest.raises(ValueError, match='Take control of this tab'):
        await command('type', text='stale')
    await command('open', OTHER_TAB)
    second_state = await command('state', OTHER_TAB)
    assert first_state['frame'] != second_state['frame']
    assert first_state['tab'] == PR_TAB and second_state['tab'] == OTHER_TAB
    assert c.page.url == 'agent' and c.browser.new_context.call_count == 1
    with pytest.raises(ValueError, match='Another person'):
        await c.command({'action': 'close_tab', 'tab': PR_TAB, 'actor': 'google:other'})
    await command('close_tab')
    assert c.controls() == 'google:owner' and c.controller_tab == OTHER_TAB
    assert first.closed and not second.closed and not c.page.closed
    assert not (await command('state'))['available']
    assert (await command('state', OTHER_TAB))['available']
    await command('release', OTHER_TAB)
    await command('close_tab', OTHER_TAB)  # Cleanup also works after releasing the lease.
    assert second.closed
    with pytest.raises(ValueError, match='signed-in person'):
        await c.command({'action': 'open', 'tab': PR_TAB})


@pytest.mark.asyncio
async def test_review_popup_scope_page_limit_and_recording_tab_are_enforced(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    c.open = AsyncMock()
    c.ensure_desktop = AsyncMock()
    c.page = BrowserPage('agent')
    first, popup = BrowserPage('first'), BrowserPage('popup')
    c.track_review_page(PR_TAB, first)
    await first.events['popup'](popup)
    assert c.page_for(PR_TAB) is popup and c.page.url == 'agent'
    await popup.close()
    assert c.page_for(PR_TAB) is first
    for number in range(15):
        c.track_review_page(f'https://github.com/a/b/pull/{number + 1}', BrowserPage(str(number)))
    with pytest.raises(ValueError, match='16-tab limit'):
        await c.open_review_page(OTHER_TAB)
    rejected = BrowserPage('too-many')
    await first.events['popup'](rejected)
    assert rejected.closed
    c.recording = {'tab': PR_TAB, 'path': tmp_path / 'capture.partial'}
    state = await c.command({'action': 'state', 'tab': OTHER_TAB, 'actor': 'google:owner'})
    assert state['recording_elsewhere'] and not state['recording']
    for action, tab in [('claim', OTHER_TAB), ('close_tab', PR_TAB)]:
        with pytest.raises(ValueError, match='Stop the recording'):
            await c.command({'action': action, 'tab': tab, 'actor': 'google:owner'})


@pytest.mark.asyncio
async def test_review_browser_startup_never_creates_or_reuses_the_agent_context(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    review, agent = BrowserPage('review'), BrowserPage('agent')
    review_context = SimpleNamespace(new_page=AsyncMock(return_value=review))
    agent_context = SimpleNamespace(new_page=AsyncMock(return_value=agent), on=lambda *args: None)
    c.ensure_desktop = AsyncMock()
    c.browser = SimpleNamespace(is_connected=lambda: True, new_context=AsyncMock(side_effect=[review_context, agent_context]))
    assert await c.open_review_page(PR_TAB) is review
    assert c.page is None and c.context is None
    await c.open()
    assert c.page is agent and c.context is agent_context and c.review_context is review_context
    assert c.page_for(PR_TAB) is review


@pytest.mark.asyncio
async def test_recording_can_stop_after_its_review_page_disappears(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    path = tmp_path / 'closed-page.partial'
    path.write_bytes(WEBM)
    c.recording = {'tab': PR_TAB, 'path': path, 'started': computer.time.time(),
                   'process': SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))}
    await c.command({'action': 'claim', 'tab': PR_TAB, 'actor': 'google:owner'})
    result = await c.command({'action': 'record_stop', 'tab': PR_TAB, 'actor': 'google:owner'})
    assert result['name'] == 'closed-page.webm' and not c.recording
    assert c.page is None and not c.review_pages


@pytest.mark.asyncio
@pytest.mark.parametrize('lease_actor,lease_tab,expired,recording_tab,allowed', [
    ('google:owner', PR_TAB, False, None, True),
    ('google:owner', OTHER_TAB, False, None, True),
    ('google:other', OTHER_TAB, False, None, False),
    ('google:other', OTHER_TAB, True, None, True),
    ('', '', False, None, True),
    ('google:owner', PR_TAB, False, PR_TAB, False),
    ('google:owner', PR_TAB, True, PR_TAB, False),
    ('', '', False, PR_TAB, False),
    ('google:owner', OTHER_TAB, False, OTHER_TAB, True),
    ('google:other', OTHER_TAB, False, OTHER_TAB, False),
    ('google:other', OTHER_TAB, True, OTHER_TAB, True),
    ('', '', False, OTHER_TAB, True),
    ('google:owner', '', False, '', True),
])
async def test_close_lifecycle_preserves_other_tab_lease_frames_and_recording(
        monkeypatch, tmp_path, lease_actor, lease_tab, expired, recording_tab, allowed):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    monkeypatch.setattr(computer.time, 'monotonic', lambda: 100)
    c = computer.Computer()
    first, other, agent = BrowserPage('first'), BrowserPage('other'), BrowserPage('agent')
    c.page = agent
    c.track_review_page(PR_TAB, first)
    c.track_review_page(OTHER_TAB, other)
    c.frame_cache = {PR_TAB: ('first-frame', 1), OTHER_TAB: ('other-frame', 2), '': ('agent-frame', 3)}
    c.controller, c.controller_tab = lease_actor, lease_tab
    c.lease_until = 99 if expired else 160
    c.foreground_tab = recording_tab if recording_tab is not None else lease_tab
    recording = {'tab': recording_tab, 'path': tmp_path / 'active.partial'} if recording_tab is not None else None
    c.recording = recording
    body = {'action': 'close_tab', 'tab': PR_TAB, 'actor': 'google:owner'}
    if allowed:
        assert not (await c.command(body))['available']
        assert first.closed and PR_TAB not in c.review_pages and PR_TAB not in c.frame_cache
    else:
        with pytest.raises(ValueError, match='Another person|Stop the recording'):
            await c.command(body)
        assert not first.closed and c.page_for(PR_TAB) is first
        assert c.frame_cache[PR_TAB] == ('first-frame', 1)
    expected_actor = '' if expired or (allowed and lease_tab == PR_TAB) else lease_actor
    expected_tab = '' if expired or (allowed and lease_tab == PR_TAB) else lease_tab
    assert (c.controller, c.controller_tab) == (expected_actor, expected_tab)
    assert c.recording is recording and not other.closed and not agent.closed
    assert c.frame_cache[OTHER_TAB] == ('other-frame', 2) and c.frame_cache[''] == ('agent-frame', 3)
    assert c.foreground_tab == (recording_tab if recording_tab is not None else expected_tab)
    for page in (first, other, agent):
        page.bring_to_front.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_close_retains_surviving_popup_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    first, popup = BrowserPage('first'), BrowserPage('popup')
    c.track_review_page(PR_TAB, first)
    c.track_review_page(PR_TAB, popup)
    await c.command({'action': 'claim', 'tab': PR_TAB, 'actor': 'google:owner'})
    frame = c.frame_cache[PR_TAB]
    real_close = popup.close
    popup.close = AsyncMock(side_effect=RuntimeError('Browser transport disconnected'))
    body = {'action': 'close_tab', 'tab': PR_TAB, 'actor': 'google:owner'}
    with pytest.raises(RuntimeError, match='disconnected'):
        await c.command(body)
    assert first.closed and not popup.closed and c.page_for(PR_TAB) is popup
    assert c.frame_cache[PR_TAB] == frame
    assert c.controller == 'google:owner' and c.controller_tab == PR_TAB
    popup.close = real_close
    assert not (await c.command(body))['available']
    assert popup.closed and PR_TAB not in c.review_pages and PR_TAB not in c.frame_cache
    assert not c.controller


@pytest.mark.asyncio
@pytest.mark.parametrize('externally_closed', [False, True])
@pytest.mark.parametrize('lease_tab,recording_tab', [(OTHER_TAB, OTHER_TAB), (PR_TAB, None), ('', ''), (OTHER_TAB, None)])
async def test_absent_close_preserves_other_actors_control_and_recordings(
        monkeypatch, tmp_path, externally_closed, lease_tab, recording_tab):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    monkeypatch.setattr(computer.time, 'monotonic', lambda: 100)
    c = computer.Computer()
    c.page = BrowserPage('agent')
    other = BrowserPage('other')
    c.track_review_page(OTHER_TAB, other)
    if externally_closed:
        closed = BrowserPage('closed')
        c.track_review_page(PR_TAB, closed)
        await closed.close()
        closed.close = AsyncMock(side_effect=AssertionError('Already closed'))
    c.frame_cache = {PR_TAB: ('stale-frame', 1), OTHER_TAB: ('other-frame', 2), '': ('agent-frame', 3)}
    c.controller, c.controller_tab, c.lease_until = 'google:other', lease_tab, 160
    c.foreground_tab = lease_tab
    recording = {'tab': recording_tab, 'path': tmp_path / 'active.partial'} if recording_tab is not None else None
    c.recording = recording
    result = await c.command({'action': 'close_tab', 'tab': PR_TAB, 'actor': 'google:owner'})
    assert result['tab'] == PR_TAB and not result['available']
    assert PR_TAB not in c.review_pages and PR_TAB not in c.frame_cache
    assert (c.controller, c.controller_tab, c.lease_until) == ('google:other', lease_tab, 160)
    assert c.recording is recording and c.foreground_tab == ('' if lease_tab == PR_TAB else lease_tab)
    assert not other.closed and not c.page.closed
    assert c.frame_cache == {OTHER_TAB: ('other-frame', 2), '': ('agent-frame', 3)}
    other.bring_to_front.assert_not_awaited()
    c.page.bring_to_front.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('externally_closed', [False, True])
@pytest.mark.parametrize('expired', [False, True])
async def test_absent_recording_owner_still_requires_explicit_stop(monkeypatch, tmp_path, externally_closed, expired):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    monkeypatch.setattr(computer.time, 'monotonic', lambda: 100)
    c = computer.Computer()
    if externally_closed:
        closed = BrowserPage('closed')
        c.track_review_page(PR_TAB, closed)
        await closed.close()
    c.frame_cache[PR_TAB] = ('last-recorded-frame', 1)
    c.controller, c.controller_tab, c.lease_until = 'google:other', PR_TAB, 99 if expired else 160
    recording = {'tab': PR_TAB, 'path': tmp_path / 'recording.partial'}
    c.recording = recording
    with pytest.raises(ValueError, match='Stop the recording'):
        await c.command({'action': 'close_tab', 'tab': PR_TAB, 'actor': 'google:owner'})
    assert c.recording is recording and c.frame_cache[PR_TAB] == ('last-recorded-frame', 1)
    assert c.page is None


@pytest.mark.asyncio
@pytest.mark.parametrize('actor,tab,expired', [
    ('agent', '', False), ('google:owner', '', False), ('google:owner', PR_TAB, False),
    ('google:owner', '', True), ('google:owner', PR_TAB, True),
])
async def test_agent_turn_finish_preserves_human_recording_ownership(monkeypatch, tmp_path, actor, tab, expired):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    clock, signals = [0], []
    monotonic = computer.time.monotonic
    monkeypatch.setattr(computer.time, 'monotonic', lambda: monotonic() + clock[0])
    c = computer.Computer()
    c.page = BrowserPage('agent')
    c.open = AsyncMock()
    c.ensure_desktop = AsyncMock()
    if tab:
        c.track_review_page(tab, BrowserPage('review'))
    process = SimpleNamespace(returncode=None, send_signal=signals.append)
    async def finished():
        process.returncode = 0
        return 0
    process.wait = AsyncMock(side_effect=finished)
    async def spawn(*args, **kwargs):
        assert args[0] == 'ffmpeg'
        Path(args[-1]).write_bytes(WEBM)
        return process
    monkeypatch.setattr(computer.asyncio, 'create_subprocess_exec', spawn)
    if actor != 'agent':
        await c.command({'action': 'claim', 'actor': actor, 'tab': tab})
    await c.command({'action': 'record_start', 'actor': actor, 'tab': tab, 'args': {'name': 'owned'}})
    recording = c.recording
    if expired:
        clock[0] = 61
    await c.command({'action': 'finish'})  # Exact agent-finally and turn-artifact request.
    if actor == 'agent':
        assert not c.recording and signals == [computer.signal.SIGINT]
        assert recording['path'].with_suffix('.webm').exists()
        return
    assert c.recording is recording and recording['actor'] == actor and recording['tab'] == tab
    assert not signals and recording['path'].exists() and not recording['path'].with_suffix('.webm').exists()
    assert (c.controller, c.controller_tab) == (('', '') if expired else (actor, tab))
    clock[0] = 61  # Expiring human control must not transfer capture ownership to the agent.
    with pytest.raises(ValueError, match='recording'):
        await c.command({'action': 'record_stop'})
    assert c.recording is recording and not signals
    await c.command({'action': 'claim', 'actor': actor, 'tab': tab})
    await c.command({'action': 'record_stop', 'actor': actor, 'tab': tab})
    assert not c.recording and signals == [computer.signal.SIGINT]
    assert recording['path'].with_suffix('.webm').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('tab', ['', PR_TAB])
async def test_whole_sandbox_finish_finalizes_human_capture_without_taking_control(monkeypatch, tmp_path, tab):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    path = tmp_path / 'teardown.partial'
    path.write_bytes(WEBM)
    c.recording = {'actor': 'google:owner', 'tab': tab, 'path': path, 'started': computer.time.time(),
                   'process': SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))}
    c.controller, c.controller_tab = 'google:owner', tab
    c.lease_until = computer.time.monotonic() + 60
    result = await c.command({'action': 'finish', 'args': {'all': True}})
    assert result['name'] == 'teardown.webm' and not c.recording
    assert (c.controller, c.controller_tab) == ('google:owner', tab)


@pytest.mark.asyncio
async def test_pr_input_uses_content_coordinates_and_validates_before_any_event(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    page = BrowserPage('review')
    c.track_review_page(PR_TAB, page)
    initial = await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    assert initial['surface'] == 'browser' and initial['tab'] == PR_TAB
    c.desktop = SimpleNamespace(poll=lambda: None)
    c.window_command = AsyncMock(return_value=None)
    c.frame = 'desktop-frame'
    c.desktop_input = AsyncMock(side_effect=AssertionError('PR input must not use xdotool'))
    c.clear_clipboard = AsyncMock(side_effect=AssertionError('PR input must not use desktop clipboard'))
    assert (await c.state())['surface'] == 'desktop' and (await c.state())['frame'] == 'desktop-frame'
    sent = []
    for name in ('move', 'down', 'up', 'wheel'):
        setattr(page.mouse, name, AsyncMock(side_effect=lambda *args, _name=name, **kwargs: sent.append((_name, args, kwargs))))
    for name in ('insert_text', 'press'):
        setattr(page.keyboard, name, AsyncMock(side_effect=lambda *args, _name=name, **kwargs: sent.append((_name, args, kwargs))))
    invalid = [{'type': 'text', 'text': 'must not type'}, {'type': 'pointer', 'phase': 'move', 'x': float('nan'), 'y': 2}]
    with pytest.raises(ValueError):
        await c.command({'action': 'input', 'actor': 'owner', 'tab': PR_TAB, 'args': {'events': invalid}})
    assert not sent
    page.bring_to_front.assert_not_awaited()
    events = [{'type': 'pointer', 'phase': 'down', 'x': 22, 'y': 88},
              {'type': 'pointer', 'phase': 'move', 'x': 45, 'y': 90}, {'type': 'pointer', 'phase': 'up'},
              {'type': 'paste', 'text': 'text fixture'}, {'type': 'key', 'key': 'Meta+a'},
              {'type': 'scroll', 'dx': 30, 'dy': -1600}]
    result = await c.command({'action': 'input', 'actor': 'owner', 'tab': PR_TAB, 'args': {'events': events}})
    assert sent == [('move', (22, 88), {}), ('down', (), {'button': 'left'}), ('move', (45, 90), {}),
                    ('up', (), {'button': 'left'}), ('insert_text', ('text fixture',), {}),
                    ('press', ('Control+a',), {}), ('wheel', (30, -1200), {})]
    assert result['surface'] == 'browser' and result['controller_tab'] == PR_TAB and result['frame']
    assert c.frame == 'desktop-frame'
    c.desktop_input.assert_not_awaited()
    c.clear_clipboard.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_pr_drag_releases_original_page_before_new_tab_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    c = computer.Computer()
    first, second = BrowserPage('first'), BrowserPage('second')
    c.track_review_page(PR_TAB, first)
    c.track_review_page(OTHER_TAB, second)
    await c.command({'action': 'claim', 'actor': 'owner', 'tab': PR_TAB})
    await c.command({'action': 'input', 'actor': 'owner', 'tab': PR_TAB,
                     'args': {'events': [{'type': 'pointer', 'phase': 'down', 'x': 10, 'y': 10}]}})
    c.clear_clipboard = AsyncMock()
    c.lease_until = 0
    first.mouse.up.side_effect = RuntimeError('Pointer release interrupted')
    with pytest.raises(RuntimeError, match='Pointer release'):
        await c.command({'action': 'claim', 'actor': 'next-owner', 'tab': OTHER_TAB})
    assert c.release_pending and c.release_tab == PR_TAB and not c.controller
    first.mouse.up.reset_mock(side_effect=True)
    result = await c.command({'action': 'claim', 'actor': 'next-owner', 'tab': OTHER_TAB})
    assert [call.kwargs['button'] for call in first.mouse.up.await_args_list] == ['left', 'middle', 'right']
    second.mouse.up.assert_not_awaited()
    c.clear_clipboard.assert_not_awaited()
    assert result['controller'] == 'next-owner' and result['controller_tab'] == OTHER_TAB
    await c.command({'action': 'release', 'actor': 'owner', 'tab': PR_TAB})
    assert c.controller == 'next-owner' and c.controller_tab == OTHER_TAB
    second.mouse.up.assert_not_awaited()


@pytest.mark.parametrize('fallback,expected', [
    ({'available': True, 'recording': False}, 200),
    ({'available': True, 'recording': False, 'error': 'State unavailable'}, 503),
    ({'available': False, 'recording': False, 'tab': PR_TAB}, 503),
])
def test_named_close_legacy_bridge_fallback_requires_healthy_unscoped_state(workspace, fallback, expected):
    app, client, _, url = cloud(workspace)
    hub = app.state.computer
    native = AsyncMock(side_effect=OSError('Legacy script has no bridge'))
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=native))
    hub.execute = AsyncMock(return_value=fallback)
    response = client.post(url, json={'action': 'close_tab', 'tab': PR_TAB})
    assert response.status_code == expected
    assert native.await_count == 2 and all(call.args[0]['action'] == 'state' for call in native.await_args_list)
    assert hub.execute.await_count == 1 and json.loads(hub.execute.call_args.args[2])['action'] == 'state'


def test_wake_uses_existing_authorization_and_preserves_conversation(workspace, monkeypatch):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    hub.manager.wake_computer = AsyncMock()
    hub.manager.state = lambda _: {'computer_only': True, 'phase': 'provision'}
    hub.manager.computer_state = lambda _: {'waking': True, 'can_wake': False}
    messages = app.state.store.messages(rid)
    hub.sandbox = AsyncMock(return_value=None)
    monkeypatch.setattr(app.state.security, 'role', lambda _: 'member')
    assert client.post(url, json={'action': 'wake'}).status_code == 403
    monkeypatch.setattr(app.state.store, 'identity', lambda _: 'google:owner')
    assert client.post(url, json={'action': 'wake'}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    hub.manager.wake_computer.assert_not_awaited()
    result = client.post(url, json={'action': 'wake'})
    assert result.status_code == 200 and result.json()['waking'] is True
    hub.manager.wake_computer.assert_awaited_once_with(rid)
    assert app.state.store.messages(rid) == messages
    assert client.get(url).json()['waking'] is True
    assert not client.get(url).json()['can_wake']
    app.state.store.update_run(rid, status='stopping')
    assert client.post(url, json={'action': 'wake'}).status_code == 409
    app.state.store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (rid,))
    assert client.post(url, json={'action': 'wake'}).status_code == 404


def test_wake_distinguishes_sleep_transport_failure_and_legacy_runtime(workspace):
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    hub.sandbox = AsyncMock(return_value=None)
    assert client.get(url).json()['wake_supported'] is False
    assert client.post(url, json={'action': 'wake'}).status_code == 409
    hub.manager.wake_computer = AsyncMock()
    hub.manager.state = lambda _: {'phase': 'idle'}
    hub.manager.computer_state = lambda _: {'waking': False, 'can_wake': True}
    hub.cache.clear()
    assert client.get(url).json()['can_wake'] is True
    hub.cache.clear()
    hub.sandbox = AsyncMock(side_effect=ConnectionError('private provider error'))
    response = client.get(url)
    assert response.status_code == 503 and not hub.cache
    assert 'private provider error' not in response.text
    assert 'can_wake' not in response.json() and 'available' not in response.json()


@pytest.mark.parametrize('phase', ['prepare', 'provision', 'waiting_environment', 'waiting_children'])
def test_computer_panel_uses_durable_admission_during_an_agent_turn(workspace, phase):
    from app.temporal_runtime import TemporalRunManager
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    hub.manager = TemporalRunManager(app.state.store, app.state.settings)
    hub.manager.submit(app.state.store.run(rid))
    hub.manager.save(rid, {'phase': phase})
    app.state.store.update_run(rid, status='running')
    hub.sandbox = AsyncMock(return_value=None)
    state = client.get(url).json()
    assert not state['can_wake'] and not state['waking']
    assert state['starting'] == (phase != 'waiting_children')
    assert state['wake_notice'] and 'asleep' not in state['wake_notice'].lower()
    hub.cache.clear()
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=AsyncMock(
        return_value={'available': True, 'surface': 'desktop', 'frame': 'live'})))
    live = client.get(url).json()
    assert live['available'] and live['has_sandbox'] and live['frame'] == 'live'
    assert not live['can_wake']


@pytest.mark.parametrize('phase', ['cleanup', 'warm_cleanup'])
@pytest.mark.parametrize('late', [False, True])
def test_cleanup_fences_live_commands_even_after_connection_lookup(workspace, phase, late):
    from app.temporal_runtime import TemporalRunManager
    app, client, rid, url = cloud(workspace)
    hub = app.state.computer
    hub.manager = TemporalRunManager(app.state.store, app.state.settings)
    hub.manager.submit(app.state.store.run(rid))
    app.state.store.update_run(rid, status='idle', sandbox_id='live-machine')
    cleanup = {'phase': phase, 'computer_only': True, 'computer_error': 'Desktop startup failed.'}
    transport = AsyncMock(return_value={'available': True, 'surface': 'desktop', 'frame': 'live'})
    async def lookup(run):
        if late:
            hub.manager.save(rid, cleanup)
        return SimpleNamespace(computer_request=transport)
    hub.sandbox = lookup
    for action in ['claim', 'open', 'input', 'record_start', 'wake']:
        hub.connections.clear()
        hub.manager.save(rid, {'phase': 'install', 'computer_only': True} if late and action != 'wake' else cleanup)
        assert client.post(url, json={'action': action}).status_code == 409
    transport.assert_not_awaited()
    state = client.get(url).json()
    assert state['shutting_down'] and state['available'] and state['frame'] == 'live'
    assert not state['can_wake']
    # Verified remote absence still permits dismissal while cleanup is pending.
    transport.return_value = {'available': False, 'recording': False, 'tab': PR_TAB}
    assert client.post(url, json={'action': 'close_tab', 'tab': PR_TAB}).status_code == 200
    assert all(call.args[0]['action'] == 'state' for call in transport.await_args_list)


async def test_native_wake_starts_display_without_claim_or_browser_navigation(monkeypatch, tmp_path):
    monkeypatch.setattr(computer, 'CAPTURES', tmp_path)
    desktop = computer.Computer()
    desktop.ensure_desktop = AsyncMock()
    desktop.refresh_frame = AsyncMock()
    desktop.open = AsyncMock()
    result = await desktop.command({'action': 'wake'})
    desktop.ensure_desktop.assert_awaited_once()
    desktop.refresh_frame.assert_awaited_once()
    desktop.open.assert_not_awaited()
    assert result['controller'] == ''
