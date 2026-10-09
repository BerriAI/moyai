"""Native Codex search + MCP execution through Moyai's real relay and broker."""
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx

from sandbox.codex_harness import CodexAgent
from sandbox.tool_guidance import tool_guidance
from test_codex_sdk_transport import background_gateway, send_response
from test_workspace import workspace as broker_workspace  # noqa: F401


def request_tools(body):
    return [*body.get('tools', []), *[tool for item in body['input']
        if isinstance(item, dict) and item.get('type') == 'additional_tools'
        for tool in item['tools']]]


def search_case(tmp_path, monkeypatch, broker_workspace, progress=print):
    events, proof = [], {}

    def upstream(body, state):
        sequence = len(state.requests)
        if sequence == 1:
            tools = request_tools(body)
            search = next(tool for tool in tools if tool['type'] == 'tool_search')
            assert search['execution'] == 'client'
            functions = next(tool for tool in tools if tool.get('name') == 'functions')
            assert any(tool['name'] == 'exec' for tool in functions['tools'])
            assert not any(tool.get('name') == 'mcp__moyai' for tool in tools)
            proof['native_search_available'] = True
            proof['code_execution_available'] = True
            proof['mcp_schemas_deferred'] = True
            progress('Codex advertises native tool_search; Moyai MCP schemas are deferred.')
            output = {'type': 'tool_search_call', 'id': 'search_item', 'call_id': 'search_call',
                      'execution': 'client', 'status': 'completed',
                      'arguments': {'query': 'workspace_diagnostics', 'limit': 1}}
        elif sequence == 2:
            result = next(item for item in body['input'] if item.get('type') == 'tool_search_output')
            assert result['call_id'] == 'search_call'
            namespace = next(tool for tool in result['tools'] if tool.get('name') == 'mcp__moyai')
            assert [tool['name'] for tool in namespace['tools']] == ['workspace_diagnostics']
            assert namespace['tools'][0]['parameters']['type'] == 'object'
            proof['discovered_tool'] = 'workspace_diagnostics'
            progress('Native search returned the workspace_diagnostics argument schema.')
            output = {'type': 'function_call', 'id': 'diagnostic_item', 'call_id': 'diagnostic_call',
                      'namespace': namespace['name'], 'name': 'workspace_diagnostics', 'arguments': '{}'}
        elif sequence == 3:
            result = next(item for item in body['input']
                if item.get('type') == 'function_call_output' and item.get('call_id') == 'diagnostic_call')
            assert 'broker_catalog' in json.dumps(result) and 'mcp_catalog' in json.dumps(result)
            proof['mcp_call_completed'] = True
            progress('Discovered tool executed through the real MCP bridge, relay, and session broker.')
            output = {'type': 'tool_search_call', 'id': 'restricted_item', 'call_id': 'restricted_call',
                      'execution': 'client', 'status': 'completed',
                      'arguments': {'query': 'github_repositories', 'limit': 1}}
        elif sequence == 4:
            result = next(item for item in body['input']
                if item.get('type') == 'tool_search_output' and item.get('call_id') == 'restricted_call')
            assert 'github_repositories' not in json.dumps(result['tools'])
            proof['unselected_connection_hidden'] = True
            progress('Search cannot reveal GitHub tools outside this session’s connection scope.')
            output = {'type': 'message', 'id': 'final', 'role': 'assistant', 'phase': 'final_answer',
                      'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Native search verified.'}]}
        else:
            raise AssertionError('Unexpected model request')
        pipe = SimpleNamespace(wfile=BytesIO(), send_response=lambda *a: None,
                               send_header=lambda *a: None, end_headers=lambda: None)
        send_response(pipe, output, sequence)
        return httpx.Response(200, content=pipe.wfile.getvalue(), headers={'Content-Type': 'text/event-stream'})

    with background_gateway(tmp_path, monkeypatch, broker_workspace, 'codex', upstream, progress) as state:
        agent = CodexAgent(spec={'model': 'openai/gpt-6-astra', 'timeout': 30, 'max_iterations': 5},
            relay=state.relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
                'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': state.relay.url, 'WORKSPACE_RUN_TOKEN': state.capability}}}},
            activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
                complete=lambda *args: events.append(('complete', args)), commentary=progress),
            step=lambda: None, cwd=str(tmp_path), definition=None)
        try:
            result = agent.run_conversation('Discover and call workspace diagnostics.',
                conversation_history=[], system_message=tool_guidance('codex'))
        finally:
            agent.close()
        assert result['completed'], result
        assert len(state.requests) == 4
        proof['completed'] = True
        progress('PASS: native search → schema → real MCP execution, with session scope enforced.')
        return proof, events, result


def test_native_search_discovers_and_executes_scoped_mcp_tool(tmp_path, monkeypatch, broker_workspace):
    proof, events, result = search_case(tmp_path, monkeypatch, broker_workspace)
    assert all(proof.values())
    assert any(kind == 'complete' and args[1] == 'mcp__moyai__workspace_diagnostics'
               for kind, args in events)
    searches = [args for kind, args in events if kind == 'complete' and args[1] == 'tool_search']
    assert len(searches) == 2 and all(not args[3]['isError'] for args in searches)
    assert 'workspace_diagnostics' in searches[0][3]['content'][0]['text']
