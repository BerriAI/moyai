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
    payload = {'model': 'astra', 'messages': [{'role': 'user', 'content': 'x' * 9000}]}
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
async def test_view_allocation_uses_output_allowance_without_counting_partial_input(monkeypatch):
    budget = service(monkeypatch, lambda req: pytest.fail('Unexpected remote count'),
                     {'alias': limits(16000, 8000)})
    async def count(*args, **kwargs):
        pytest.fail('Only the final prepared request may be counted')
    monkeypatch.setattr(budget, 'count', count)
    default = await budget.allocation({'model': 'alias'})
    explicit = await budget.allocation({'model': 'alias', 'max_output_tokens': 4000})
    assert default.output_tokens == 8000 and explicit.output_tokens == 4000
    assert explicit.input_budget > default.input_budget
    with pytest.raises(HTTPException):
        await budget.allocation({'model': 'alias', 'max_output_tokens': 8001})


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


@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
def test_responses_tool_images_are_counted_as_images_without_changing_wire(kind):
    image = {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 10000, 'detail': 'high'}
    payload = {'input': [{'type': kind, 'call_id': 'image-call', 'id': 'output-id', 'output': [
        {'type': 'input_text', 'text': 'Screenshot receipt'}, image]}]}
    original = json.dumps(payload)
    messages, byte_count, images = counting_input(payload)
    content = messages[0]['content']
    assert images and byte_count < 300
    assert content[1:] == [{'type': 'image_url', 'image_url': {'url': image['image_url'], 'detail': 'high'}}]
    assert json.loads(content[0]['text']) == {'input': [
        {**payload['input'][0], 'output': [{'type': 'input_text', 'text': 'Screenshot receipt'},
                                        '[image counted separately]']} ]}
    assert json.dumps(payload) == original


def test_nested_anthropic_tool_result_keeps_images_separate():
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}
    payload = {'messages': [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'mcp-image-call', 'content': [
            {'type': 'text', 'text': 'MCP screenshot'}, image]}]}]}
    messages, _, images = counting_input(payload)
    assert images
    assert messages[0]['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}
    assert image == payload['messages'][0]['content'][0]['content'][1]


@pytest.mark.parametrize('media_type', ['input_image', 'input_file'])
def test_image_like_argument_and_schema_dictionaries_are_opaque(media_type):
    opaque = {'type': media_type, 'image_url': 'user-data-not-an-image', 'content': [{'type': media_type}]}
    payload = {
        'tools': [{'name': 'schema', 'input_schema': {'input': opaque, 'content': opaque, 'output': opaque}}],
        'input': [{'type': 'custom_tool_call', 'call_id': 'call', 'input': opaque},
                  {'type': 'function_call', 'call_id': 'call-2', 'arguments': {'content': opaque}},
                  {'role': 'user', 'content': {'input': opaque}}],
        'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-3', 'input': opaque}]}],
    }
    messages, byte_count, images = counting_input(payload)
    assert not images
    assert len(messages[0]['content']) == 1
    assert json.loads(messages[0]['content'][0]['text']) == payload
    assert byte_count == len(messages[0]['content'][0]['text'].encode())


@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
@pytest.mark.parametrize('media_type', ['input_audio', 'audio', 'input_file', 'file', 'video', 'video_url'])
def test_unsupported_media_in_responses_tool_output_fails_before_counting(kind, media_type):
    with pytest.raises(HTTPException) as exc:
        counting_input({'input': [{'type': kind, 'call_id': 'media-call', 'output': [{'type': media_type}]}]})
    assert exc.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
async def test_tool_image_uses_provider_count_instead_of_base64_text_bound(monkeypatch, kind):
    image = {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 1200000, 'detail': 'high'}
    original = {'model': 'astra', 'input': [{'role': 'user', 'content': 'Inspect the screenshot.'}]}
    payload = {**original, 'input': [*original['input'], {'type': kind, 'call_id': 'image-call', 'output': [image]}]}
    seen = []
    def upstream(request):
        body = json.loads(request.content)
        content = body['messages'][0]['content']
        assert len(content[0]['text']) < 300
        assert content[1]['image_url'] == {'url': image['image_url'], 'detail': 'high'}
        seen.append(body)
        return httpx.Response(200, json={'total_tokens': 110000, 'tokenizer_type': 'openai_api'})
    budget = service(monkeypatch, upstream, {'astra': limits(922000, 128000)})
    budget.remember(original, {'prompt_tokens': 109182}, 'run')
    result = await budget.check(payload, scope='run')
    assert result.input_tokens == 110000 and result.input_budget == 714600
    assert result.method == 'provider_serialization_estimate'
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
@pytest.mark.parametrize('counter', [
    (503, {}), (200, {'total_tokens': 1, 'tokenizer_type': 'openai_tokenizer'}),
    (200, {'total_tokens': 0, 'tokenizer_type': 'openai_api'}),
])
async def test_new_tool_image_never_uses_text_fallback_when_counter_unavailable(monkeypatch, kind, counter):
    budget = service(monkeypatch, lambda req: httpx.Response(counter[0], json=counter[1]), {'astra': limits()})
    original = {'model': 'astra', 'input': [{'role': 'user', 'content': 'Inspect.'}]}
    budget.remember(original, {'prompt_tokens': 100}, 'run')
    payload = {**original, 'input': [*original['input'], {'type': kind, 'call_id': 'image-call', 'output': [
        {'type': 'input_image', 'image_url': 'https://example.test/image.png'}]}]}
    with pytest.raises(HTTPException) as exc:
        await budget.check(payload, scope='run')
    assert exc.value.status_code == 503 and 'not sent' in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
async def test_unchanged_tool_image_prefix_uses_usage_but_appended_image_requires_counter(monkeypatch, kind):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(503)
    budget = service(monkeypatch, upstream, {'astra': limits()})
    image = {'type': kind, 'call_id': 'image-call', 'output': [
        {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 100000}]}
    original = {'model': 'astra', 'input': [image]}
    budget.remember(original, {'prompt_tokens': 2500}, 'run')
    followup = {**original, 'input': [image, {'role': 'user', 'content': 'Explain it.'}]}
    result = await budget.check(followup, scope='run')
    assert result.method == 'usage_plus_utf8_delta' and 2500 < result.input_tokens < 2700
    assert not calls
    with pytest.raises(HTTPException) as exc:
        await budget.check({**followup, 'input': [*followup['input'], {**image, 'call_id': 'new-call'}]}, scope='run')
    assert exc.value.status_code == 503 and len(calls) == 1


@pytest.mark.parametrize('kind', ['function_call_output', 'custom_tool_call_output'])
def test_tool_image_reaches_inference_with_original_tool_identity(workspace, monkeypatch, kind):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    monkeypatch.setattr(app.state.context_budget, 'count', ContextBudget.count.__get__(app.state.context_budget))
    image = {'type': 'input_image', 'image_url': 'data:image/png;base64,fixture', 'detail': 'low'}
    output = {'type': kind, 'id': 'output-id', 'call_id': 'image-call', 'output': [image]}
    seen = []
    def upstream(request):
        body = json.loads(request.content)
        seen.append(request.url.path)
        if request.url.path == '/utils/token_counter':
            assert body['messages'][0]['content'][1]['image_url'] == {'url': image['image_url'], 'detail': 'low'}
            return httpx.Response(200, json={'total_tokens': 100, 'tokenizer_type': 'openai_api'})
        assert request.url.path == '/v1/responses'
        assert body['input'][-1] == output
        return httpx.Response(200, json={'status': 'completed', 'output': [], 'usage': {'input_tokens': 100, 'output_tokens': 1}})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.context_budget.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('inspect image', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/responses", headers={'Authorization': 'Bearer cap'},
                           json={'input': [output]})
    assert response.status_code == 200, response.text
    assert seen == ['/utils/token_counter', '/v1/responses']


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
    async def count(payload, **kwargs):
        assert payload[output_field] == 8000
        assert payload['model'] == 'openai/gpt-6-astra'
        assert 'injected-private-memory' in json.dumps(payload)
        assert 'injected-private-skills' in json.dumps(payload)
        assert 'tool-schema' in json.dumps(payload)
        return 9000, 'fixture'
    monkeypatch.setattr(app.state.context_budget, 'limits', small_limits)
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    monkeypatch.setattr('app.memory.Memory.context', lambda *a: 'injected-private-memory')
    monkeypatch.setattr('app.skills.Skills.context', lambda *a: 'injected-private-skills')
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
    assert 'injected-private-skills' not in json.dumps(app.state.store.events(run['id']))


def test_compaction_splits_to_fit_and_advances_only_returned_prefix(workspace, monkeypatch, tmp_path):
    from agent.context_store import ContextStore
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
    async def count(payload, **kwargs):
        app.state.store.update_run(run['id'], status='cancelled', token_hash='')
        return 1, 'fixture'
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    response = client.post(f"/broker/{run['id']}/v1/messages", headers={'Authorization': 'Bearer cap'}, json={'messages': []})
    assert response.status_code == 401
    assert not app.state.store.rows('SELECT * FROM model_requests')


@pytest.mark.parametrize('route,field', [('messages', 'messages'), ('responses', 'input'), ('chat/completions', 'messages')])
@pytest.mark.parametrize('code,expected', [('context_length_exceeded', 409), ('rate_limit_exceeded', 400)])
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
    result = await budget.check({'model': 'glm', 'messages': [{'role': 'user', 'content': '界' * 3500}]})
    assert result.input_tokens > 10500
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


@pytest.mark.asyncio
async def test_usage_anchor_avoids_counter_and_invalidates_on_context_changes(monkeypatch):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={'total_tokens': 3000, 'tokenizer_type': 'openai_api'})
    budget = service(monkeypatch, upstream, {'astra': limits(), 'glm': limits()})
    original = {'model': 'astra', 'system': 'private A', 'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'history ' * 1500,
                                    'cache_control': {'type': 'ephemeral'}}]}]}
    budget.remember(original, {'prompt_tokens': 3000}, 'run-a')
    followup = json.loads(json.dumps(original))
    followup['messages'][0]['content'][0].pop('cache_control')
    followup['messages'].append({'role': 'assistant', 'content': 'next step'})
    result = await budget.check(followup, scope='run-a')
    assert result.method == 'usage_plus_utf8_delta'
    assert 3000 < result.input_tokens < 3200
    assert not calls
    # Another run, model, new injected memory, or changed old history cannot
    # reuse a cheaper input count from an unrelated prompt.
    for payload, scope in [(followup, 'run-b'), ({**followup, 'model': 'glm'}, 'run-a'),
                           ({**followup, 'system': 'private B'}, 'run-a')]:
        await budget.check(payload, scope=scope)
    changed = json.loads(json.dumps(followup))
    changed['messages'][0]['content'][0]['text'] += 'changed old history'
    await budget.check(changed, scope='run-a')
    assert len(calls) == 4
    budget.settings.litellm_api_key = 'rotated'
    await budget.check(followup, scope='run-a')
    budget.settings.litellm_api_key = 'fixture'
    budget.settings.litellm_api_base = 'https://another-gateway.example/v1'
    await budget.check(followup, scope='run-a')
    assert len(calls) == 6
    budget.settings.litellm_api_base = 'https://gateway.example/v1'
    monkeypatch.setattr('app.context_budget.time.monotonic', lambda: 10**12)
    await budget.check(followup, scope='run-a')
    assert len(calls) == 7


@pytest.mark.asyncio
async def test_ordinary_turns_never_request_remote_count(monkeypatch):
    budget = service(monkeypatch, lambda req: pytest.fail('Unexpected counting network call'), {'astra': limits()})
    payload = {'model': 'astra', 'messages': []}
    for i in range(20):
        payload['messages'].append({'role': 'user', 'content': f'Continue step {i}.'})
        result = await budget.check(payload, scope='run')
        budget.remember(payload, {'prompt_tokens': 100 + i * 10}, 'run')
    assert result.method == 'usage_plus_utf8_delta'


@pytest.mark.asyncio
async def test_new_images_require_count_even_with_usage_anchor(monkeypatch):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={'total_tokens': 2500, 'tokenizer_type': 'openai_api'})
    budget = service(monkeypatch, upstream, {'astra': limits()})
    image = {'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/image.png'}}]}
    payload = {'model': 'astra', 'messages': [image]}
    await budget.check(payload, scope='run')
    budget.remember(payload, {'prompt_tokens': 2500}, 'run')
    followup = {**payload, 'messages': [image, {'role': 'user', 'content': 'Explain it.'}]}
    assert (await budget.check(followup, scope='run')).method == 'usage_plus_utf8_delta'
    assert len(calls) == 1
    await budget.check({**followup, 'messages': [*followup['messages'], image]}, scope='run')
    assert len(calls) == 2


def test_usage_fingerprint_preserves_tool_arguments_and_ignores_truncated_usage():
    budget = ContextBudget(SimpleNamespace())
    payload = {'model': 'astra', 'input': [{'type': 'tool_use', 'input': {'cache_control': 'first'}}]}
    changed = {'model': 'astra', 'input': [{'type': 'tool_use', 'input': {'cache_control': 'second'}}]}
    assert budget.fingerprint(payload) != budget.fingerprint(changed)
    budget.remember({**payload, 'truncation': 'auto'}, {'prompt_tokens': 100}, 'run')
    assert not budget.usage


def test_native_window_is_authenticated_and_reserves_injected_context(workspace, monkeypatch):
    app, client = workspace
    async def small_limits(model): return limits(100000, 20000)
    monkeypatch.setattr(app.state.context_budget, 'limits', small_limits)
    monkeypatch.setattr(app.state.memory, 'context', lambda run: 'private-marker' * 100)
    monkeypatch.setattr(app.state.skills, 'context', lambda run: '')
    run = app.state.store.create_run('native window', '', 'modal', [], model='openai/gpt-6-astra')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    path = f"/broker/{run['id']}/context/window"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={'Authorization': 'Bearer cap'})
    assert response.status_code == 200
    assert response.json() == {'model': 'openai/gpt-6-astra', 'input_budget': 72000 - 1400 - 4096,
                               'live_compaction': True}
    assert 'private' not in response.text
    assert not app.state.store.rows('SELECT * FROM model_requests')
