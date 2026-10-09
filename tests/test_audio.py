import asyncio
from io import BytesIO
from uuid import uuid4
import wave
import httpx
import pytest
from app.attachments import attachment_context
from app.db import Store
from app.message_queue import MessageQueue
from app.slack_files import file_ids
from test_attachments import upload, start, storage_mode, off_event_loop
from storage_fixture import MemoryObjects
from test_slack import slack_app, signed, event
from test_spend import sign_in
from test_workspace import workspace


def wav():
    out = BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', ''))
        audio.writeframes(b'\0\0' * 1600)
    return out.getvalue()


def gateway(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.audio.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(handler), **kw))


def slack_download(monkeypatch, handler):
    """Mock only the SDK's HTTP transport, preserving its download validation."""
    class Stream:
        def __init__(self, response):
            self.response, self.status, self.content = response, response.status_code, self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            for chunk in self.response.iter_bytes(size):
                yield chunk

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, *, headers, allow_redirects):
            assert allow_redirects is False
            return Stream(handler(httpx.Request('GET', url, headers=headers)))

    monkeypatch.setattr('agentchat.channels.slack_files.aiohttp.ClientSession', Session)


def configured(app):
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'private-gateway-key'


def test_web_audio_transcribes_once_preserves_original_and_reaches_agent(workspace, monkeypatch, storage_mode):
    app, client = workspace
    configured(app)
    calls = []
    def response(request):
        calls.append(request)
        assert str(request.url) == 'https://gateway.example/v1/audio/transcriptions'
        assert request.headers['Authorization'] == 'Bearer private-gateway-key'
        assert b'gpt-transcribe' in request.content and wav() in request.content
        return httpx.Response(200, json={'text': 'Please investigate the failing health check.'})
    gateway(monkeypatch, response)
    file_id = uuid4().hex
    file = upload(client, 'voice.wav', wav(), file_id).json()
    assert file['media_type'] == 'audio/wav'
    assert file['transcript'] == 'Please investigate the failing health check.'
    assert client.get(file['audio_url']).content == wav()
    assert client.get(file['audio_url']).headers['content-type'] == 'audio/wav'
    partial = client.get(file['audio_url'], headers={'Range': 'bytes=10-29'})
    assert partial.status_code == 206 and partial.content == wav()[10:30]
    assert partial.headers['Content-Range'] == f'bytes 10-29/{len(wav())}'
    assert upload(client, 'voice.wav', wav(), file_id).json() == file
    assert len(calls) == 1
    run_id = start(app, client, monkeypatch, [file]).json()['id']
    turn = app.state.store.claim_message(run_id)
    spec = app.state.manager.spec({**app.state.store.run(run_id), 'message_id': turn['id']})
    assert file['transcript'] in spec['attachment_context']
    assert 'recognition errors' in spec['attachment_context']
    assert Store(app.state.settings.data_dir).messages(run_id)[0]['attachments'][0]['transcript'] == file['transcript']


def test_audio_playback_obeys_draft_ownership(workspace, monkeypatch, storage_mode):
    app, client = workspace
    configured(app)
    gateway(monkeypatch, lambda r: httpx.Response(200, json={'text': 'Private draft'}))
    sign_in(app, client)
    file = upload(client, 'private.wav', wav()).json()
    sign_in(app, client, 'other', 'other@berri.ai')
    assert client.get(file['audio_url']).status_code == 404
    client.cookies.clear()
    assert client.get(file['audio_url']).status_code == 401


@pytest.mark.parametrize('reply', [{'text': ''}, {'text': 'x' * 16001}, {'text': None}, []])
def test_empty_or_oversized_transcripts_cannot_be_sent(workspace, monkeypatch, reply):
    app, client = workspace
    configured(app)
    gateway(monkeypatch, lambda r: httpx.Response(200, json=reply))
    assert upload(client, 'voice.wav', wav()).status_code == 422
    assert not app.state.store.rows('SELECT id FROM attachments')


def test_unconfigured_invalid_and_provider_failure_are_actionable(workspace, monkeypatch):
    app, client = workspace
    assert 'not configured' in upload(client, 'voice.wav', wav()).text
    assert 'could not be read' in upload(client, 'fake.mp3', b'<html>not audio</html>').text
    configured(app)
    gateway(monkeypatch, lambda r: httpx.Response(403, text='private-gateway-key'))
    response = upload(client, 'voice.wav', wav())
    assert response.status_code == 422 and 'Retry' in response.text
    assert 'private-gateway-key' not in response.text


@pytest.mark.parametrize('remote', [False, True])
def test_slack_audio_only_is_durable_deduplicated_and_off_ack_path(slack_app, monkeypatch, remote):
    app, client, runs, _ = slack_app
    if remote:
        app.state.store.objects = MemoryObjects()
        monkeypatch.setattr(app.state.store.objects, 'put', off_event_loop(app.state.store.objects.put))
    payload = event(text='<@U99999999>', files=[{'id':'F12345678','name':'voice.wav','mimetype':'audio/wav'}])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(runs) == 1
    run_id = runs[0]['id']
    client.post('/hooks/slack/events', **signed(payload))
    payload['event_id'] = 'EvPairedAudio'
    payload['event']['type'] = 'message'
    client.post('/hooks/slack/events', **signed(payload))
    assert len(app.state.store.messages(run_id)) == 1
    assert len(app.state.store.rows('SELECT * FROM slack_audio_inputs')) == 1
    assert not app.state.store.rows('SELECT id FROM attachments')
    assert Store(app.state.settings.data_dir).rows('SELECT status FROM slack_audio_inputs')[0]['status'] == 'pending'
    read = []
    async def recording(file_id, team):
        read.append(file_id)
        assert team == 'T12345678'
        return 'voice.wav', wav(), ('audio/wav', b'', 'Investigate the deployment logs.')
    monkeypatch.setattr(app.state.slack.files, 'read', recording)
    message = app.state.store.claim_message(run_id)
    asyncio.run(app.state.slack.files.prepare(run_id))
    asyncio.run(app.state.slack.files.prepare(run_id))
    assert read == ['F12345678']
    stored = app.state.store.messages(run_id)[0]
    assert stored['user_id'] == message['user_id']
    assert stored['attachments'][0]['transcript'] == 'Investigate the deployment logs.'
    assert app.state.store.attachments.broker_file(app.state.store.run(run_id), stored['attachments'][0]['id']).body == wav()
    assert 'Investigate' in attachment_context(app.state.store.attachments.for_run(run_id, message['id']))


def test_slack_followup_audio_requires_handoff_without_leaking(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    client.post('/hooks/slack/events', **signed(event()))
    run_id = runs[0]['id']
    first = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    payload = event('EvAudioFollowup', type='message', subtype='file_share', text='<@U99999999>', ts='1790719001.123456',
                    thread_ts='1790719000.123456', files=[{'id':'F12345678','name':'voice.m4a','mimetype':'audio/mp4'}])
    client.post('/hooks/slack/events', **signed(payload))
    second = app.state.store.messages(run_id)[1]
    assert MessageQueue(app.state.store).live_control(run_id, first['id'], []) == {'steer_message_id':second['id'], 'handoff':True}
    asyncio.run(app.state.slack.files.prepare(run_id))
    assert not app.state.store.rows('SELECT id FROM attachments')
    app.state.store.finish_message(run_id, first['id'], '', 'steered')
    app.state.store.claim_message(run_id)
    async def unavailable(*args):
        raise ValueError('Reconnect Slack with files:read permission, then resend the audio.')
    monkeypatch.setattr(app.state.slack.files, 'read', unavailable)
    asyncio.run(app.state.slack.files.prepare(run_id))
    text = app.state.store.rows('SELECT content FROM messages WHERE id=?', (second['id'],))[0]['content']
    assert 'files:read' in text and 'Do not guess' in text


def test_slack_audio_respects_existing_authorization(slack_app):
    app, client, runs, _ = slack_app
    payload = event(text='', type='message', files=[{'id':'F12345678','mimetype':'audio/wav'}])
    client.post('/hooks/slack/events', **signed(payload))
    assert not runs
    payload['event'].update(channel='D12345678', channel_type='im')
    app.state.settings.slack_dm_enabled = False
    client.post('/hooks/slack/events', **signed(payload))
    assert not runs
    app.state.settings.slack_dm_enabled = True
    client.post('/hooks/slack/events', **signed(payload))
    assert len(runs) == 1


def grant_files(app):
    app.state.connectors.save('slack', {'access_token':'user-token','kind':'oauth',
        'bot':{'access_token':'bot-token','team':{'id':'T12345678'},'bot_user_id':'U99999999','scope':'files:read'}}, 'Test')


@pytest.mark.parametrize('url', ['https://evil.example/audio', 'http://files.slack.com/files-pri/a',
    'https://files.slack.com.evil.example/files-pri/a', 'https://token@files.slack.com/files-pri/a'])
def test_slack_never_sends_bot_token_to_untrusted_file_urls(slack_app, monkeypatch, url):
    app, *_ = slack_app
    grant_files(app)
    async def info(*args, **kwargs):
        return {'ok':True, 'file':{'id':'F12345678','name':'voice.wav','size':len(wav()),'url_private':url}}
    monkeypatch.setattr(app.state.connectors, 'request', info)
    with pytest.raises(ValueError, match='unsupported file location'):
        asyncio.run(app.state.slack.files.read('F12345678','T12345678'))


def test_slack_download_and_transcription_use_separate_credentials(slack_app, monkeypatch):
    app, *_ = slack_app
    grant_files(app)
    async def info(*args, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer bot-token'
        return {'ok':True, 'file':{'id':'F12345678','name':'voice.wav','size':len(wav()),'url_private':'https://files.slack.com/files-pri/voice'}}
    def response(request):
        if request.method == 'GET':
            assert request.headers['Authorization'] == 'Bearer bot-token'
            return httpx.Response(200, content=wav())
        assert request.headers['Authorization'] == 'Bearer model-test-key'
        return httpx.Response(200, json={'text':'Read this voice message.'})
    monkeypatch.setattr(app.state.connectors, 'request', info)
    gateway(monkeypatch, response)
    slack_download(monkeypatch, response)
    name, raw, inspected = asyncio.run(app.state.slack.files.read('F12345678','T12345678'))
    assert inspected[2] == 'Read this voice message.' and raw == wav()


def test_slack_file_candidates_include_images_and_ignore_untrusted_urls():
    assert file_ids([{'id':'F12345678','name':'voice.m4a','url_private':'https://evil.example'}]) == ['F12345678']
    assert file_ids([{'id':'F12345678','name':'image.png','mimetype':'image/png'}]) == ['F12345678']
    assert file_ids([{'id':'not-a-slack-id','mimetype':'audio/mp3'}]) == []


def test_audio_only_api_inputs_are_valid_but_empty_messages_are_not(workspace, monkeypatch):
    app, client = workspace
    configured(app)
    gateway(monkeypatch, lambda r: httpx.Response(200, json={'text':'Use this recording.'}))
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    file = upload(client, 'voice.wav', wav()).json()
    result = client.post('/api/runs', json={'attachment_ids':[file['id']]})
    assert result.status_code == 201
    second = upload(client, 'reply.wav', wav()).json()
    endpoint = '/api/runs/' + result.json()['id'] + '/messages'
    assert client.post(endpoint, json={'attachment_ids':[second['id']], 'client_id':'audio-only-reply'}).status_code == 202
    assert client.post('/api/runs', json={}).status_code == 422
    assert client.post(endpoint, json={'content':'', 'client_id':'empty-message'}).status_code == 422


def test_legacy_slack_intake_keeps_audio(slack_app):
    app, client, runs, _ = slack_app
    app.state.settings.slack_thread_chat_enabled = False
    payload = event(text='<@U99999999>',files=[{'id':'F12345678','name':'voice.wav','mimetype':'audio/wav'}])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(runs) == 1
    assert len(app.state.store.rows('SELECT * FROM slack_audio_inputs')) == 1


def test_audio_range_requests_allow_seeking_without_bypassing_permissions(workspace, monkeypatch):
    app, client = workspace
    configured(app)
    gateway(monkeypatch, lambda r: httpx.Response(200, json={'text':'Seek this recording.'}))
    file = upload(client, 'voice.wav', wav()).json()
    for header, expected in [('bytes=0-1',wav()[:2]),('bytes=100-',wav()[100:]),('bytes=-10',wav()[-10:])]:
        response = client.get(file['audio_url'], headers={'Range':header})
        assert response.status_code == 206 and response.content == expected
        assert response.headers['content-range'].endswith('/' + str(len(wav())))
    for header in ['bytes=999999-', 'bytes=2-1', 'bytes=-0', 'bytes=-', 'bytes=0-1,2-3']:
        assert client.get(file['audio_url'], headers={'Range':header}).status_code == 416
    client.cookies.clear()
    assert client.get(file['audio_url'],headers={'Range':'bytes=0-1'}).status_code == 401


def test_transcription_glossary_and_concurrent_retries_share_one_upload(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app, client = workspace
    configured(app)
    app.state.settings.audio_transcription_prompt = 'Moyai, LiteLLM, OAuth'
    calls = []
    async def response(request):
        calls.append(1)
        assert b'Moyai, LiteLLM, OAuth' in request.content
        await asyncio.sleep(.1)
        return httpx.Response(200,json={'text':'Inspect the OAuth callback.'})
    gateway(monkeypatch,response)
    file_id = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: upload(client,'voice.wav',wav(),file_id),range(2)))
    assert [reply.status_code for reply in replies] == [200,200]
    assert len(calls) == 1
    assert len(app.state.store.rows('SELECT id FROM attachments')) == 1
