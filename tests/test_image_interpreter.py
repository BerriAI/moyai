import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json

import httpx
import pytest

from app.context_budget import ContextBudget, counting_input, map_images
from app.image_interpreter import ImageInterpreter, InterpretationUnavailable
from app.security import digest
from test_attachments import png, start, upload
from test_spend import active
from test_workspace import workspace


GLM = 'fireworks_ai/glm-5p3'
VISION = 'openai/gpt-6-astra'
IMAGE = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}
READING = 'Error: AgentCore runtime ARN is missing. The Submit button is disabled.'


def gateway(app, monkeypatch, *, selected=GLM, vision=False, generic=False,
            broken=(), counter_outage=False, pause=None):
    settings = app.state.settings
    settings.litellm_api_base = 'https://gateway.example/v1'
    settings.litellm_api_key = 'workspace-only-key'
    budget = app.state.context_budget
    # Exercise real metadata and image counting, not the fixture byte counter.
    monkeypatch.setattr(budget, 'count', ContextBudget.count.__get__(budget))
    monkeypatch.setattr(budget, 'limits', ContextBudget.limits.__get__(budget))
    seen = {'helper': [], 'agent': [], 'counter': []}

    async def upstream(request):
        assert request.headers['authorization'] == 'Bearer workspace-only-key'
        if request.url.path == '/model/info':
            return httpx.Response(200, json={'data': [
                {'model_name': model, 'model_info': {'supports_vision': vision if model == selected else True,
                 'max_input_tokens': 100000, 'max_output_tokens': 16000}}
                for model in settings.allowed_models()]})
        payload = json.loads(request.content)
        if request.url.path == '/utils/token_counter':
            seen['counter'].append(payload)
            if counter_outage:
                return httpx.Response(503)
            return httpx.Response(200, json={'total_tokens': 1200, 'tokenizer_type':
                'openai_tokenizer' if generic and payload['model'] == selected else 'openai_api'})
        is_helper = payload['model'] != selected
        seen['helper' if is_helper else 'agent'].append(payload)
        if is_helper:
            assert request.url.path == '/v1/chat/completions'
            assert not payload.get('tools') and not payload.get('stream')
            assert 'private-memory' not in json.dumps(payload)
            assert 'private-user-prompt' not in json.dumps(payload)
            if pause:
                await pause()
            if payload['model'] in broken:
                return httpx.Response(503)
            images = [part for part in payload['messages'][-1]['content'] if part['type'] == 'image_url']
            content = json.dumps({'images': [{'index': i, 'description': READING} for i in range(1, len(images) + 1)]})
        else:
            content = 'I can continue the task.'
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.003',
            'x-litellm-call-id': request.headers['x-litellm-call-id']}, json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                'usage': {'prompt_tokens': 1200, 'completion_tokens': 30, 'total_tokens': 1230}})

    actual = httpx.AsyncClient
    monkeypatch.setattr('app.image_interpreter.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))
    return seen


def post(client, run, body, route='chat/completions'):
    return client.post(f"/broker/{run['id']}/v1/{route}",
                       headers={'Authorization': 'Bearer capability'}, json=body)


@pytest.mark.parametrize('route,field', [('messages', 'messages'), ('responses', 'input'), ('chat/completions', 'messages')])
@pytest.mark.parametrize('generic', [False, True])
def test_glm_screenshot_reaches_agent_with_reading_and_retries_reuse_it(workspace, monkeypatch, route, field, generic):
    app, client = workspace
    file = upload(client, 'earlier-thread-screenshot.png', png()).json()
    run_id = start(app, client, monkeypatch, [file]).json()['id']
    store = app.state.store
    message = store.claim_message(run_id)
    store.update_run(run_id, status='running', token_hash=digest('capability'))
    store.execute('UPDATE runs SET active_model=?,mode=? WHERE id=?', (GLM, 'modal', run_id))
    run = store.run(run_id)
    monkeypatch.setattr('app.memory.Memory.context', lambda *a: 'private-memory')
    seen = gateway(app, monkeypatch, vision=generic, generic=generic)
    body = {field: [{'role': 'user', 'content': f"private-user-prompt [moyai-attachment:{file['id']}]"}]}
    original = copy.deepcopy(body)
    for _ in range(2):
        response = post(client, run, body, route)
        assert response.status_code == 200, response.text
    assert body == original
    assert len(seen['helper']) == 1 and len(seen['agent']) == 2
    for payload in seen['agent']:
        assert payload['model'] == GLM
        assert not counting_input(payload)[2]
        assert READING in json.dumps(payload) and 'private-memory' in json.dumps(payload)
    assert client.get(file['url']).content == png()
    assert READING not in json.dumps(store.messages(run_id))
    assert READING not in json.dumps(store.events(run_id))
    requests = store.rows('SELECT * FROM model_requests WHERE run_id=?', (run_id,))
    assert len(requests) == 3
    assert [row['model'] for row in requests] == [VISION, GLM, GLM]
    assert all(row['status'] == 'completed' and row['cost'] == '0.003' for row in requests)
    assert all(row['message_id'] == message['id'] and row['user_id'] == run['active_user_id'] for row in requests)
    assert store.run(run_id)['model_calls'] == 3


def test_supported_native_vision_is_unchanged(workspace, monkeypatch):
    app, client = workspace
    run = active(app, model=VISION)
    seen = gateway(app, monkeypatch, selected=VISION, vision=True)
    body = {'messages': [{'role': 'user', 'content': [IMAGE]}]}
    response = post(client, run, body)
    assert response.status_code == 200, response.text
    assert not seen['helper']
    assert seen['agent'][0]['messages'][-1:] == body['messages']
    assert len(seen['counter']) == 1


@pytest.mark.parametrize('kind', ['messages', 'responses', 'chat/completions'])
def test_tool_images_are_replaced_in_their_original_tool_result(workspace, monkeypatch, kind):
    app, client = workspace
    run = active(app, model=GLM)
    seen = gateway(app, monkeypatch)
    if kind == 'messages':
        body = {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'read',
            'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}]}]}]}
    elif kind == 'responses':
        body = {'input': [{'type': 'function_call_output', 'call_id': 'read',
            'output': [{'type': 'input_image', 'image_url': IMAGE['image_url']['url']}]}]}
    else:
        body = {'messages': [{'role': 'tool', 'tool_call_id': 'read', 'content': [IMAGE]}]}
    original = copy.deepcopy(body)
    response = post(client, run, body, kind)
    assert response.status_code == 200, response.text
    assert body == original and len(seen['helper']) == 1
    payload = seen['agent'][0]
    assert READING in json.dumps(payload) and 'read' in json.dumps(payload)
    assert not counting_input(payload)[2]
    if kind == 'responses':
        assert payload['input'][0]['output'][0]['type'] == 'input_text'


def test_fallback_model_is_used_if_first_helper_fails(workspace, monkeypatch):
    app, client = workspace
    run = active(app, model=GLM)
    seen = gateway(app, monkeypatch, broken=[VISION])
    response = post(client, run, {'messages': [{'role': 'user', 'content': [IMAGE]}]})
    assert response.status_code == 200
    assert [p['model'] for p in seen['helper']] == [VISION, 'openai/gpt-6.1-sol']
    assert READING in json.dumps(seen['agent'])


@pytest.mark.parametrize('counter_outage', [False, True])
def test_image_outage_does_not_restart_task_or_pretend_image_was_read(workspace, monkeypatch, counter_outage):
    app, client = workspace
    run = active(app, model=GLM)
    seen = gateway(app, monkeypatch, counter_outage=counter_outage, broken=app.state.settings.allowed_models())
    body = {'messages': [{'role': 'user', 'content': [IMAGE, {'type': 'text', 'text': 'Continue the existing task'}]}]}
    for _ in range(2):
        response = post(client, run, body)
        assert response.status_code == 200, response.text
    assert len(seen['helper']) == (0 if counter_outage else 2)
    assert len(seen['agent']) == 2
    assert all(not counting_input(payload)[2] for payload in seen['agent'])
    assert 'Image not read' in json.dumps(seen['agent'])
    assert 'Continue the existing task' in json.dumps(seen['agent'])
    assert READING not in json.dumps(seen['agent'])


@pytest.mark.parametrize('change', ['stop', 'requester', 'model'])
def test_scope_change_cancels_helper_and_prevents_follow_on_inference(workspace, monkeypatch, change):
    app, client = workspace
    run = active(app, model=GLM)
    cancelled = []

    async def change_scope():
        if change == 'stop':
            app.state.store.update_run(run['id'], status='cancelled', token_hash='')
        elif change == 'requester':
            app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
        else:
            app.state.store.execute('UPDATE runs SET active_model=? WHERE id=?', (VISION, run['id']))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    seen = gateway(app, monkeypatch, pause=change_scope)
    response = post(client, run, {'messages': [{'role': 'user', 'content': [IMAGE]}]})
    assert response.status_code in {401, 409}
    assert cancelled and len(seen['helper']) == 1 and not seen['agent']
    assert [row['status'] for row in app.state.store.rows('SELECT status FROM model_requests')] == ['interrupted']


def test_concurrent_retries_share_reading_but_another_run_cannot(workspace, monkeypatch):
    app, client = workspace
    run = active(app, model=GLM)
    async def pause():
        await asyncio.sleep(.05)
    seen = gateway(app, monkeypatch, pause=pause)
    body = {'messages': [{'role': 'user', 'content': [IMAGE]}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: post(client, run, body), range(2)))
    assert [reply.status_code for reply in replies] == [200, 200]
    assert len(seen['helper']) == 1
    other = active(app, user='google:bob', model=GLM)
    assert post(client, other, body).status_code == 200
    assert len(seen['helper']) == 2


def test_tool_arguments_are_data_even_if_they_resemble_images():
    payload = {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'write_file',
        'id': 'one', 'input': {'content': [IMAGE]}}]}],
        'tools': [{'name': 'write_file', 'input_schema': {'examples': [{'content': [IMAGE]}]}}]}
    assert not counting_input(payload)[2]
    assert map_images(payload, lambda _: pytest.fail('Rewrote tool arguments')) == payload


@pytest.mark.parametrize('finish,readings', [
    ('length', [{'index': 1, 'description': READING}]),
    ('stop', []), ('stop', [{'index': 2, 'description': READING}]),
    ('stop', [{'index': True, 'description': READING}]),
    ('stop', [{'index': 1, 'description': READING}] * 2),
])
def test_incomplete_or_misindexed_readings_are_never_cached_as_success(finish, readings):
    raw = json.dumps({'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps({'images': readings})}}]})
    with pytest.raises(InterpretationUnavailable):
        ImageInterpreter.parse(raw, [b'image'])
