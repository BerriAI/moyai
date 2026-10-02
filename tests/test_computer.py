import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import captures
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
