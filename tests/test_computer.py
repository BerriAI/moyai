import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import captures
from app.computer import DesktopConnection
from sandbox import computer
from test_workspace import workspace

PNG = b'\x89PNG\r\n\x1a\n' + b'capture bytes'
WEBM = b'\x1aE\xdf\xa3\x42\x82\x84webm' + bytes(range(100))


def cloud(workspace):
    app, client = workspace
    run = app.state.store.create_run('Browser test', '', 'modal', [], chat_enabled=True,
                                    user_id='google:owner')
    return app, client, run['id'], f"/api/runs/{run['id']}/computer"


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


def test_saved_media_survives_sleep_supports_ranges_and_blocks_paths(workspace):
    app, client, rid, url = cloud(workspace)
    directory = captures.directory(app.state.settings, rid)
    directory.mkdir(parents=True)
    (directory/'image-a.png').write_bytes(PNG)
    (directory/'flow-b.webm').write_bytes(WEBM)
    (directory/'evil.png').write_bytes(b'<html><script>alert(1)</script>')
    (directory/'link.png').symlink_to(directory/'image-a.png')
    listing = client.get(f'/api/runs/{rid}/files').json()['files']
    assert {f['name'] for f in listing} == {'image-a.png','flow-b.webm','evil.png'}
    assert client.get(f'/api/runs/{rid}').json()['has_captures']
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
    client.cookies.clear()
    assert client.get(media).status_code == 401


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_rejects_bad_or_oversized_media(workspace):
    app, _, rid, _ = cloud(workspace)
    hub = app.state.computer
    hub.execute = AsyncMock(return_value={'data': base64.b64encode(PNG).decode()})
    hub.manager.persist = AsyncMock()
    rows = [{'name':'proof.png','size':len(PNG)}]
    await hub.sync(object(), rid, rows)
    await hub.sync(object(), rid, rows)
    assert hub.execute.await_count == 1
    assert hub.manager.persist.await_count == 1
    assert (captures.directory(app.state.settings,rid)/'proof.png').read_bytes() == PNG
    await hub.sync(object(),rid,[{'name':'../escape.png','size':1}, {'name':'huge.webm','size':captures.MAX_FILE+1}])
    assert hub.execute.await_count == 1
    hub.execute.return_value = {'data':base64.b64encode(b'not an image').decode()}
    with pytest.raises(Exception,match='supported image'):
        await hub.sync(object(),rid,[{'name':'invalid.png','size':len(b'not an image')}])
    assert not (captures.directory(app.state.settings,rid)/'invalid.png').exists()


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
    assert hub.sandbox.await_count == 1
    clock[0] += 21
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
