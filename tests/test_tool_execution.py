"""New connector tools inherit direct execution and existing access policies."""
import pytest

from app.connectors import Args, TOOLS
from test_workspace import cloud_capability, workspace


@pytest.mark.parametrize('restriction', [None, 'read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_new_write_tool_needs_no_approval_exception(workspace, monkeypatch, restriction):
    app, client = workspace
    name = 'slack_new_write_tool'
    monkeypatch.setitem(TOOLS, name, ('slack', True, Args, 'A newly connected write tool.'))
    run_id, headers = cloud_capability(app, ['slack'])
    calls = []

    async def call(tool, arguments, *, run):
        assert run['id'] == run_id
        calls.append((tool, arguments))
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'call', call)
    if restriction in {'read_only', 'disabled'}:
        assert client.patch('/api/connections/slack/policy', json={
            'enabled': restriction != 'disabled', 'read_only': restriction == 'read_only',
        }).status_code == 200
    elif restriction == 'disconnect':
        assert client.delete('/api/connections/slack').status_code == 200
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    elif restriction == 'revoked':
        app.state.store.update_run(run_id, token_hash='')

    discovered = client.get(f'/broker/{run_id}/tools', headers=headers)
    if not restriction:
        tool = next(t for t in discovered.json() if t['name'] == name)
        assert tool['annotations']['readOnlyHint'] is False
    elif discovered.status_code == 200:
        assert name not in {t['name'] for t in discovered.json()}
    else:
        assert discovered.status_code == 401

    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': name, 'arguments': {}})
    if restriction:
        assert response.status_code in {401, 403}
        assert not calls
    else:
        assert response.status_code == 200 and response.json() == {'ok': True}
        assert calls == [(name, {})]
        assert app.state.store.run(run_id)['status'] == 'running'
        assert any(e['message'] == f'{name} completed' for e in app.state.store.events(run_id))
    assert not app.state.store.approvals(run_id)
    assert not any(e['kind'] == 'approval' for e in app.state.store.events(run_id))


def test_all_connector_tools_advertise_direct_execution(workspace):
    app, client = workspace
    connections = client.get('/api/connections').json()
    tools = [tool for connection in connections for tool in connection['tools']]
    assert {tool['name'] for tool in tools} == set(TOOLS)
    assert all(tool['requires_approval'] is False for tool in tools)
    for tool in tools:
        assert tool['write'] == TOOLS[tool['name']][1]
