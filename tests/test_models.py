import json

import httpx
import pytest

from app.config import MODEL_CATALOG
from app.db import Store
from app.security import digest
from test_workspace import workspace
from test_slack import slack_app, event, signed
from test_slack_chat import start, send

ASTRA = 'openai/gpt-6-astra'
SOL = 'openai/gpt-6.1-sol'
OPUS = 'anthropic/claude-opus-5-5'
GLM = 'fireworks_ai/glm-5p3'


def test_code_catalog_addition_reaches_picker_and_model_validation(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setitem(MODEL_CATALOG, 'example/new-model', 'New model')
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    assert {'id': 'example/new-model', 'name': 'New model', 'default_harness': 'hermes'} in client.get('/api/config').json()['models']
    response = client.post('/api/runs', json={'prompt': 'Use the new model', 'model': 'example/new-model'})
    assert response.status_code == 201
    assert response.json()['model'] == 'example/new-model'


@pytest.mark.parametrize('alias, selected', [
    ('claude/opus-5-5', OPUS), ('glm-5.3', GLM), ('GLM 5.3', GLM), ('Claude Opus 5.5', OPUS), (GLM, GLM),
    ('sol', SOL), ('GPT 6.1 Sol', SOL), ('GPT-6.1 Sol', SOL), (SOL, SOL),
])
def test_model_selection_is_validated_and_frozen_on_each_queued_message(workspace, monkeypatch, alias, selected):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    models = client.get('/api/config').json()['models']
    assert {x['id'] for x in models} >= {ASTRA, SOL, OPUS, GLM}
    assert {'id': GLM, 'name': 'GLM-5.3', 'default_harness': 'hermes'} in models
    assert {'id': SOL, 'name': 'GPT-6.1 Sol', 'default_harness': 'hermes'} in models
    before = len(app.state.store.rows('SELECT id FROM runs'))
    assert client.post('/api/runs', json={'prompt': 'Invalid model', 'model': 'unapproved'}).status_code == 422
    assert len(app.state.store.rows('SELECT id FROM runs')) == before
    run = client.post('/api/runs', json={'prompt': 'First turn', 'model': 'openai/6-astra'}).json()
    assert run['model'] == ASTRA
    first = app.state.store.claim_message(run['id'])
    endpoint = f"/api/runs/{run['id']}/messages"
    followup = {'content': 'Use the same files', 'client_id': 'switch-model-1', 'model': alias}
    response = client.post(endpoint, json=followup)
    assert response.status_code == 202 and response.json()['model'] == selected
    assert app.state.store.run(run['id'])['active_model'] == ASTRA
    assert app.state.store.run(run['id'])['model'] == selected
    assert client.post(endpoint, json=followup).json()['created'] is False
    assert client.post(endpoint, json={**followup, 'model': ASTRA}).status_code == 409
    assert client.post(endpoint, json={**followup, 'client_id': 'invalid-model', 'model': 'unapproved'}).status_code == 422
    # Omitting the model inherits the session preference at admission.
    assert client.post(endpoint, json={'content':'Third turn', 'client_id':'switch-model-2'}).json()['model'] == selected
    app.state.store.finish_message(run['id'], first['id'], 'First response')
    second = app.state.store.claim_message(run['id'])
    assert second['model'] == selected
    assert app.state.store.run(run['id'])['active_model'] == selected
    reopened = Store(app.state.settings.data_dir, default_model=ASTRA)
    assert reopened.run(run['id'])['model'] == selected
    assert reopened.run(run['id'])['active_model'] == selected
    assert next(m for m in reopened.messages(run['id']) if m['role'] == 'assistant')['model'] == ASTRA


def test_sol_can_start_a_session(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/runs', json={'prompt': 'Use GPT-6.1 Sol', 'model': SOL})
    assert response.status_code == 201
    assert response.json()['model'] == SOL
    assert app.state.store.claim_message(response.json()['id'])['model'] == SOL


def test_gateway_pins_active_model_despite_future_switch_or_sandbox_override(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'only-the-server-has-this'
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        assert str(request.url) == 'https://gateway.example/v1/chat/completions'
        return httpx.Response(200, json={'choices':[{'message':{'content':'Done'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kwargs: actual(transport=httpx.MockTransport(upstream), **kwargs))
    for selected, future in [(ASTRA, OPUS), (OPUS, ASTRA), (GLM, ASTRA), (ASTRA, GLM), (SOL, ASTRA), (ASTRA, SOL)]:
        run = app.state.store.create_run('Pinned turn', '', 'modal', [], chat_enabled=True, model=selected)
        app.state.store.claim_message(run['id'])
        app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
        app.state.store.enqueue_message(run['id'], 'Future turn', 'future-turn', future)
        headers = {'Authorization':'Bearer capability'}
        assert client.get(f"/broker/{run['id']}/v1/models", headers=headers).json()['data'][0]['id'] == selected
        response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers=headers,
                               json={'model':future, 'messages':[{'role':'user','content':'Hello'}], 'api_base':'https://untrusted.example'})
        assert response.status_code == 200
        assert captured[-1]['model'] == selected and 'api_base' not in captured[-1]


@pytest.mark.parametrize('alias, selected', [
    ('opus', OPUS), ('glm-5.3', GLM), ('glm 5.3', GLM), ('glm', GLM), ('glm-5p3', GLM),
    ('sol', SOL), ('GPT 6.1 Sol', SOL),
])
def test_slack_model_commands_do_not_run_the_agent_and_keep_queued_models(slack_app, alias, selected):
    app, client, run_id = start(slack_app)
    original = app.state.store.messages(run_id)[0]['model']
    send(client, 1, f'<@U99999999> model {alias}')
    assert len(app.state.store.messages(run_id)) == 1
    assert app.state.store.run(run_id)['model'] == selected
    assert app.state.store.messages(run_id)[0]['model'] == original
    send(client, 2, '<@U99999999> Continue with the selected model')
    assert app.state.store.messages(run_id)[-1]['model'] == selected
    send(client, 3, '<@U99999999> model unapproved')
    assert app.state.store.run(run_id)['model'] == selected
    assert len(app.state.store.messages(run_id)) == 2
    assert 'Choose' in app.state.store.rows("SELECT text FROM slack_outbox WHERE dedupe_key='command:EvChat3'")[0]['text']


def test_slack_can_start_with_a_model_directive_and_task(slack_app):
    app, client, submitted, _ = slack_app
    payload = event(text='<@U99999999> model opus\nRead the thread and summarize it')
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(submitted) == 1
    run = submitted[0]
    assert run['model'] == OPUS
    assert app.state.store.messages(run['id'])[0]['model'] == OPUS
    assert app.state.store.messages(run['id'])[0]['content'] == 'Read the thread and summarize it'


def test_slack_model_only_creates_an_idle_session_without_compute(slack_app):
    app, client, submitted, _ = slack_app
    assert client.post('/hooks/slack/events', **signed(event(text='<@U99999999> model opus'))).status_code == 200
    assert not submitted
    run = app.state.store.rows('SELECT * FROM runs')[0]
    assert run['status'] == 'idle' and run['model'] == OPUS
    assert not app.state.store.messages(run['id'])
    send(client, 1, '<@U99999999> Start the task now')
    assert submitted[0]['id'] == run['id']
    assert app.state.store.messages(run['id'])[0]['model'] == OPUS
