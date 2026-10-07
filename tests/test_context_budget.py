import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.context_budget import ContextBudget, ContextPressure, ModelContextLimits, counting_input
from app.security import digest
from test_workspace import workspace


def limits(window=16000, output=4000):
    return ModelContextLimits(context_window=window, max_input_tokens=window, max_output_tokens=output)


def service(monkeypatch, handler, overrides=None):
    settings = Settings(_env_file=None, litellm_api_base='https://gateway.example/v1',
                        litellm_api_key='fixture', model_context_limits=overrides or {})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.context_budget.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(handler), **kw))
    return ContextBudget(settings)


@pytest.mark.asyncio
async def test_alias_uses_smallest_deployment_and_refreshes_cache(monkeypatch):
    seen = []
    def upstream(request):
        assert request.url.path == '/model/info'
        seen.append(request)
        return httpx.Response(200, json={'data': [
            {'model_name': 'alias', 'model_info': {'max_input_tokens': n, 'max_output_tokens': 4000,
                                                  'max_tokens': 99999999}}
            for n in [32000, 16000]]})
    budget = service(monkeypatch, upstream)
    assert (await budget.limits('alias')).context_window == 16000
    assert (await budget.limits('alias')).context_window == 16000
    assert len(seen) == 1
    monkeypatch.setattr('app.context_budget.time.monotonic', lambda: 10**12)
    await budget.limits('alias')
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('info', [{}, {'max_tokens': 128000}, {'max_input_tokens': True, 'max_output_tokens': 4000}])
async def test_unknown_or_ambiguous_limits_never_allow_inference(monkeypatch, info):
    budget = service(monkeypatch, lambda req: httpx.Response(200, json={
        'data': [{'model_name': 'alias', 'model_info': info}]}))
    with pytest.raises(HTTPException) as exc:
        await budget.check({'model': 'alias', 'messages': []})
    assert exc.value.status_code == 503 and 'MODEL_CONTEXT_LIMITS' in exc.value.detail


@pytest.mark.asyncio
async def test_output_room_is_reserved_without_changing_generation_fields(monkeypatch):
    budget = service(monkeypatch, lambda req: httpx.Response(200, json={'total_tokens': 10000}),
                     {'astra': limits(16000, 8000), 'glm': limits(32000, 8000)})
    payload = {'model': 'astra', 'messages': [{'role': 'user', 'content': 'hello'}]}
    with pytest.raises(ContextPressure):
        await budget.check(payload)
    assert set(payload) == {'model', 'messages'}
    result = await budget.check({**payload, 'max_tokens': 4000})
    assert result.output_tokens == 4000
    assert (await budget.check({**payload, 'model': 'glm'})).output_tokens == 8000
    with pytest.raises(HTTPException) as exc:
        await budget.check({**payload, 'max_tokens': 8001})
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_full_serialized_input_and_images_reach_counter(monkeypatch):
    seen = []
    def upstream(request):
        assert request.url.path == '/utils/token_counter'
        assert request.url.params['call_endpoint'] == 'true'
        body = json.loads(request.content)
        seen.append(body)
        text = json.dumps(body)
        assert all(marker in text for marker in ['private-memory', 'custom-schema', 'prior-reasoning', 'current-input'])
        content = body['messages'][0]['content']
        assert content[-1]['type'] == 'image_url'
        assert content[-1]['image_url']['url'] == 'data:image/png;base64,fixture'
        return httpx.Response(200, json={'total_tokens': 12000, 'tokenizer_type': 'openai_api'})
    budget = service(monkeypatch, upstream, {'alias': limits()})
    with pytest.raises(ContextPressure):
        await budget.check({'model': 'alias', 'system': 'private-memory',
            'tools': [{'name': 'custom-schema', 'input_schema': {'type': 'object'}}],
            'input': [{'type': 'reasoning', 'encrypted_content': 'prior-reasoning'},
                      {'role': 'user', 'content': [{'type': 'input_text', 'text': 'current-input'},
                                                 {'type': 'input_image', 'image_url': 'data:image/png;base64,fixture'}]}]})
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_counter_outage_uses_conservative_text_bound_but_never_guesses_images(monkeypatch):
    budget = service(monkeypatch, lambda req: httpx.Response(503), {'alias': limits()})
    text = {'model': 'alias', 'messages': [{'role': 'user', 'content': '界' * 1000}]}
    result = await budget.check(text)
    assert result.input_tokens > 3000 and result.method == 'utf8_upper_estimate'
    with pytest.raises(HTTPException) as exc:
        await budget.check({'model': 'alias', 'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/image.png'}}]}]})
    assert exc.value.status_code == 503


@pytest.mark.parametrize('route,field,output_field', [
    ('messages', 'messages', 'max_tokens'), ('responses', 'input', 'max_output_tokens'),
    ('chat/completions', 'messages', 'max_completion_tokens')])
def test_all_routes_check_injected_input_before_admission(workspace, monkeypatch, route, field, output_field):
    app, client = workspace
    async def small_limits(model): return limits(16000, 8000)
    async def count(payload):
        assert payload[output_field] == 8000
        assert payload['model'] == 'openai/gpt-6-astra'
        assert 'injected-private-memory' in json.dumps(payload)
        assert 'tool-schema' in json.dumps(payload)
        return 9000, 'fixture'
    monkeypatch.setattr(app.state.context_budget, 'limits', small_limits)
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    monkeypatch.setattr('app.memory.Memory.context', lambda *a: 'injected-private-memory')
    monkeypatch.setattr('app.context_budget.httpx.AsyncClient', lambda **kw: pytest.fail('Inference admitted'))
    run = app.state.store.create_run('context check', '', 'modal', [], model='openai/gpt-6-astra')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer cap'},
                           json={field: [{'role': 'user', 'content': 'small request'}],
                                 output_field: 8000, 'tools': [{'name': 'tool-schema'}]})
    assert response.status_code == 409 and response.headers['X-Moyai-Context'] == 'compact'
    assert response.json()['detail']['output_tokens'] == 8000
    assert app.state.store.run(run['id'])['model_calls'] == 0
    assert not app.state.store.rows('SELECT * FROM model_requests')
    assert 'injected-private-memory' not in json.dumps(app.state.store.events(run['id']))


def test_compaction_splits_to_fit_and_advances_only_returned_prefix(workspace, monkeypatch, tmp_path):
    from sandbox.context_store import ContextStore
    app, client = workspace
    async def small_limits(model): return limits(7000, 1000)
    monkeypatch.setattr(app.state.context_budget, 'limits', small_limits)
    requests = []
    def upstream(request):
        payload = json.loads(request.content)
        assert counting_input(payload)[1] <= 5400
        entries = json.loads(payload['messages'][1]['content'])['new_records']
        requests.append([entry['seq'] for entry in entries])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'All receipts retained.'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = app.state.store.create_run('compact', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    store = ContextStore(tmp_path / 'context.db', run['id'])
    store.initialize([{'role': 'assistant', 'content': 'receipt-' + str(i) + 'x' * 1500} for i in range(40)])
    def summarize(summary, entries, **kwargs):
        response = client.post(f"/broker/{run['id']}/context/compact", headers={'Authorization': 'Bearer cap'},
                               json={'summary': summary, 'entries': entries, 'cursor_protocol': 1, **kwargs})
        assert response.status_code == 200, response.text
        return response.json()
    store.compact(summarize, force=True)
    assert store.state()['cursor'] == 40
    assert [seq for request in requests for seq in request] == list(range(1, 41))
    assert len(requests) > 2
    assert store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == 40
    store.close()


def test_revocation_during_count_cannot_admit_model(workspace, monkeypatch):
    app, client = workspace
    run = app.state.store.create_run('count', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    async def count(payload):
        app.state.store.update_run(run['id'], status='cancelled', token_hash='')
        return 1, 'fixture'
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    response = client.post(f"/broker/{run['id']}/v1/messages", headers={'Authorization': 'Bearer cap'}, json={'messages': []})
    assert response.status_code == 401
    assert not app.state.store.rows('SELECT * FROM model_requests')


@pytest.mark.parametrize('route,field', [('messages', 'messages'), ('responses', 'input'), ('chat/completions', 'messages')])
@pytest.mark.parametrize('code,expected', [('context_length_exceeded', 409), ('rate_limit_exceeded', 502)])
def test_only_confirmed_provider_context_rejection_can_request_recovery(workspace, monkeypatch, route, field, code, expected):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    calls = []
    def upstream(request):
        calls.append(1)
        return httpx.Response(400, json={'error': {'code': code, 'message': 'PRIVATE provider detail'}})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.context_budget.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('count', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer cap'},
                           json={field: [{'role': 'user', 'content': 'small request'}]})
    assert response.status_code == expected
    assert ('X-Moyai-Context' in response.headers) == (expected == 409)
    assert calls == [1]
    assert app.state.store.rows('SELECT status FROM model_requests')[0]['status'] == 'failed'
    assert 'PRIVATE' not in response.text + json.dumps(app.state.store.events(run['id']))


@pytest.mark.asyncio
async def test_generic_tokenizer_cannot_undercount_other_model_family(monkeypatch):
    budget = service(monkeypatch, lambda req: httpx.Response(200, json={
        'total_tokens': 1, 'tokenizer_type': 'openai_tokenizer'}), {'glm': limits()})
    result = await budget.check({'model': 'glm', 'messages': [{'role': 'user', 'content': '界' * 1000}]})
    assert result.input_tokens > 3000
    assert result.method == 'conservative_gateway_estimate'


def test_compaction_provider_rejection_reduces_only_unprocessed_prefix(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    seen = []
    def upstream(request):
        entries = json.loads(json.loads(request.content)['messages'][1]['content'])['new_records']
        seen.append(entries)
        if len(seen) == 1:
            return httpx.Response(400, json={'error': {'code': 'context_length_exceeded'}})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'Saved receipt.'}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('compact', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    entries = [{'seq': i, 'excerpt': f'receipt {i}'} for i in range(1, 9)]
    response = client.post(f"/broker/{run['id']}/context/compact", headers={'Authorization': 'Bearer cap'},
                           json={'summary': '', 'entries': entries, 'cursor_protocol': 1})
    assert response.status_code == 200 and response.json()['through_seq'] == 4
    assert seen == [entries, entries[:4]]
    assert [r['status'] for r in app.state.store.rows('SELECT status FROM model_requests ORDER BY created_at')] == ['failed', 'completed']


def test_legacy_compactor_cannot_skip_records_during_rolling_deployment(workspace, monkeypatch):
    app, client = workspace
    async def small_limits(model): return limits(5000, 1000)
    monkeypatch.setattr(app.state.context_budget, 'limits', small_limits)
    run = app.state.store.create_run('compact', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/context/compact", headers={'Authorization': 'Bearer cap'},
        json={'summary': '', 'entries': [{'seq': i, 'excerpt': 'large receipt ' * 80} for i in range(1, 9)]})
    assert response.status_code == 422 and 'Reconnect' in response.json()['detail']
    assert not app.state.store.rows('SELECT * FROM model_requests')
