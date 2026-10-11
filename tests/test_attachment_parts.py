import hashlib
from io import BytesIO
from uuid import uuid4

import httpx
import pytest

from app.attachment_uploads import AttachmentUploads, PART_BYTES
from app.attachment_transport import CONTENT_TYPE
from app.attachments import attachment_context
from app.db import Store
from fastapi import HTTPException
from app.storage_maintenance import migrate, plan_connection, verify
from sandbox.attachments import prepare_attachments
from test_attachment_transport import packet
from test_attachments import png, start, storage_mode
from test_audio import configured, gateway, wav
from test_spend import sign_in
from test_workspace import workspace


def begin(client, raw, name='large.bin', attachment_id=None):
    attachment_id = attachment_id or uuid4().hex
    result = client.post(f'/api/attachments/{attachment_id}/upload', json={'name':name, 'size':len(raw)})
    assert result.status_code == 200, result.text
    return attachment_id, result.json()


def part(client, attachment_id, name, index, raw):
    data = packet(client.headers['X-CSRF-Token'], f'{attachment_id}:{index}', name, raw)
    return client.put(f'/api/attachments/{attachment_id}/parts/{index}', params={'name':name},
                      content=data, headers={'Content-Type':CONTENT_TYPE})


def upload(client, raw, name='large.bin', attachment_id=None):
    attachment_id, meta = begin(client, raw, name, attachment_id)
    for index, offset in enumerate(range(0,len(raw),meta['part_size'])):
        result = part(client, attachment_id, name, index, raw[offset:offset+meta['part_size']])
        assert result.status_code == 200, result.text
    result = client.post(f'/api/attachments/{attachment_id}/complete')
    assert result.status_code == 200, result.text
    return result.json()


def test_complete_large_files_survive_publication_download_and_new_computer(workspace, monkeypatch, storage_mode, tmp_path):
    app, client = workspace
    raw = bytes(range(256)) * (11*1024*1024//256+1)
    first = upload(client, raw)
    second = upload(client, raw[::-1], 'second.bin')
    assert client.get(first['url']).content == raw
    assert start(app, client, monkeypatch, [first,second]).status_code == 201
    stored = app.state.store.rows('SELECT * FROM attachments WHERE id=?', (first['id'],))[0]
    assert stored['sha256'] == hashlib.sha256(raw).hexdigest()
    assert stored['data'] == b'' and stored['data_ref'] == '' and stored['upload_state'] == 'ready'
    assert max(r['size'] for r in app.state.store.rows('SELECT size FROM attachment_parts')) <= PART_BYTES
    class Download(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= PART_BYTES
            return super().read(size)
    spec = {'broker_url':'https://broker.example', 'attachments':[
        {'id':first['id'],'name':first['name'],'size':len(raw),'sha256':stored['sha256']}]}
    prepare_attachments(spec, 'test-token', root=tmp_path/'files', opener=lambda *a,**kw: Download(raw))
    assert (tmp_path/'files'/first['id']/first['name']).read_bytes() == raw
    assert Store(app.state.settings.data_dir).rows('SELECT size FROM attachments WHERE id=?',(first['id'],))[0]['size'] == len(raw)


def test_partial_upload_is_private_unsendable_and_retryable(workspace, monkeypatch, storage_mode):
    app, client = workspace
    sign_in(app, client)
    raw = b'x'*(PART_BYTES+7)
    attachment_id, _ = begin(client, raw)
    base = '/api/attachments/'+attachment_id
    assert part(client,attachment_id,'large.bin',0,raw[:PART_BYTES]).status_code == 200
    assert client.post(base+'/complete').status_code == 409
    assert client.get(base).status_code == 409
    assert start(app,client,monkeypatch,[{'id':attachment_id}]).status_code == 409
    assert not app.state.store.rows('SELECT id FROM messages')
    sign_in(app,client,'bob','bob@berri.ai')
    assert client.post(base+'/complete').status_code == 404
    assert part(client,attachment_id,'large.bin',1,raw[PART_BYTES:]).status_code == 404
    sign_in(app,client)
    receipt = part(client,attachment_id,'large.bin',0,raw[:PART_BYTES])
    assert receipt.status_code == 200
    assert part(client,attachment_id,'large.bin',0,b'y'*PART_BYTES).status_code == 409
    assert part(client,attachment_id,'large.bin',1,raw[PART_BYTES:]).status_code == 200
    file = client.post(base+'/complete').json()
    assert client.post(base+'/complete').json() == file
    assert client.get(base).content == raw
    assert len(app.state.store.rows('SELECT part FROM attachment_parts')) == 2
    client.delete(base)
    assert not app.state.store.rows('SELECT part FROM attachment_parts')


def test_part_identity_and_declared_range_are_authenticated(workspace):
    app,client=workspace
    attachment_id,_=begin(client,b'x'*(PART_BYTES+1))
    sealed=packet(client.headers['X-CSRF-Token'],attachment_id+':0','large.bin',b'x'*PART_BYTES)
    wrong=client.put(f'/api/attachments/{attachment_id}/parts/1',params={'name':'large.bin'},content=sealed,
                     headers={'Content-Type':CONTENT_TYPE})
    assert wrong.status_code == 422
    assert part(client,attachment_id,'large.bin',0,b'x').status_code == 422
    assert part(client,attachment_id,'large.bin',2,b'x').status_code == 422
    assert part(client,attachment_id,'large.bin',-1,b'x').status_code == 422
    assert not app.state.store.rows('SELECT part FROM attachment_parts')


def test_preview_and_transcription_failure_do_not_discard_originals(workspace):
    app,client=workspace
    file=upload(client,wav(),'voice.wav')
    assert 'not configured' in file['processing_error'] and not file['transcript']
    assert client.get(file['url']).content == wav()
    context=attachment_context([{**file,'path':'/workspace/voice.wav'}])
    assert 'original file is retained' in context and 'not configured' in context
    image=upload(client,png(),'image.png')
    assert client.get(image['preview_url']).content.startswith(b'\xff\xd8')


def test_audio_ranges_read_only_verified_parts_without_full_file_buffers(workspace, monkeypatch):
    app, client = workspace
    raw = wav()+b'\0'*(PART_BYTES*3)
    file = upload(client,raw,'large.wav')
    monkeypatch.setattr(app.state.store.attachments, 'payload', lambda *a,**k: pytest.fail('Whole-file buffer'))
    response = client.get(file['audio_url'], headers={'Range':f'bytes={PART_BYTES-3}-{PART_BYTES+7}'})
    assert response.status_code == 206 and response.content == raw[PART_BYTES-3:PART_BYTES+8]
    assert response.headers['content-range'] == f'bytes {PART_BYTES-3}-{PART_BYTES+7}/{len(raw)}'
    assert client.get(file['audio_url'], headers={'Range':'bytes=-15'}).content == raw[-15:]
    assert client.get(file['audio_url'], headers={'Range':f'bytes={len(raw)}-'}).status_code == 416


def test_slack_file_normalization_exhausts_sdk_batches():
    from app.slack_files import file_ids
    files = [{'id':f'F{i:08d}'} for i in range(230)]
    assert file_ids([*files,files[0],{'id':'not-a-file'}]) == [item['id'] for item in files]


def test_referenced_images_are_not_silently_capped_at_ten(workspace,monkeypatch):
    app,client=workspace
    files=[upload(client,png(),f'image-{i}.png') for i in range(12)]
    run_id=start(app,client,monkeypatch,files).json()['id']
    app.state.store.claim_message(run_id)
    content='\n'.join('[moyai-attachment:'+file['id']+']' for file in files)
    messages=app.state.store.attachments.with_images(app.state.store.run(run_id),[{'role':'user','content':content}])
    assert len(messages[0]['content']) == 13


def test_long_transcript_is_complete_and_billed_once_on_completed_retry(workspace,monkeypatch):
    app,client=workspace
    configured(app)
    text='Complete transcript 🗿\n'*20000
    calls=[]
    gateway(monkeypatch,lambda r: calls.append(r) or httpx.Response(200,json={'text':text}))
    file=upload(client,wav(),'voice.wav')
    assert file['transcript'] == text.strip()
    assert client.post('/api/attachments/'+file['id']+'/complete').json() == file
    assert len(calls) == 1


def test_storage_budget_reserves_drafts_and_parts_migrate_without_changing_manifest(workspace,monkeypatch):
    app,client=workspace
    app.state.settings.attachment_storage_limit_mb = 2
    first=upload(client,b'x'*PART_BYTES)
    attachment_id,_=begin(client,b'y'*PART_BYTES)
    assert client.post('/api/attachments/'+uuid4().hex+'/upload',json={'name':'third','size':1}).status_code == 422
    client.delete('/api/attachments/'+attachment_id)
    from storage_fixture import MemoryObjects
    store=app.state.store
    original=store.rows('SELECT sha256 FROM attachments')[0]['sha256']
    store.objects=MemoryObjects()
    with store.connect() as conn:
        assert plan_connection(store.path.parent,conn)['attachment_parts']['pending'] == 1
    assert migrate(store,clear_attachment_blobs=True)['attachment_parts_published'] == 1
    assert verify(store)['verified_objects'] == 1
    assert store.rows('SELECT sha256 FROM attachments')[0]['sha256'] == original
    assert client.get(first['url']).content == b'x'*PART_BYTES


@pytest.mark.parametrize('boundary',['part','preview'])
def test_late_object_save_cannot_publish_into_a_replaced_draft(workspace,monkeypatch,boundary):
    app,client=workspace
    from storage_fixture import MemoryObjects
    from app.attachments import inspect_file
    store=app.state.store
    store.objects=objects=MemoryObjects()
    uploads=AttachmentUploads(store.attachments)
    attachment_id=uuid4().hex
    uploads.begin(attachment_id,'owner','old.bin',3,100000)
    if boundary=='preview':
        uploads.part(attachment_id,'owner',0,b'old')
        original,stream,checksum=uploads.stage(attachment_id,'owner')
        stream.close()
    put=objects.put
    def replace(raw):
        store.execute('DELETE FROM attachments WHERE id=?',(attachment_id,))
        uploads.begin(attachment_id,'owner','replacement.bin',3,100000)
        return put(raw)
    monkeypatch.setattr(objects,'put',replace)
    with pytest.raises(HTTPException) as error:
        if boundary=='part': uploads.part(attachment_id,'owner',0,b'old')
        else: uploads.finish(original,checksum,inspect_file(png()),100000)
    assert error.value.status_code==409
    row=store.rows('SELECT * FROM attachments')[0]
    assert row['name']=='replacement.bin' and row['upload_state']=='uploading'
    assert not store.rows('SELECT * FROM attachment_parts')
