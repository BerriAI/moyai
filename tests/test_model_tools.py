"""Exercise conversational switches through the same broker used by Hermes."""
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException

from app.db import Store
from app.model_tools import TOOL_NAMES
from app.security import digest
from test_models import ASTRA, DEEPSEEK, GLM, OPUS
from test_slack import slack_app, event, signed
from test_spend import active
from test_workspace import workspace


def call(client, run, name='model_switch', **arguments):
    if name == 'model_switch':
        arguments = {'turn_id': run['active_message_id'], 'request_key': 'switch-to-glm', 'model': 'GLM 5.3', **arguments}
    return client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
                       json={'name': name, 'arguments': arguments})


def gateway(app, monkeypatch):
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'test-only'
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Done'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kwargs: actual(transport=httpx.MockTransport(upstream), **kwargs))
    return captured


@pytest.mark.parametrize('user', ['google:alice', 'slack:T12345678:U12345678'])
@pytest.mark.parametrize('selected, alias', [(GLM, 'GLM 5.3'), (DEEPSEEK, 'DeepSeek V4.1 Flash')])
def test_tools_switch_next_inference_preserving_history_files_and_admission_receipts(workspace, monkeypatch, user, selected, alias):
    app, client = workspace
    run = active(app, user)
    captured = gateway(app, monkeypatch)
    headers = {'Authorization': 'Bearer capability'}
    listing = client.get(f"/broker/{run['id']}/tools", headers=headers).json()
    assert TOOL_NAMES <= {tool['name'] for tool in listing}
    assert call(client, run, 'model_list').json()['active_model'] == ASTRA
    app.state.store.update_run(run['id'], snapshot_id='existing-workspace-snapshot')
    history = [{'role': 'user', 'content': f'Remember copper lighthouse. Use {alias} and summarize this.'}]
    endpoint = f"/broker/{run['id']}/v1/chat/completions"
    assert client.post(endpoint, headers=headers, json={'model': GLM, 'messages': history}).status_code == 200
    assert captured[-1]['model'] == ASTRA  # Raw sandbox overrides still cannot switch.
    response = call(client, run, model=alias)
    assert response.status_code == 200, response.text
    assert response.json()['active_model'] == response.json()['default_model'] == selected
    assert response.json()['effective'] == 'next_model_request'
    history += [{'role': 'assistant', 'content': 'The model switch succeeded.'}, {'role': 'user', 'content': 'Continue the same task.'}]
    assert client.post(endpoint, headers=headers, json={'model': ASTRA, 'messages': history}).status_code == 200
    assert captured[-1]['model'] == selected and captured[-1]['messages'][-len(history):] == history
    assert app.state.store.run(run['id'])['snapshot_id'] == 'existing-workspace-snapshot'
    assert len(app.state.store.messages(run['id'])) == 1  # No fabricated user turn or replay.
    app.state.store.finish_message(run['id'], run['active_message_id'], f'Done using {alias}')
    messages = app.state.store.messages(run['id'])
    assert messages[0]['model'] == ASTRA and messages[1]['model'] == selected
    reopened = Store(app.state.settings.data_dir, default_model=ASTRA)
    assert reopened.run(run['id'])['active_model'] == selected
    assert reopened.run(run['id'])['model'] == selected


def test_natural_slack_request_reaches_tools_with_task_intact(slack_app):
    app, client, submitted, _ = slack_app
    prompt = 'Use GLM 5.3 and summarize the release notes.'
    assert client.post('/hooks/slack/events', **signed(event(text='<@U99999999> ' + prompt))).status_code == 200
    run = submitted[0]
    message = app.state.store.claim_message(run['id'])
    assert message['content'] == prompt
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    run = app.state.store.run(run['id'])
    assert call(client, run, 'model_list').status_code == 200
    assert call(client, run).json()['active_model'] == GLM
    assert app.state.store.messages(run['id'])[0]['content'] == prompt


def test_queue_models_and_newer_preference_survive_switch_and_retry(workspace):
    app, client = workspace
    run = active(app)
    queued, _ = app.state.store.enqueue_message(run['id'], 'Later request', 'later-request', OPUS, 'google:bob')
    selected = call(client, run).json()
    assert selected['active_model'] == GLM and selected['default_model'] == OPUS
    assert selected['default_updated'] is False
    assert call(client, run).json()['replayed'] is True
    assert app.state.store.messages(run['id'])[-1]['model'] == OPUS
    future, _ = app.state.store.enqueue_message(run['id'], 'Another request', 'another-request', user_id='google:bob')
    assert future['model'] == OPUS
    app.state.store.finish_message(run['id'], run['active_message_id'], 'Done')
    assert app.state.store.claim_message(run['id'])['id'] == queued['id']
    assert app.state.store.run(run['id'])['active_model'] == OPUS


def test_concurrent_retries_and_superseded_retries_never_replay_changes(workspace):
    app, client = workspace
    run = active(app)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(client, run), range(4)))
    assert all(result.status_code == 200 for result in results)
    assert sum(not result.json()['replayed'] for result in results) == 1
    assert len(app.state.store.rows("SELECT * FROM events WHERE message LIKE 'Model selected:%'")) == 1
    assert call(client, run, model='opus').status_code == 409
    assert call(client, run, model='opus', request_key='new-user-choice').status_code == 200
    retry = call(client, run).json()
    assert retry['status'] == 'superseded' and retry['active_model'] == OPUS
    assert app.state.store.run(run['id'])['model'] == OPUS


@pytest.mark.parametrize('arguments,status', [
    ({'model': 'unavailable-model'}, 422), ({'turn_id': 999999}, 409),
    ({'owner_id': 'google:bob'}, 422), ({'run_id': 'different-session'}, 422),
    ({'request_key': 'short'}, 422),
])
def test_invalid_or_stale_requests_do_not_change_model(workspace, arguments, status):
    app, client = workspace
    run = active(app)
    assert call(client, run, **arguments).status_code == status
    assert app.state.store.run(run['id'])['active_model'] == ASTRA
    assert not app.state.store.rows('SELECT * FROM model_switch_operations')


@pytest.mark.parametrize('changes', [{'status': 'saving'}, {'status': 'stopping'}, {'chat_enabled': 0},
                                   {'parent_run_id': 'parent'}, {'active_user_id': ''}, {'active_message_id': None}])
def test_tools_hidden_and_denied_outside_direct_active_chat(workspace, changes):
    app, client = workspace
    run = active(app)
    field, value = next(iter(changes.items()))
    app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, run['id']))
    assert not app.state.model_tools.tools(run)
    assert call(client, run).status_code in {401, 403}
    assert app.state.store.run(run['id'])['active_model'] == ASTRA


def test_requester_race_and_automated_runs_denied(workspace):
    app, client = workspace
    run = active(app)
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
    with pytest.raises(HTTPException) as error:
        app.state.model_tools.call(run, 'model_switch', {'turn_id': run['active_message_id'], 'model': GLM, 'request_key': 'requester-race'})
    assert error.value.status_code == 409
    owner = app.state.store.identity({'method': 'local', 'role': 'admin'})
    app.state.store.execute("INSERT INTO automations(id,owner_id,definition,created_at,updated_at) VALUES('automation',?,'{}',?,?)",
                            (owner, run['created_at'], run['created_at']))
    app.state.store.execute("INSERT INTO automation_runs VALUES('occurrence','automation',1,?,'started','',?)", (run['id'], run['created_at']))
    assert call(client, run).status_code == 403
    assert app.state.store.run(run['id'])['active_model'] == ASTRA
