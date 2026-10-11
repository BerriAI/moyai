import base64
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from agent.tools import artifact_tools
from app.published_artifacts import text_page
from storage_fixture import MemoryObjects
from test_workspace import workspace
from test_spend import active, sign_in


@pytest.fixture
def outputs(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    return app, client, run, app.state.published_artifacts


def begin(service, run, raw, path='output.bin'):
    identity = uuid4().hex
    service.transfer(run, dict(action='begin', upload_id=identity, path=path, size=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    return identity


def save(service, run, raw, path='output.bin'):
    identity = begin(service, run, raw, path)
    parts = 0
    for offset in range(0, len(raw), 1024 * 1024):
        service.transfer(run, dict(action='part', upload_id=identity, part=parts, data=base64.b64encode(raw[offset:offset+1024*1024]).decode()))
        parts += 1
    return service.transfer(run, dict(action='commit', upload_id=identity, parts=parts))


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('raw', [('🗿résult\n'*300000).encode(), bytes(range(256))*10000, b''])
def test_complete_roundtrip_versions_and_http_ranges(outputs, remote, raw):
    app, client, run, service = outputs
    if remote:
        app.state.store.objects = MemoryObjects()
    first = save(service, run, raw, 'reports/complete.md')
    assert client.get(first['url']).content == raw
    download = client.get(first['url'])
    assert download.headers['cache-control'] == 'no-store'
    if raw:
        response = client.get(first['url'], headers={'Range': 'bytes=17-2001'})
        assert response.status_code == 206 and response.content == raw[17:2002]
        assert client.get(first['url'], headers={'Range': 'bytes=-5'}).content == raw[-5:]
        assert client.get(first['url'], headers={'Range': 'bytes=0-1', 'If-Range': '"different"'}).content == raw
    assert client.get(first['url'], headers={'Range': 'bytes=999999999-'}).status_code == 416
    collected = bytearray()
    while True:
        page = service.call(run, 'artifact_read', dict(path=first['path'], revision=first['revision'], offset=len(collected), length=73119))
        collected.extend(base64.b64decode(page['data_base64']))
        if page['next_offset'] is None:
            break
        assert page['next_offset'] == len(collected)
    assert bytes(collected) == raw
    second = save(service, run, b'new version', first['path'])
    assert service.listing(run['id'])[0]['revision'] == second['revision']
    assert client.get(first['url']).content == raw
    files = client.get(f"/api/runs/{run['id']}/files").json()['files']
    assert files[0]['url'] == second['url']
    assert client.get(f"/api/runs/{run['id']}").json()['has_published_artifacts']
    client.cookies.clear()
    assert client.get(first['url']).status_code == 401


def test_atomic_visibility_identity_and_post_commit_immutability(outputs):
    app, client, run, service = outputs
    raw = b'complete'
    identity = begin(service, run, raw)
    with pytest.raises(HTTPException, match='incomplete'):
        service.transfer(run, dict(action='commit', upload_id=identity, parts=1))
    assert service.listing(run['id']) == []
    part = dict(action='part', upload_id=identity, part=0, data=base64.b64encode(raw).decode())
    assert service.transfer(run, part) == service.transfer(run, part)
    with pytest.raises(HTTPException, match='different bytes'):
        service.transfer(run, {**part, 'data': base64.b64encode(b'wrong').decode()})
    committed = service.transfer(run, dict(action='commit', upload_id=identity, parts=1))
    assert service.transfer(run, dict(action='commit', upload_id=identity, parts=1)) == committed
    assert len(app.state.store.rows("SELECT * FROM events WHERE kind='artifact' AND run_id=?", (run['id'],))) == 1
    with pytest.raises(HTTPException, match='immutable'):
        service.transfer(run, {**part, 'part': 1})
    bad = begin(service, run, b'expected', 'bad')
    service.transfer(run, {**part, 'upload_id': bad})
    with pytest.raises(HTTPException, match='hash'):
        service.transfer(run, dict(action='commit', upload_id=bad, parts=1))
    assert len(service.listing(run['id'])) == 1


@pytest.mark.parametrize('change', [{'status': 'cancelled'}, {'token_hash': 'changed'}, {'active_user_id': 'other'}, {'active_message_id': 999}])
def test_old_execution_cannot_finish_upload_or_read(outputs, change):
    app, client, run, service = outputs
    identity = begin(service, run, b'')
    key, value = next(iter(change.items()))
    app.state.store.execute(f'UPDATE runs SET {key}=? WHERE id=?', (value, run['id']))
    with pytest.raises(HTTPException, match='capability changed'):
        service.transfer(run, dict(action='commit', upload_id=identity, parts=0))
    with pytest.raises(HTTPException, match='capability changed'):
        service.call(run, 'artifacts_list', {})
    assert service.listing(run['id']) == []


def test_direct_children_only_and_exact_revision(outputs):
    app, client, run, service = outputs
    child = active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], child['id']))
    value = save(service, child, b'child')
    assert service.call(run, 'artifact_read', dict(run_id=child['id'], path=value['path'], revision=value['revision']))['text'] == 'child'
    other = active(app)
    with pytest.raises(HTTPException, match='direct child'):
        service.call(other, 'artifacts_list', dict(run_id=child['id']))
    with pytest.raises(HTTPException, match='revision not found'):
        service.call(run, 'artifact_read', dict(run_id=child['id'], path=value['path'], revision='0'*64))


def test_preview_continues_across_utf8_boundary(outputs):
    app, client, run, service = outputs
    raw = ('x'*65535+'🗿héllo'*10000).encode()
    value = save(service, run, raw, 'report.md')
    preview = client.get(value['preview_url']).json()
    assert preview['text'].endswith('🗿') and preview['next_offset'] == 65539
    while preview['next_offset'] is not None:
        more = client.get(value['preview_url']+'&offset='+str(preview['next_offset'])).json()
        preview = {**more, 'text': preview['text']+more['text']}
    assert preview['text'].encode() == raw
    emoji = '🗿'.encode()
    page = text_page(lambda start, count: emoji[start:start+count], 0, 1, len(emoji))
    assert page['text'] == '🗿' and page['next_offset'] is None


def test_local_tools_retry_lost_ack_and_fetch_exact_bytes(outputs, tmp_path, monkeypatch):
    app, client, run, service = outputs
    raw = bytes(range(256))*10000
    source = tmp_path/'source.bin'; source.write_bytes(raw)
    lost = set()
    def broker(path, body):
        if path == '/artifacts/transfer':
            result = service.transfer(run, body)
            identity = body['action']+str(body.get('part', ''))
            if identity not in lost:
                lost.add(identity)
                raise OSError('ack lost')
            return result
        return service.call(run, body['name'], body['arguments'])
    monkeypatch.setattr(artifact_tools.time, 'sleep', lambda _: None)
    result = artifact_tools.call('artifact_publish', {'path': 'source.bin'}, broker, tmp_path)
    fetched = artifact_tools.call('artifact_fetch', {**{k: result[k] for k in ('path', 'revision')}, 'destination': 'copy.bin'}, broker, tmp_path)
    assert fetched['saved'] and (tmp_path/'copy.bin').read_bytes() == raw
    assert len(app.state.store.rows('SELECT * FROM published_artifacts')) == 1
    with pytest.raises(artifact_tools.ArtifactError, match='never overwrites'):
        artifact_tools.call('artifact_fetch', {'path': result['path'], 'revision': result['revision'], 'destination': 'copy.bin'}, broker, tmp_path)
    with pytest.raises(artifact_tools.ArtifactError, match='inside the workspace'):
        artifact_tools.call('artifact_publish', {'path': '../escape'}, broker, tmp_path)
    with pytest.raises(artifact_tools.ArtifactError, match='schema'):
        artifact_tools.call('artifact_publish', {'path': 'source.bin', 'extra': True}, broker, tmp_path)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'secret-credential')
    source.write_bytes(b'x'*(artifact_tools.CHUNK_BYTES-3)+b'secret-credential')
    with pytest.raises(artifact_tools.ArtifactError, match='credential'):
        artifact_tools.call('artifact_publish', {'path': 'source.bin'}, broker, tmp_path)


def test_broker_transfer_catalog_discovery_and_repairable_errors(outputs):
    app, client, run, service = outputs
    headers = {'Authorization': 'Bearer capability'}
    base = f"/broker/{run['id']}"
    names = {tool['name'] for tool in client.get(base+'/tools', headers=headers).json()}
    assert {'artifact_publish', 'artifact_fetch', 'artifact_read', 'artifacts_list'} <= names
    identity = uuid4().hex
    raw = b'broker output'
    for payload in [dict(action='begin', path='broker.txt', size=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
                    dict(action='part', part=0, data=base64.b64encode(raw).decode()), dict(action='commit', parts=1)]:
        response = client.post(base+'/artifacts/transfer', headers=headers, json=dict(upload_id=identity, **payload))
        assert response.status_code == 200
    assert response.json()['saved']
    invalid = client.post(base+'/tools/call', headers=headers, json={'name': 'artifact_read', 'arguments': {'path': 'PRIVATE-SENTINEL'}})
    assert invalid.json()['outcome'] == 'rejected' and 'PRIVATE-SENTINEL' not in invalid.text
    assert client.post(base+'/artifacts/transfer', headers=headers, json={'bad': 'PRIVATE-SENTINEL'}).status_code == 422
    app.state.store.update_run(run['id'], status='cancelled')
    assert client.get(base+'/tools', headers=headers).status_code == 401


def test_all_revisions_discoverable_and_chunk_storage_migration_verified(outputs):
    from app import storage_maintenance
    app, client, run, service = outputs
    for raw in (b'first', b'second'):
        save(service, run, raw)
    first = service.call(run, 'artifacts_list', {'revisions': True, 'page_size': 1})
    second = service.call(run, 'artifacts_list', {'revisions': True, 'page_size': 1, 'cursor': first['next']})
    assert {first['artifacts'][0]['revision'], second['artifacts'][0]['revision']} == {hashlib.sha256(raw).hexdigest() for raw in (b'first', b'second')}
    app.state.store.objects = MemoryObjects()
    assert storage_maintenance.migrate(app.state.store)['artifact_parts_published'] == 2
    assert storage_maintenance.verify(app.state.store)['verified_objects'] == 2
    assert app.state.store.rows('SELECT data FROM artifact_parts') == [{'data': b''}, {'data': b''}]
    value = first['artifacts'][0]
    assert client.get(value['url']).content == b'first'
    app.state.store.objects.values.clear()
    with pytest.raises(HTTPException):
        storage_maintenance.verify(app.state.store)


def test_explicit_image_preview_decodes_pixels_and_never_serves_active_html(outputs):
    from io import BytesIO
    from PIL import Image
    app, client, run, service = outputs
    raw = BytesIO(); Image.new('RGB', (24, 16), 'purple').save(raw, format='PNG')
    output = save(service, run, raw.getvalue(), 'proof.png')
    preview = client.get(output['inline_url'])
    assert preview.headers['content-type'] == 'image/jpeg'
    assert Image.open(BytesIO(preview.content)).size == (24, 16)
    assert client.get(output['url']).content == raw.getvalue()
    bad = save(service, run, b'<script>alert(1)</script>', 'bad.png')
    assert client.get(bad['inline_url']).status_code == 415
    assert client.get(bad['url']).headers['content-type'] == 'application/octet-stream'
