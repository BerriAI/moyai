import json

import httpx
import pytest

from app.model_selection import ASTRA, ASTRA_ULTRAFAST, gateway_payload
from app.security import digest
from test_workspace import workspace


GLM = 'fireworks_ai/glm-5p3'


@pytest.mark.parametrize('route', ['responses', 'messages', 'chat/completions'])
@pytest.mark.parametrize('model', [ASTRA, GLM, 'anthropic/claude-opus-5-5'])
def test_broker_pins_content_policy_fallback_and_preserves_response(workspace, monkeypatch, route, model):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    calls = []
    result = {'id': 'fallback-result', 'model': GLM, 'status': 'completed',
              'choices': [{'message': {'role': 'assistant', 'content': 'Review complete.'}}],
              'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
    def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.001'}, json=result)
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('Review this code', '', 'modal', [], model=model)
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/{route}",
        headers={'Authorization': 'Bearer cap'}, json={
            'input': 'Review this code', 'messages': [{'role': 'user', 'content': 'Review this code'}],
            'stream': False, 'model': GLM, 'content_policy_fallbacks': [{'*': ['unapproved-model']}],
            'fallbacks': ['unapproved-model'], 'api_base': 'https://unapproved.example',
        })
    assert response.status_code == 200
    assert response.json() == result
    sent, = calls
    assert sent['model'] == model
    if model == ASTRA:
        assert sent['content_policy_fallbacks'] == [{ASTRA: [{'model': GLM, 'service_tier': None}]}]
    else:
        assert 'content_policy_fallbacks' not in sent
    assert 'fallbacks' not in sent and 'api_base' not in sent
    assert app.state.store.run(run['id'])['model'] == model
    ledger, = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))
    assert ledger['status'] == 'completed' and ledger['cost'] == '0.001'


def test_ultrafast_fallback_clears_astra_service_tier_without_changing_saved_choice():
    payload = {'model': ASTRA_ULTRAFAST, 'input': 'Review this code'}
    sent = gateway_payload(payload, '/v1/responses')
    assert sent['model'] == ASTRA and sent['service_tier'] == 'ultrafast'
    target, = sent['content_policy_fallbacks'][0][ASTRA]
    assert target == {'model': GLM, 'service_tier': None}
    assert payload == {'model': ASTRA_ULTRAFAST, 'input': 'Review this code'}
