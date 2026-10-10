import json
import time

from test_workspace import workspace


def test_export_retains_local_model_and_tool_traces(workspace):
    app, client = workspace
    store, tracing = app.state.store, app.state.store.tracing
    run = store.create_run('Debug this', '', 'modal', [], chat_enabled=True)
    turn = store.claim_message(run['id'])
    run = store.run(run['id'])
    response = {'choices': [{'message': {
        'content': 'I found the problem.', 'reasoning_content': 'hidden thought',
        'tool_calls': [{'function': {'name': 'read_file', 'arguments': 'private raw args'}}],
    }}]}
    tracing.model(run, 'model-request', time.time_ns(), [
        {'role': 'system', 'content': 'hidden system prompt'},
        {'role': 'user', 'content': 'Debug this'},
    ], response, 'completed')
    start = time.time_ns()
    tool = {'tool': 'read_file', 'call_id': 'tool-call', 'start_ns': start,
            'end_ns': start, 'input': {'path': 'app.py', 'api_key': 'private-key'},
            'output': 'line 1', 'status': 'completed'}
    tracing.tool(run['id'], tool)
    tracing.tool(run['id'], tool)  # replay must not duplicate a span
    store.finish_message(run['id'], turn['id'], 'Fixed.')
    store.update_run(run['id'], token_hash='private-token-hash', pending_result='private-context')
    result = client.get(f'/api/runs/{run["id"]}/export')
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    assert result.headers['content-disposition'].endswith('.json"')
    data = result.json()
    assert data['format'] == 'moyai.session' and data['version'] == 1
    assert any(message['content'] == 'Fixed.' for message in data['messages'])
    llms = [s for s in data['traces'] if s['attributes']['openinference.span.kind'] == 'LLM']
    tools = [s for s in data['traces'] if s['attributes']['openinference.span.kind'] == 'TOOL']
    assert len(llms) == len(tools) == 1
    assert llms[0]['trace_id'] == tools[0]['trace_id']
    assert llms[0]['turn_id'] == turn['id']
    assert 'I found the problem.' in llms[0]['attributes']['output.value']
    assert 'read_file' in llms[0]['attributes']['output.value']
    assert 'app.py' in tools[0]['attributes']['input.value']
    assert tools[0]['attributes']['output.value'] == 'line 1'
    for secret in ('hidden thought', 'hidden system prompt', 'private raw args',
                   'private-key', 'private-token-hash', 'private-context'):
        assert secret not in result.text
    assert not tracing.outboxes  # local capture does not need an exporter


def test_export_paginates_events_and_excludes_other_sessions(workspace):
    app, client = workspace
    store = app.state.store
    run = store.create_run('Export', '', 'demo', [], chat_enabled=True)
    other = store.create_run('Other', '', 'demo', [], chat_enabled=True)
    with store.connect() as connection:
        for index in range(10005):
            connection.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                               (run['id'], 'status', str(index), '{}', '2026-10-10'))
    store.event(other['id'], 'status', 'other-session-data')
    response = client.get(f'/api/runs/{run["id"]}/export')
    assert response.status_code == 200
    assert len(response.json()['events']) >= 10005
    assert 'other-session-data' not in response.text
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', ('2026-10-10', run['id']))
    assert client.get(f'/api/runs/{run["id"]}/export').status_code == 404
    assert client.get('/api/runs/missing/export').status_code == 404


def test_export_requires_authentication(workspace):
    app, client = workspace
    app.state.settings.google_client_id = 'fixture'
    app.state.settings.google_client_secret = 'fixture'
    client.cookies.clear()
    assert client.get('/api/runs/missing/export').status_code == 401
