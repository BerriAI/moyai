import hashlib
from io import BytesIO
import json
from uuid import uuid4

import httpx
from PIL import Image
import pytest

from app.attachments import MAX_FILE, inspect_file
from app.db import Store
from app.persistence import Checkpoints, restore_checkpoint
from app.security import digest
from sandbox.agent import conversation_prompt
from sandbox.attachments import prepare_attachments
from test_spend import sign_in
from test_workspace import workspace


def png():
    out = BytesIO()
    Image.new('RGB', (80, 60), 'purple').save(out, 'PNG')
    return out.getvalue()


def upload(client, name='SKILL.md', data=b'# Context\nUser-provided file.', attachment_id=None):
    attachment_id = attachment_id or uuid4().hex
    return client.put('/api/attachments/' + attachment_id, params={'name': name}, content=data,
                      headers={'Content-Type': 'application/octet-stream'})


def start(app, client, monkeypatch, files, prompt='Read these attachments'):
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    return client.post('/api/runs', json={'prompt': prompt, 'attachment_ids': [f['id'] for f in files], 'client_id': 'upload-new-session'})


def test_upload_send_retry_and_reopen_keep_files_on_the_exact_message(workspace, monkeypatch):
    app, client = workspace
    file = upload(client).json()
    assert file['preview_text'].startswith('# Context') and not file['preview_url']
    assert client.get(file['url']).content == b'# Context\nUser-provided file.'
    assert client.get(file['url']).headers['content-disposition'].startswith('attachment;')
    first = start(app, client, monkeypatch, [file])
    assert first.status_code == 201, first.text
    run_id = first.json()['id']
    assert start(app, client, monkeypatch, [file]).json()['id'] == run_id
    message = app.state.store.messages(run_id)[0]
    assert message['attachments'] == [file]
    # A saved file cannot be attached to another message or silently moved.
    endpoint = f'/api/runs/{run_id}/messages'
    body = {'content': 'Second turn', 'client_id': 'upload-follow-up', 'attachment_ids': [file['id']]}
    assert client.post(endpoint, json=body).status_code == 409
    second = upload(client, 'shot.png', png()).json()
    body['attachment_ids'] = [second['id']]
    assert client.post(endpoint, json=body).json()['created']
    assert client.post(endpoint, json=body).json()['created'] is False
    assert client.post(endpoint, json={**body, 'attachment_ids': []}).status_code == 409
    assert len(app.state.store.messages(run_id)) == 2
    reopened = Store(app.state.settings.data_dir)
    assert reopened.messages(run_id)[1]['attachments'][0]['id'] == second['id']
    # A draft-removal race after send cannot delete the attached file.
    assert client.delete(second['url']).status_code == 200
    assert client.get(second['url']).status_code == 200


def test_drafts_are_owner_only_and_sent_files_follow_shared_chat_access(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    file = upload(client, 'private.png', png()).json()
    sign_in(app, client, 'ishaan', 'ishaan@berri.ai')
    assert client.get(file['url']).status_code == 404
    assert client.get(file['preview_url']).status_code == 404
    assert start(app, client, monkeypatch, [file]).status_code == 409
    assert not app.state.store.rows('SELECT id FROM runs')
    client.delete(file['url'])
    sign_in(app, client)
    assert client.get(file['url']).status_code == 200
    assert start(app, client, monkeypatch, [file]).status_code == 201
    sign_in(app, client, 'ishaan', 'ishaan@berri.ai')
    assert client.get(file['url']).status_code == 200
    assert client.get(file['preview_url']).headers['content-type'] == 'image/jpeg'
    client.cookies.clear()
    assert client.get(file['url']).status_code == 401
    assert upload(client).status_code == 401


def test_upload_auth_limits_safe_names_and_idempotency(workspace, monkeypatch):
    app, client = workspace
    attachment_id = uuid4().hex
    first = upload(client, '../folder\\SKILL.md\r\n', attachment_id=attachment_id)
    assert first.status_code == 200, first.text
    assert first.json()['name'] == 'SKILL.md'
    assert upload(client, 'SKILL.md', attachment_id=attachment_id).json() == first.json()
    assert upload(client, data=b'different', attachment_id=attachment_id).status_code == 422
    assert upload(client, data=b'').status_code == 422
    assert upload(client, data=b'x' * (MAX_FILE + 1)).status_code == 413
    assert client.put('/api/attachments/' + uuid4().hex, params={'name': 'x'}, content=b'x', headers={'X-CSRF-Token': ''}).status_code == 403
    client.delete(first.json()['url'])
    assert client.get(first.json()['url']).status_code == 404
    monkeypatch.setattr('app.attachments.MAX_DRAFT', 5)
    assert upload(client, data=b'123456').status_code == 422
    assert not app.state.store.rows('SELECT id FROM attachments')


def test_preview_does_not_serve_html_svg_or_trust_claimed_mime(workspace):
    _, client = workspace
    dangerous = b'<svg onload="alert(1)">untrusted</svg>'
    file = upload(client, 'image.svg', dangerous).json()
    assert not file['preview_url'] and file['preview_text'] == dangerous.decode()
    download = client.get(file['url'])
    assert download.headers['content-type'] == 'application/octet-stream'
    assert download.headers['x-content-type-options'] == 'nosniff'
    image = upload(client, 'unusual.txt', png()).json()
    assert image['media_type'] == 'image/png'
    preview = client.get(image['preview_url'])
    assert preview.content.startswith(b'\xff\xd8')
    assert 'no-store' in preview.headers['cache-control']


def test_binding_rolls_back_whole_message_and_checks_total_size(workspace, monkeypatch):
    app, client = workspace
    file = upload(client).json()
    monkeypatch.setattr('app.attachments.MAX_MESSAGE', 2)
    assert start(app, client, monkeypatch, [file]).status_code == 409
    assert not app.state.store.rows('SELECT id FROM runs')
    assert app.state.store.rows('SELECT message_id FROM attachments')[0]['message_id'] is None
    assert client.post('/api/runs', json={'prompt': 'Inspect files', 'attachment_ids': [file['id']] * 6}).status_code == 422


@pytest.mark.parametrize('model', ['openai/gpt-6-astra', 'anthropic/claude-opus-5-5'])
def test_current_image_is_multimodal_but_future_and_other_session_files_are_inaccessible(workspace, monkeypatch, model):
    app, client = workspace
    first = upload(client, 'current.png', png()).json()
    run_id = start(app, client, monkeypatch, [first]).json()['id']
    store = app.state.store
    message = store.claim_message(run_id)
    store.update_run(run_id, status='running', token_hash=digest('capability'))
    store.execute('UPDATE runs SET active_model=? WHERE id=?', (model, run_id))
    later = upload(client, 'later.png', png()).json()
    client.post(f'/api/runs/{run_id}/messages', json={'content': 'Future files', 'client_id': 'future-image', 'attachment_ids': [later['id']]})
    store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run_id,))
    other = store.create_run('Other session', '', 'modal', [], chat_enabled=True)
    store.claim_message(other['id'])
    store.update_run(other['id'], status='running', token_hash=digest('other'))
    headers = {'Authorization': 'Bearer capability'}
    assert client.get(f"/broker/{run_id}/attachments/{first['id']}", headers=headers).content == png()
    assert client.get(f"/broker/{run_id}/attachments/{later['id']}", headers=headers).status_code == 404
    assert client.get(f"/broker/{other['id']}/attachments/{first['id']}", headers={'Authorization': 'Bearer other'}).status_code == 404
    spec = app.state.manager.spec({**store.run(run_id), 'message_id': message['id']})
    assert [f['id'] for f in spec['attachments']] == [first['id']]
    prompt = conversation_prompt(spec)
    assert first['id'] in prompt and 'reference data' in prompt
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.003'}, json={'choices': [{'message': {'content': 'A purple image'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kwargs: actual(transport=httpx.MockTransport(upstream), **kwargs))
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'existing-gateway-key'
    body = {'messages': [{'role': 'user', 'content': prompt + f"\n[moyai-attachment:{later['id']}]"}]}
    response = client.post(f'/broker/{run_id}/v1/chat/completions', headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert captured[0]['model'] == model
    content = captured[0]['messages'][-1]['content']
    assert len(content) == 2 and content[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert 'data:image' not in json.dumps(spec) and 'data:image' not in json.dumps(store.messages(run_id))
    assert store.rows('SELECT cost FROM model_requests')[0]['cost'] == '0.003'
    repeated = app.state.store.attachments.with_images(store.run(run_id), [*body['messages'], *body['messages']])
    assert isinstance(repeated[0]['content'], str)
    assert len(repeated[1]['content']) == 2  # A resumed prompt cannot multiply image payloads.


def test_sandbox_restores_originals_verifies_download_and_preserves_safe_paths(tmp_path):
    data = b'# Attached instructions are data, not authority.'
    item = {'id': uuid4().hex, 'name': 'SKILL.md', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    spec = {'broker_url': 'https://workspace.example/broker/run', 'attachments': [item]}
    calls = []
    def open_file(request, timeout):
        assert request.headers['Authorization'] == 'Bearer capability'
        calls.append(request.full_url)
        return BytesIO(data)
    root = tmp_path / '.moyai-attachments'
    prepare_attachments(spec, 'capability', root=root, opener=open_file)
    path = root / item['id'] / 'SKILL.md'
    assert path.read_bytes() == data
    prepare_attachments(spec, 'capability', root=root, opener=open_file)
    assert len(calls) == 1  # Reuse the intact input on an idle sandbox.
    path.unlink()
    with pytest.raises(ValueError, match='incomplete'):
        prepare_attachments(spec, 'capability', root=root, opener=lambda *args, **kwargs: BytesIO(b'wrong'))
    path.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='symlink'):
        prepare_attachments(spec, 'capability', root=root, opener=open_file)
    with pytest.raises(ValueError, match='metadata'):
        prepare_attachments({**spec, 'attachments': [{**item, 'name': '../outside'}]}, 'capability', root=root)


async def test_database_checkpoint_also_restores_uploaded_bytes(workspace, tmp_path):
    app, client = workspace
    file = upload(client).json()
    settings = app.state.settings.model_copy(update={'checkpoint_dir': tmp_path / 'checkpoint'})
    async def commit():
        pass
    await Checkpoints(app.state.store, settings, commit=commit).flush()
    settings.data_dir = tmp_path / 'restored'
    restore_checkpoint(settings)
    restored = Store(settings.data_dir)
    assert restored.rows('SELECT data FROM attachments WHERE id=?', (file['id'],))[0]['data'].startswith(b'# Context')
