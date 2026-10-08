import json

import httpx
import pytest

from app.security import digest
from test_models import ASTRA, DEEPSEEK, GLM
from test_workspace import workspace


@pytest.mark.parametrize('route', ['chat/completions', 'messages', 'responses'])
def test_fireworks_affinity_is_stable_scoped_and_server_owned(workspace, monkeypatch, route):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Done'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kwargs: actual(
        transport=httpx.MockTransport(upstream), **kwargs))
    run = app.state.store.create_run('Cache affinity', '', 'modal', [], chat_enabled=True,
                                     model=DEEPSEEK, user_id='google:alice')
    app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    def send():
        response = client.post(f"/broker/{run['id']}/v1/{route}",
            headers={'Authorization': 'Bearer capability', 'x-session-affinity': 'untrusted'},
            json={'messages': [{'role': 'user', 'content': 'Hi'}], 'input': 'Hi',
                  'extra_headers': {'x-session-affinity': 'untrusted', 'Authorization': 'untrusted'},
                  'user': 'untrusted', 'stream': False})
        assert response.status_code == 200
        return captured[-1]
    first = send()['extra_headers']
    assert set(first) == {'x-session-affinity'}
    assert len(first['x-session-affinity']) == 64
    assert 'alice' not in first['x-session-affinity'] and first['x-session-affinity'] != 'untrusted'
    assert send()['extra_headers'] == first  # A new inference reuses the replica hint.
    app.state.store.execute('UPDATE runs SET active_model=? WHERE id=?', (GLM, run['id']))
    assert send()['extra_headers'] == first  # All Fireworks models use the policy.
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
    assert send()['extra_headers'] != first
    app.state.store.execute('UPDATE runs SET active_model=? WHERE id=?', (ASTRA, run['id']))
    assert 'extra_headers' not in send()
