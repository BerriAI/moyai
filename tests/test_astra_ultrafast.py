"""Opt-in persistence and the actual, server-pinned upstream processing tier."""
import json

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.model_preferences import preferred_model
from app.model_selection import ASTRA, ASTRA_ULTRAFAST
from app.security import digest
from test_context_budget import limits, service
from test_model_tools import call
from test_spend import sign_in
from test_workspace import workspace


@pytest.mark.parametrize('alias', ['astra-ultrafast', '6-astra ultrafast', 'GPT-6 Astra Ultrafast', ASTRA_ULTRAFAST])
def test_ultrafast_is_an_explicit_choice_with_a_native_runtime(alias):
    settings = Settings(_env_file=None, agent_model='', agent_harness='hermes')
    assert settings.resolve_model() == ASTRA
    assert settings.resolve_model('astra') == ASTRA
    assert settings.resolve_model(alias) == ASTRA_ULTRAFAST
    assert settings.default_harness(alias) == 'codex'


def test_opt_in_survives_new_login_and_restart_and_can_be_reversed(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    alice = sign_in(app, client)
    assert client.get('/api/config').json()['model'] == ASTRA
    previous = client.post('/api/runs', json={'prompt': 'Existing normal chat'}).json()
    route = '/api/settings/model-preference'
    assert client.put(route, json={'model': ASTRA_ULTRAFAST}).status_code == 200
    config = client.get('/api/config').json()
    assert config['model'] == ASTRA_ULTRAFAST
    assert next(model for model in config['models'] if model['id'] == ASTRA_ULTRAFAST)['default_harness'] == 'codex'
    run = client.post('/api/runs', json={'prompt': 'New opted-in chat'}).json()
    assert (run['model'], run['harness']) == (ASTRA_ULTRAFAST, 'codex')
    assert app.state.store.run(previous['id'])['model'] == ASTRA
    assert app.state.manager.spec(app.state.store.run(run['id']))['model'] == ASTRA

    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/config').json()['model'] == ASTRA
    sign_in(app, client)
    assert client.get('/api/config').json()['model'] == ASTRA_ULTRAFAST
    reopened = Store(app.state.settings.data_dir, default_model=ASTRA)
    with reopened.connect() as conn:
        assert preferred_model(conn, app.state.settings, alice) == ASTRA_ULTRAFAST
    assert client.put(route, json={'model': ASTRA}).status_code == 200
    sign_in(app, client)
    assert client.get('/api/config').json()['model'] == ASTRA
    assert client.post('/api/runs', json={'prompt': 'Normal again'}).json()['model'] == ASTRA
    assert app.state.store.run(run['id'])['model'] == ASTRA_ULTRAFAST


@pytest.mark.parametrize('selected,tier', [(ASTRA, 'default'), (ASTRA_ULTRAFAST, 'ultrafast')])
@pytest.mark.parametrize('stream', [False, True])
def test_gateway_freezes_tier_per_turn_and_ignores_sandbox_overrides(workspace, monkeypatch, selected, tier, stream):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    seen = []
    result = {'id': 'tier-receipt', 'status': 'completed', 'service_tier': tier,
              'usage': {'input_tokens': 3, 'output_tokens': 2}}
    wire = (f'event: response.completed\ndata: {json.dumps({"type": "response.completed", "response": result})}\n\n'
            if stream else json.dumps(result)).encode()

    def upstream(request):
        assert request.url.path == '/v1/responses'
        seen.append(json.loads(request.content))
        assert seen[-1]['model'] == ASTRA
        assert seen[-1]['service_tier'] == tier
        return httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream' if stream else 'application/json'})

    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('First turn', '', 'modal', [], model=selected, harness='codex', chat_enabled=True)
    first = app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    future = ASTRA if selected == ASTRA_ULTRAFAST else ASTRA_ULTRAFAST
    app.state.store.enqueue_message(run['id'], 'Next turn in the other mode', 'future-tier', future)
    headers = {'Authorization': 'Bearer capability'}
    assert client.get(f"/broker/{run['id']}/v1/models", headers=headers).json()['data'][0]['id'] == ASTRA
    response = client.post(f"/broker/{run['id']}/v1/responses", headers=headers,
        json={'model': future, 'service_tier': 'default' if tier == 'ultrafast' else 'ultrafast', 'input': 'Hello', 'stream': stream})
    assert response.status_code == 200, response.text
    assert response.content == wire
    assert len(seen) == 1
    billed = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert billed['model'] == selected and billed['status'] == 'completed'
    app.state.store.finish_message(run['id'], first['id'], 'Done')
    assert app.state.store.claim_message(run['id'])['model'] == future
    assert app.state.store.run(run['id'])['active_model'] == future


def test_incompatible_harness_and_protocol_cannot_silently_disable_opt_in(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/runs', json={'prompt': 'Task', 'model': ASTRA_ULTRAFAST, 'harness': 'hermes'})
    assert response.status_code == 422 and 'requires Codex' in response.text
    run = client.post('/api/runs', json={'prompt': 'Existing Hermes task', 'harness': 'hermes', 'chat_enabled': True}).json()
    app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    run = app.state.store.run(run['id'])
    assert client.post(f"/api/runs/{run['id']}/messages", json={
        'content': 'Switch', 'client_id': 'incompatible-tier', 'model': ASTRA_ULTRAFAST}).status_code == 422
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run['id'],))
    assert call(client, app.state.store.run(run['id']), model=ASTRA_ULTRAFAST).status_code == 422
    run = app.state.store.create_run('Codex task', '', 'modal', [], model=ASTRA_ULTRAFAST, harness='codex')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    for route in ('messages', 'chat/completions'):
        response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer capability'},
                               json={'messages': [{'role': 'user', 'content': 'Hello'}]})
        assert response.status_code == 422 and 'Responses API' in response.text


@pytest.mark.asyncio
async def test_ultrafast_uses_astra_metadata_and_token_counter(monkeypatch):
    seen = []
    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == '/model/info':
            return httpx.Response(200, json={'data': [{'model_name': ASTRA, 'model_info': limits().model_dump()}]})
        assert json.loads(request.content)['model'] == ASTRA
        return httpx.Response(200, json={'total_tokens': 42, 'tokenizer_type': 'openai_api'})
    budget = service(monkeypatch, upstream)
    assert await budget.limits(ASTRA_ULTRAFAST) == await budget.limits(ASTRA)
    assert (await budget.count({'model': ASTRA_ULTRAFAST, 'input': 'Hello'}))[0] == 42
    assert seen == ['/model/info', '/utils/token_counter']
    budget.settings.model_context_limits[ASTRA] = limits(32000)
    assert (await budget.limits(ASTRA_ULTRAFAST)).context_window == 32000
