"""Provider fault qualification through HTTP, the real SDK, graph and Temporal."""
import json
from types import SimpleNamespace

import httpx
import pytest

from deepagents_provider import answer, event, response_stream
from test_deepagents_runtime import durable_app  # noqa: F401
from test_deepagents_acceptance import web_worker, create, until


@pytest.mark.parametrize('after_write', [False, True])
@pytest.mark.parametrize('failure', ['truncated', 'incomplete', 'truncated_tool', 'incomplete_tool',
                                  'permission', 'policy', 'quota', 'rate_limit'])
async def test_provider_failure_never_completes_or_repeats_prior_tool(durable_app, monkeypatch, tmp_path, failure, after_write):
    app = durable_app
    store = app.state.store
    calls = []
    receipt = tmp_path / 'receipt.txt'
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')

    async def connected(name, arguments, **kwargs):
        assert name == 'notion_search'
        # The external transport creates a countable, non-idempotent receipt.
        # Everything before and after this transport runs through the real app.
        with receipt.open('a') as stream:
            stream.write('completed tool\n')
        return {'results': [{'id': 'fixture-result'}]}

    monkeypatch.setattr(app.state.connectors, 'call', connected)
    codes = {'permission': 'permission_error', 'policy': 'cyber_policy',
             'quota': 'insufficient_quota', 'rate_limit': 'rate_limit_error'}

    def tool_response(identity):
        response = answer(identity=identity)
        response['output'] = [{'type': 'function_call', 'id': 'fc_' + identity, 'call_id': 'call_' + identity,
            'name': 'workspace_call', 'status': 'completed', 'arguments': json.dumps({
                'name': 'notion_search', 'arguments_json': json.dumps({'query': identity})})}]
        return response

    async def upstream(request):
        if not request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'total_tokens': 100})
        calls.append(json.loads(request.content))
        if after_write and len(calls) == 1:
            response = tool_response('completed')
            wire = list(response_stream(response))
        elif failure in codes:
            status = 403 if failure == 'permission' else 400 if failure == 'policy' else 429
            return httpx.Response(status, headers={'Retry-After': '1'},
                json={'error': {'code': codes[failure], 'message': 'private-provider-body'}})
        else:
            response = tool_response('unfinished') if failure.endswith('_tool') else answer('This answer is unfinished.')
            wire = list(response_stream(response))
            if failure.startswith('truncated'):
                wire = wire[:-1]
            else:
                response.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
                wire[-1] = event('response.incomplete', response=response)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=b''.join(wire))

    actual = httpx.AsyncClient
    class ProviderClient(actual):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(upstream), **kw)
    monkeypatch.setattr('app.harness_gateway.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Process the fixture, preserve the tool result, then answer.', ['notion'])
        await until(lambda: store.run(run_id)['status'] in {'idle', 'failed', 'cancelled'}, seconds=40)
        assert store.run(run_id)['status'] == 'failed'
        assert not [m for m in store.messages(run_id) if m['role'] == 'assistant' and m['status'] == 'completed']
        assert 1 <= len(calls) - int(after_write) <= 3
        if failure in {'permission', 'policy'} or failure.startswith('incomplete'):
            assert len(calls) == 1 + int(after_write)
        assert receipt.read_text() == 'completed tool\n' if after_write else not receipt.exists()
        journal = store.rows('SELECT * FROM session_items WHERE run_id=?', (run_id,))
        assert 'This answer is unfinished.' not in str(journal)
        assert ('fixture-result' in str(journal)) is after_write
        diagnostics = [e['data'].get('error_code') for e in store.events(run_id, limit=1000)
                       if e['data'].get('phase') == 'broker_failure']
        expected = codes.get(failure, 'stream_incomplete' if failure.startswith('truncated') else 'max_output_tokens')
        assert expected in diagnostics
        assert 'private-provider-body' not in str(store.events(run_id, limit=1000))
