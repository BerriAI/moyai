import io
import json

import pytest

from sandbox import mcp_bridge
from sandbox.tool_guidance import tool_guidance
from app.security import digest
from test_workspace import cloud_capability, workspace  # noqa: F401


def test_diagnostics_explains_registration_and_policy_without_raw_logs(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['github'])
    app.state.store.execute('UPDATE runs SET harness=? WHERE id=?', ('codex', run_id))
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'a' * 40)
    app.state.store.event(run_id, 'error', 'SECRET from raw stderr', {
        'phase': 'broker_failure', 'route': '/tools', 'http_status': 503,
        'request_id': 'known-request-id', 'response_started': False,
        'headers': {'Authorization': 'Bearer SECRET'}, 'error_type': 'SECRET',
        'response_body': 'SECRET', 'provider_key': 'SECRET'})
    app.state.store.event(run_id, 'tool', 'SECRET tool output', {'phase': 'completed', 'output': 'SECRET'})
    other, _ = cloud_capability(app, ['github'])
    app.state.store.update_run(other, token_hash=digest('other-capability'))
    app.state.store.event(other, 'error', 'Other session', {'phase': 'broker_failure', 'request_id': 'other-request'})
    endpoint = f'/broker/{run_id}/tools/call'
    body = {'name': 'workspace_diagnostics', 'arguments': {}}
    catalog = client.get(f'/broker/{run_id}/tools', headers=headers).json()
    assert next(tool for tool in catalog if tool['name'] == 'workspace_diagnostics')['annotations']['readOnlyHint']
    response = client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200
    data = response.json()
    assert data['harness'] == 'codex'
    assert data['source']['deployed_revision'] == 'a' * 40
    assert data['broker_catalog']['names'] == sorted(tool['name'] for tool in catalog)
    github = next(item for item in data['connections'] if item['provider'] == 'github')
    assert github['connected'] and github['selected_for_session'] and github['enabled']
    assert 'github_repositories' in github['available_tools']
    assert data['recent_failures'][0]['http_status'] == 503
    assert data['recent_failures'][0]['request_id'] == 'known-request-id'
    assert 'SECRET' not in response.text and 'other-request' not in response.text
    assert 'provider-test-token' not in response.text
    assert client.post(endpoint, headers=headers, json={**body, 'arguments': {'run_id': other}}).status_code == 422
    assert client.post(endpoint, headers={**headers, 'Authorization': 'Bearer wrong'}, json=body).status_code == 401
    assert client.post(endpoint, headers={'Authorization': 'Bearer other-capability'}, json=body).status_code == 401
    assert client.patch('/api/connections/github/policy', json={'enabled': False, 'read_only': False}).status_code == 200
    paused = client.post(endpoint, headers=headers, json=body).json()
    github = next(item for item in paused['connections'] if item['provider'] == 'github')
    assert github['connected'] and not github['enabled'] and not github['available_tools']
    app.state.store.update_run(run_id, token_hash='')
    assert client.post(endpoint, headers=headers, json=body).status_code == 401


@pytest.mark.parametrize('list_first', [True, False])
def test_mcp_diagnostics_distinguishes_last_catalog_from_current_broker(monkeypatch, capsys, list_first):
    old = {'name': 'old_tool', 'inputSchema': {'type': 'object'}, 'description': 'Old catalog'}
    requests = ([{'id': 1, 'method': 'tools/list'}] if list_first else []) + [
        {'id': 2, 'method': 'tools/call', 'params': {'name': 'workspace_diagnostics', 'arguments': {}}}]
    monkeypatch.setattr('sys.stdin', io.StringIO('\n'.join(json.dumps(row) for row in requests)))
    monkeypatch.setattr(mcp_bridge, 'broker', lambda path, body=None:
        [old] if path == '/tools' else {'broker_catalog': {'count': 1, 'names': ['new_tool']}})
    mcp_bridge.serve()
    reply = json.loads(capsys.readouterr().out.splitlines()[-1])['result']
    data = json.loads(reply['content'][0]['text'])
    assert data['broker_catalog']['names'] == ['new_tool']
    assert data['mcp_catalog']['names'] == (sorted(['old_tool', *[tool['name'] for tool in mcp_bridge.BROWSER_TOOLS]]) if list_first else None)
    assert 'not the model request catalog' in data['mcp_catalog']['scope']


@pytest.mark.parametrize('harness', ['hermes', 'codex', 'claude-agent-sdk', 'deepagents', 'tool-loop', 'opencode', 'pi'])
def test_discovery_instructions_match_runtime(harness):
    instructions = tool_guidance(harness)
    assert ('tool_search' in instructions) is (harness == 'hermes')
    assert ('ToolSearch' in instructions) is (harness == 'claude-agent-sdk')
    assert ('ALL_TOOLS' in instructions) is (harness == 'codex')
    assert 'workspace_diagnostics' in instructions and 'github_checkout' in instructions
