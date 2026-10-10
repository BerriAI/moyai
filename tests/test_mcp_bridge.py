import asyncio
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.security import digest
from test_broker_transport import diagnostic_relay
from test_skill_saving import call, form
from test_spend import active, sign_in
from test_workspace import workspace


def test_stdio_bridge_discovers_tools_and_forwards_only_run_token():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer test-run-token"
            assert self.path == "/tools"
            self.reply([{"name": "linear_search", "description": "Fixture search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
                        {"name":"credentials_run","description":"Scoped command","inputSchema":{"type":"object"}}])

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer test-run-token"
            if self.path == '/credentials/materialize':
                args=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert args=={'request_ids':['a'*32]}
                self.reply({'status':'ready','bindings':[{'request_id':'a'*32,'revision':1,'format':'env','env_var':'',
                            'value':json.dumps({'TEST_KEY':'synthetic-stdio-secret'})}]})
                return
            assert self.path == "/tools/call"
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if calls[-1]['name'] == 'skills_save':
                self.reply({'detail':'Skill revision changed. Use expected_revision=2 after reviewing the current skill.'},409)
                return
            if calls[-1]['name'] == 'skills_search':
                self.reply({'detail':'The active turn changed. Use the current turn_id.'},409)
                return
            self.reply({"issues": [{"identifier": "LIT-123", "title": "Fixture issue"}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "linear_search", "arguments": {"query": "fixture"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "unknown_method"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name":"skills_save", "arguments":{}}},
        {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"credentials_run","arguments":{
            "request_ids":['a'*32],"command":shlex.join([sys.executable,'-c',"import os; print(os.environ['TEST_KEY'])"])}}},
        {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"skills_search","arguments":{"query":"benchmark","turn_id":1}}},
    ]
    try:
        script = Path(__file__).resolve().parents[1] / "sandbox" / "mcp_bridge.py"
        result = subprocess.run([sys.executable, str(script)], input="\n".join(json.dumps(m) for m in messages)+"\n",
                                text=True, capture_output=True, timeout=10,
                                env={"PATH": os.environ["PATH"], "WORKSPACE_BROKER_URL": f"http://127.0.0.1:{server.server_port}", "WORKSPACE_RUN_TOKEN": "test-run-token"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(output) == 7  # notifications never receive responses
    assert output[0]["result"]["protocolVersion"] == "2025-03-26"
    names = {tool["name"] for tool in output[1]["result"]["tools"]}
    assert names == {"credentials_run", "linear_search", "browser_open", "browser_read", "browser_click", "browser_fill", "browser_screenshot", "browser_record_start", "browser_record_stop", "browser_key", "browser_scroll"}
    assert "Fixture issue" in output[2]["result"]["content"][0]["text"]
    assert output[3]["error"]["code"] == -32601
    assert output[4]['result']['isError']
    assert 'expected_revision=2' in output[4]['result']['content'][0]['text']
    executed=json.loads(output[5]['result']['content'][0]['text'])
    assert executed['exit_code']==0 and '[credential redacted]' in executed['output']
    assert output[6]['result']['isError']
    assert 'Use the current turn_id' in output[6]['result']['content'][0]['text']
    assert 'synthetic-stdio-secret' not in result.stdout and 'synthetic-stdio-secret' not in result.stderr
    assert calls == [{"name": "linear_search", "arguments": {"query": "fixture"}}, {'name':'skills_save','arguments':{}},
                     {'name':'skills_search','arguments':{'query':'benchmark','turn_id':1}}]


@pytest.mark.parametrize('harness', ['stdio', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('tool_family', ['skills', 'skills-missing', 'automations', 'memory-schema', 'memory-scope'])
def test_validation_remains_a_tool_error_through_broker_and_mcp(workspace, harness, tool_family):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    if tool_family.startswith('skills'):
        assert call(client, run, **form(files=[{'path': 'references/check.md', 'content': 'Read the evidence.'}])).json()['saved']
    capability = 'private-capability'
    app.state.store.update_run(run['id'], token_hash=digest(capability))

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_GET(self):
            response = client.get('/broker/' + run['id'] + self.path,
                headers={'Authorization': self.headers['Authorization']})
            self.send_response(response.status_code)
            self.end_headers()
            self.wfile.write(response.content)

        def do_POST(self):
            assert self.path == '/tools/call'
            response = client.post('/broker/' + run['id'] + self.path,
                content=self.rfile.read(int(self.headers['Content-Length'])),
                headers={key: self.headers[key] for key in ('Authorization', 'Content-Type')})
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    messages = [{'jsonrpc': '2.0', 'id': index, 'method': 'tools/call', 'params': {
        'name': 'skills_read_file', 'arguments': {'name': 'personal:team-review', 'path': path}}}
        for index, path in enumerate(['/private-input-marker/secret.md', 'references/check.md'], 1)]
    if tool_family == 'skills-missing':
        messages[0]['params']['arguments']['path'] = 'references/missing.md'
    if tool_family == 'automations':
        from test_automation_tools import definition
        messages = [{'jsonrpc': '2.0', 'id': index, 'method': 'tools/call', 'params': {
            'name': 'automation_create', 'arguments': {'turn_id': run['active_message_id'],
                'request_key': 'correct-automation-fields', 'definition': definition(**changes)}}}
            for index, changes in enumerate([{'metadata': {'bootstrap_source': 'private-input-marker' + 'x' * (16385 - len('private-input-marker'))}}, {}], 1)]
    elif tool_family.startswith('memory-'):
        quote = 'Keep all future explanations concise.'
        app.state.store.execute('UPDATE messages SET content=? WHERE id=?', (quote, run['active_message_id']))
        invalid = ({'key': 'private-input-marker!'} if tool_family == 'memory-schema'
                   else {'repo_url': 'https://github.com/private-input-marker/other'})
        messages = [{'jsonrpc': '2.0', 'id': index, 'method': 'tools/call', 'params': {
            'name': 'memory_save', 'arguments': {'turn_id': run['active_message_id'],
                'key': 'concise-explanations', 'title': 'Concise explanations', 'content': quote,
                'request_id': 'correct-memory-fields', 'source_message_id': run['active_message_id'],
                'source_quote': quote, **changes}}} for index, changes in enumerate([invalid, {}], 1)]
    script = Path(__file__).resolve().parents[1] / 'sandbox' / 'mcp_bridge.py'
    with diagnostic_relay(Edge) as (relay, relay_client, diagnostics):
        saved = None
        for existing_failure in (False, True):
            if existing_failure:
                app.state.store.update_run(run['id'], token_hash='')
                assert relay_client.post('/tools/call', json=messages[0]['params']).status_code == 401
                saved = relay.last_failure
                assert relay.last_error and relay.uncertain_tool and len(diagnostics) == 1
                app.state.store.update_run(run['id'], token_hash=digest(capability))
            env = {'PATH': os.environ['PATH'], 'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}
            if harness == 'stdio':
                result = subprocess.run([sys.executable, str(script)],
                    input='\n'.join(json.dumps(message) for message in messages) + '\n',
                    text=True, capture_output=True, timeout=10, env=env)
                assert result.returncode == 0, result.stderr
                assert 'private-input-marker' not in result.stdout + result.stderr
                rejected, recovered = [json.loads(line)['result'] for line in result.stdout.splitlines()]
            else:
                from sandbox.harness_bindings import RUNTIME_BINDINGS
                config = {'mcp_servers': {'workspace': {'command': sys.executable, 'args': [str(script)], 'env': env}}}
                workspace_call = RUNTIME_BINDINGS[harness].tools(str(app.state.settings.data_dir), config)[1]

                async def invoke() -> list[object]:
                    return [json.loads(await workspace_call(message['params']['name'],
                        json.dumps(message['params']['arguments']))) for message in messages]

                rejected, recovered = asyncio.run(invoke())
            assert relay.last_failure == saved
            assert bool(relay.last_error) == relay.uncertain_tool == existing_failure
            assert len(diagnostics) == int(existing_failure)
            assert rejected['isError']
            assert not recovered['isError']
            if tool_family == 'skills':
                assert 'Invalid skill arguments' in rejected['content'][0]['text']
                assert json.loads(recovered['content'][0]['text'])['loaded']
            elif tool_family == 'skills-missing':
                failure = json.loads(rejected['content'][0]['text'])
                assert failure['status_code'] == 404 and failure['error']
                assert json.loads(recovered['content'][0]['text'])['loaded']
            elif tool_family == 'automations':
                failure = json.loads(rejected['content'][0]['text'])
                assert failure['status_code'] == 422
                assert failure['validation_errors'][0]['field'] == 'definition.metadata.bootstrap_source'
                assert json.loads(recovered['content'][0]['text'])['status'] == 'paused'
                assert len(app.state.store.rows('SELECT * FROM automation_operations')) == 1
            else:
                failure = json.loads(rejected['content'][0]['text'])
                assert failure['status_code'] == 422 and failure['error']
                assert json.loads(recovered['content'][0]['text'])['saved'] is True
                notes = app.state.memory.listing('google:alice')
                assert len(notes) == 1 and notes[0]['repo_url'] == '' and notes[0]['revision'] == 1
            assert 'private-input-marker' not in json.dumps([rejected, recovered])


@pytest.mark.parametrize('arguments', ['{private-input-marker', '[]', 'null', '"private-input-marker"', '1', 'true'])
def test_workspace_call_rejects_nonobject_arguments_without_starting_mcp(arguments: str) -> None:
    from sandbox.harness_tools import tools_for
    # No server configuration: rejected arguments must never attempt a call.
    result = json.loads(asyncio.run(tools_for('/workspace', {})[1]('fixture', arguments)))
    assert result['isError']
    assert result['content'][0]['text'] == 'arguments_json must be a valid JSON object. No tool was called.'
    assert 'private-input-marker' not in json.dumps(result)


@pytest.mark.parametrize('tool_index', [0, 1])
def test_workspace_call_preserves_mcp_transport_failure(tool_index: int) -> None:
    from sandbox.harness_tools import tools_for
    # Initialize successfully, then close the MCP transport during discovery.
    server = """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'tools/list': break
    if request['method'] == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2025-03-26',
            'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}}), flush=True)
"""
    config = {'mcp_servers': {'workspace': {'command': sys.executable, 'args': ['-c', server]}}}
    with pytest.raises(ExceptionGroup):
        tool = tools_for('/workspace', config)[tool_index]
        asyncio.run(tool(*(['fixture', '{}'] if tool_index else [])))


@pytest.mark.parametrize('harness', ['deepagents', 'tool-loop'])
@pytest.mark.parametrize('status', [401, 503])
@pytest.mark.parametrize('tool_index', [0, 1])
def test_workspace_discovery_failure_precedes_actions_and_can_recover(
        monkeypatch: pytest.MonkeyPatch, harness: str, status: int, tool_index: int) -> None:
    from sandbox.harness_bindings import RUNTIME_BINDINGS
    from sandbox.startup import read_with_reconnect
    # Exercise real retry exhaustion without spending the production 45 seconds.
    monkeypatch.setattr('sandbox.broker_relay.read_with_reconnect', partial(read_with_reconnect, budget=0))
    requests = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(('GET', self.path))
            self.send_response(status if len(requests) == 1 else 200)
            self.end_headers()
            self.wfile.write(b'private-provider-body' if len(requests) == 1 else json.dumps([
                {'name': 'fixture', 'description': 'Fixture', 'inputSchema': {'type': 'object'}}]).encode())
        def do_POST(self):
            requests.append(('POST', self.path))
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"saved":true}')
    script = Path(__file__).resolve().parents[1] / 'sandbox' / 'mcp_bridge.py'
    with diagnostic_relay(Edge) as (relay, _, diagnostics):
        config = {'mcp_servers': {'workspace': {'command': sys.executable, 'args': [str(script)],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': 'private-capability'}}}}
        tool = RUNTIME_BINDINGS[harness].tools('/workspace', config)[tool_index]
        args = ['fixture', '{}'] if tool_index else []
        rejected = json.loads(asyncio.run(tool(*args)))
        assert rejected['isError'] and 'No tool action was attempted' in rejected['content'][0]['text']
        assert 'private' not in json.dumps(rejected) and 'tools' not in rejected
        assert requests == [('GET', '/tools')]
        recovered = json.loads(asyncio.run(tool(*args)))
        if tool_index:
            assert not recovered['isError'] and json.loads(recovered['content'][0]['text'])['saved']
        else:
            assert 'fixture' in {tool['name'] for tool in recovered['tools']}
        assert requests == [('GET', '/tools'), ('GET', '/tools')] + ([('POST', '/tools/call')] if tool_index else [])
        assert len(diagnostics) == int(status == 401)
        if tool_index:
            unknown = json.loads(asyncio.run(tool('unadvertised', '{}')))
            assert unknown['isError'] and 'not in the authorized catalog' in unknown['content'][0]['text']
            assert requests[-1] == ('GET', '/tools') and len(requests) == 4
