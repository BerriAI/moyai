import asyncio
import base64
import json

import httpx
import pytest

from app.attachments import MAX_FILE, attachment_context, inspect_file
from app.db import Store
from app.message_queue import MessageQueue
from app.security import digest
from sandbox.agent import conversation_prompt
from test_attachments import png
from test_audio import gateway, grant_files, slack_download
from test_slack import event, signed, slack_app
from test_spend import sign_in

FILE = {'id': 'F12345678', 'name': 'image.png', 'mimetype': 'image/png'}
ROOT = '1790719000.123456'


def provider(app, monkeypatch, *, raw=None, metadata=None, history=None, status=200):
    grant_files(app)
    raw = png() if raw is None else raw
    calls, model_inputs = [], []

    async def request(method, url, **kwargs):
        if url.endswith('files.info'):
            assert kwargs['headers']['Authorization'] == 'Bearer bot-token'
            file_id = kwargs['params']['file']
            calls.append(file_id)
            return {'ok': True, 'file': {**FILE, 'id': file_id, 'size': len(raw),
                'url_private_download': 'https://files.slack.com/files-pri/' + file_id, **(metadata or {})}}
        if 'conversations.' in url:
            return {'messages': history or [{'ts': ROOT, 'user': 'U12345678', 'text': 'Inspect the image', 'files': [FILE]}]}
        return {'ok': True, 'ts': ROOT}

    def transport(request):
        if request.url.host == 'files.slack.com':
            assert request.headers['Authorization'] == 'Bearer bot-token'
            return httpx.Response(status, content=raw, headers={'location': 'https://evil.example/stolen'})
        assert request.url.host == 'gateway.example'
        assert request.headers['Authorization'] == 'Bearer model-test-key'
        if request.url.path == '/model/info':
            return httpx.Response(200, json={'data': [{'model_name': name, 'model_info': {
                'max_input_tokens': 128000, 'max_output_tokens': 16000}}
                for name in ['openai/gpt-6-astra', 'anthropic/claude-opus-5-5']]})
        if request.url.path == '/utils/token_counter':
            content = json.loads(request.content)['messages'][0]['content']
            assert content[-1]['type'] == 'image_url'
            assert base64.b64decode(content[-1]['image_url']['url'].split(',', 1)[1]) == inspect_file(png())[1]
            return httpx.Response(200, json={'total_tokens': 1000, 'tokenizer_type': 'openai_api'})
        model_inputs.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Image received.'}}]})

    monkeypatch.setattr(app.state.connectors, 'request', request)
    gateway(monkeypatch, transport)
    slack_download(monkeypatch, transport)
    return calls, model_inputs


@pytest.mark.parametrize('model', ['openai/gpt-6-astra', 'anthropic/claude-opus-5-5'])
def test_signed_slack_image_reaches_web_preview_and_model_via_web_attachment_pipeline(slack_app, monkeypatch, model):
    app, client, runs, _ = slack_app
    calls, model_inputs = provider(app, monkeypatch)
    payload = event(text='<@U99999999> help with this image', files=[{**FILE, 'url_private': 'https://evil.example/ignored'}])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert not calls  # Acknowledgment never downloads files.
    run_id = runs[0]['id']
    client.post('/hooks/slack/events', **signed(payload))
    payload['event_id'] = 'PairedEvent'
    payload['event']['type'] = 'message'
    client.post('/hooks/slack/events', **signed(payload))
    store = app.state.store
    assert len(store.messages(run_id)) == 1
    message = store.claim_message(run_id)
    asyncio.run(app.state.slack.prepare(run_id))
    asyncio.run(app.state.slack.prepare(run_id))
    assert calls == [FILE['id']]
    attachment = store.messages(run_id)[0]['attachments'][0]
    assert attachment['name'] == 'image.png' and attachment['media_type'] == 'image/png'
    sign_in(app, client)
    assert client.get(attachment['url']).content == png()
    assert client.get(attachment['preview_url']).content == inspect_file(png())[1]
    assert Store(app.state.settings.data_dir).messages(run_id)[0]['attachments'] == [attachment]
    store.update_run(run_id, status='running', token_hash=digest('capability'))
    store.execute('UPDATE runs SET active_model=? WHERE id=?', (model, run_id))
    spec = app.state.manager.spec({**store.run(run_id), 'message_id': message['id']})
    prompt = conversation_prompt(spec)
    assert attachment['id'] in prompt and 'reference data' in prompt
    assert 'data:image' not in json.dumps(spec)
    response = client.post(f'/broker/{run_id}/v1/chat/completions',
        headers={'Authorization': 'Bearer capability'}, json={'messages': [{'role': 'user', 'content': prompt}]})
    assert response.status_code == 200, response.text
    parts = model_inputs[0]['messages'][-1]['content']
    assert model_inputs[0]['model'] == model
    assert len(parts) == 2 and parts[1]['type'] == 'image_url'
    assert base64.b64decode(parts[1]['image_url']['url'].split(',', 1)[1]) == inspect_file(png())[1]


def test_image_only_slack_dm_is_accepted(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    calls, _ = provider(app, monkeypatch)
    client.post('/hooks/slack/events', **signed(event(type='message', subtype='file_share',
        channel='D12345678', channel_type='im', text='', files=[FILE])))
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.prepare(run_id))
    assert calls == [FILE['id']]
    assert app.state.store.messages(run_id)[0]['attachments'][0]['preview_url']


def test_slack_image_followup_waits_for_its_own_turn(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    calls, _ = provider(app, monkeypatch)
    client.post('/hooks/slack/events', **signed(event()))
    run_id = runs[0]['id']
    store = app.state.store
    first = store.claim_message(run_id)
    store.update_run(run_id, status='running')
    client.post('/hooks/slack/events', **signed(event('ImageFollowup', type='message', subtype='file_share',
        ts='1790719001.123456', thread_ts=ROOT, text='', files=[FILE])))
    second = store.messages(run_id)[1]
    assert MessageQueue(store).live_control(run_id, first['id'], []) == {'steer_message_id': second['id'], 'handoff': True}
    asyncio.run(app.state.slack.files.prepare(run_id))
    assert not calls and not store.attachments.for_run(run_id, first['id'])
    store.finish_message(run_id, first['id'], '', 'steered')
    store.claim_message(run_id)
    asyncio.run(app.state.slack.files.prepare(run_id))
    uploads = store.attachments.for_run(run_id, second['id'])
    assert len(uploads) == 1 and uploads[0]['message_id'] == second['id']
    assert uploads[0]['id'] in attachment_context(uploads)


@pytest.mark.parametrize('threaded', [False, True])
def test_context_recovers_mention_files_and_only_imports_prior_files_from_invoked_thread(slack_app, monkeypatch, threaded):
    app, client, runs, _ = slack_app
    earlier = '1790718000.123456'
    other_file = {**FILE, 'id': 'F87654321', 'url_private': 'https://evil.example/ignored'}
    history = [{'ts': earlier, 'text': 'Original screenshot', 'user': 'U87654321', 'files': [other_file]},
               {'ts': ROOT, 'thread_ts': earlier if threaded else ROOT,
                'text': 'Inspect this', 'user': 'U12345678', 'files': [FILE]},
               {'ts': '1790719999.123456', 'text': 'Future file', 'user': 'U12345678', 'files': [{**FILE, 'id': 'F99999999'}]}]
    calls, _ = provider(app, monkeypatch, history=history)
    # app_mention can omit files; recover them from the bounded Slack read.
    client.post('/hooks/slack/events', **signed(event(**({'thread_ts': earlier} if threaded else {}))))
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.prepare(run_id))
    assert calls == [FILE['id'], other_file['id']] if threaded else calls == [FILE['id']]
    source = app.state.store.slack_source(run_id)
    assert 'url_private' not in json.dumps(source)
    assert len(app.state.store.messages(run_id)[0]['attachments']) == (2 if threaded else 1)


@pytest.mark.parametrize('metadata,raw,status,error', [
    ({'size': MAX_FILE + 1}, None, 200, '10 MB'),
    ({'is_external': True}, None, 200, 'unavailable'),
    ({'url_private_download': 'https://evil.example/image.png'}, None, 200, 'unsupported file location'),
    ({}, b'not an image', 200, 'could not be opened'),
    ({}, None, 302, 'Could not read'),
    ({}, None, 403, 'Could not read'),
])
def test_failed_slack_images_are_explicit_and_never_become_model_images(slack_app, monkeypatch, metadata, raw, status, error):
    app, client, runs, _ = slack_app
    provider(app, monkeypatch, metadata=metadata, raw=raw, status=status)
    client.post('/hooks/slack/events', **signed(event(files=[FILE])))
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.files.prepare(run_id))
    message = app.state.store.messages(run_id)[0]
    assert not message['attachments']
    assert error in message['content'] and 'Do not guess the missing content' in message['content']
    assert 'bot-token' not in message['content'] and 'evil.example' not in message['content']


def test_slack_image_actual_download_limit_and_connection_changes(slack_app, monkeypatch):
    app, *_ = slack_app
    provider(app, monkeypatch, raw=b'x' * 32, metadata={'size': 1})
    monkeypatch.setattr('app.slack_files.MAX_FILE', 16)
    with pytest.raises(ValueError, match='download limit'):
        asyncio.run(app.state.slack.files.read(FILE['id'], 'T12345678'))
    with pytest.raises(ValueError, match='access changed'):
        asyncio.run(app.state.slack.files.read(FILE['id'], 'T87654321'))


@pytest.mark.parametrize('scopes', ['', 'reactions:write'])
def test_slack_images_check_live_access_when_saved_scopes_are_stale(slack_app, monkeypatch, scopes):
    app, client, runs, _ = slack_app
    calls, _ = provider(app, monkeypatch)
    app.state.connectors.save('slack', {'access_token': 'user-token', 'kind': 'oauth',
        'bot': {'access_token': 'bot-token', 'team': {'id': 'T12345678'},
                'bot_user_id': 'U99999999', 'scope': scopes}}, 'Test')
    client.post('/hooks/slack/events', **signed(event(type='message', text='', files=[FILE])))
    assert not runs  # Unaddressed channel uploads do not start sessions.
    client.post('/hooks/slack/events', **signed(event(files=[FILE])))
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.files.prepare(run_id))
    message = app.state.store.messages(run_id)[0]
    assert calls == [FILE['id']]
    assert message['attachments'][0]['preview_url']
    assert 'could not be read' not in message['content']


@pytest.mark.parametrize('saved_scope', ['', 'files:read'])
@pytest.mark.parametrize('error,expected', [
    ('missing_scope', 'Reconnect Slack with files:read permission'),
    ('file_not_found', 'Could not read this Slack attachment'),
    ('invalid_auth', 'Could not read this Slack attachment'),
])
def test_slack_file_access_errors_come_from_live_api(slack_app, monkeypatch, saved_scope, error, expected):
    app, client, runs, _ = slack_app
    app.state.connectors.save('slack', {'access_token': 'user-token', 'kind': 'oauth',
        'bot': {'access_token': 'bot-token', 'team': {'id': 'T12345678'},
                'bot_user_id': 'U99999999', 'scope': saved_scope}}, 'Test')
    client.post('/hooks/slack/events', **signed(event(files=[FILE])))
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    calls = []

    def transport(request):
        assert str(request.url).startswith('https://slack.com/api/files.info?')
        assert request.headers['Authorization'] == 'Bearer bot-token'
        calls.append(request.url.params['file'])
        return httpx.Response(200, json={'ok': False, 'error': error})

    # Exercise the real connector's error handling, not only the SDK adapter.
    from app.connectors import Connectors
    monkeypatch.setattr(app.state.connectors, 'request',
                        Connectors.request.__get__(app.state.connectors))
    gateway(monkeypatch, transport)
    asyncio.run(app.state.slack.files.prepare(run_id))
    message = app.state.store.messages(run_id)[0]
    assert calls == [FILE['id']]
    assert not message['attachments'] and expected in message['content']
    assert 'Do not guess the missing content' in message['content']
    assert 'bot-token' not in message['content']
    if error != 'missing_scope':
        assert 'files:read' not in message['content']


@pytest.mark.parametrize('change', ['paused', 'team_changed'])
@pytest.mark.parametrize('during_read', [False, True])
def test_live_file_scope_check_preserves_connection_guards(slack_app, monkeypatch, change, during_read):
    app, *_ = slack_app
    calls, _ = provider(app, monkeypatch)
    original = app.state.connectors.request

    def revoke():
        if change == 'paused':
            app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0) "
                                    "ON CONFLICT(provider) DO UPDATE SET enabled=0")
        else:
            app.state.connectors.save('slack', {'kind': 'oauth', 'bot': {
                'access_token': 'replacement-token', 'team': {'id': 'T87654321'},
                'bot_user_id': 'U99999999', 'scope': 'files:read'}}, 'Other team')

    async def request(*args, **kwargs):
        result = await original(*args, **kwargs)
        revoke()
        return result

    if during_read:
        monkeypatch.setattr(app.state.connectors, 'request', request)
    else:
        revoke()
    with pytest.raises(ValueError, match='Slack access changed'):
        asyncio.run(app.state.slack.files.read(FILE['id'], 'T12345678'))
    assert calls == ([FILE['id']] if during_read else [])


@pytest.mark.parametrize('from_context', [False, True])
def test_slack_eight_file_budget_keeps_mention_priority_and_deduplicates(slack_app, monkeypatch, from_context):
    app, client, runs, _ = slack_app
    files = [{**FILE, 'id': f'F{i:08d}'} for i in range(1, 10)]
    earlier = '1790718000.123456'
    history = [
        {'ts': earlier, 'text': 'Earlier files', 'user': 'U12345678', 'files': files},
        {'ts': ROOT, 'thread_ts': earlier, 'text': 'Inspect this', 'user': 'U12345678', 'files': [files[-1]]},
    ]
    calls, _ = provider(app, monkeypatch, history=history)
    payload = event(thread_ts=earlier, files=[] if from_context else [files[-1], *files])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    run_id = runs[0]['id']
    app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.prepare(run_id))
    assert calls == [files[-1]['id'], *[file['id'] for file in files[:7]]]
    assert len(app.state.store.messages(run_id)[0]['attachments']) == 8
