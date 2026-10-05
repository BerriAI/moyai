"""Direct ticket creation still requires a live, writable Linear connection."""
import pytest

from app.connector_errors import ConnectorError
from app.connectors import TOOLS
from test_workspace import workspace, cloud_capability

ARGS = {'team_id': '12345678-1234-1234-1234-123456789abc',
        'title': 'Create a requested ticket', 'description': 'Acceptance criteria and source context.'}


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_linear_creation_respects_connection_and_session_boundaries(workspace, monkeypatch, restriction):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    if restriction in {'read_only', 'disabled'}:
        app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                ('linear', restriction != 'disabled', restriction == 'read_only'))
    elif restriction == 'disconnect':
        app.state.store.execute("DELETE FROM connections WHERE provider='linear'")
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    else:
        app.state.store.update_run(run_id, token_hash='')
    async def forbidden(*args, **kwargs):
        pytest.fail('A forbidden ticket creation reached Linear')
    monkeypatch.setattr(app.state.connectors, 'request', forbidden)
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_create_issue', 'arguments': ARGS})
    assert response.status_code in {401, 403}
    assert not app.state.store.approvals(run_id)


@pytest.mark.parametrize('failure', ['permission', 'unconfirmed', 'lost_response'])
def test_failed_creation_is_uncertain_without_retries_or_approval(workspace, monkeypatch, failure):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    calls = []
    async def request(*args, **kwargs):
        calls.append(kwargs['json'])
        if failure == 'permission':
            raise ConnectorError('Linear credential lacks Create issues permission.')
        if failure == 'lost_response':
            raise ConnectorError('Linear response was not received.')
        return {'data': {'issueCreate': {'success': False, 'issue': None}}}
    monkeypatch.setattr(app.state.connectors, 'request', request)
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_create_issue', 'arguments': ARGS})
    assert response.status_code == 200
    assert response.json()['error'] and response.json()['outcome_uncertain'] is True
    assert response.json()['instruction'] == 'Verify the destination before retrying a write.'
    assert len(calls) == 1 and not app.state.store.approvals(run_id)
    assert app.state.store.run(run_id)['status'] == 'running'
    assert any(e['kind'] == 'error' for e in app.state.store.events(run_id))


def test_other_connected_app_writes_keep_approval(workspace):
    app, _ = workspace
    direct = {'github_create_pull_request', 'github_update_pull_request', 'github_comment_pull_request', 'linear_create_issue'}
    for name, spec in TOOLS.items():
        assert app.state.connectors.requires_approval(name) == (spec[1] and name not in direct)


def test_invalid_ticket_still_fails_before_provider_call(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    async def forbidden(*args, **kwargs):
        pytest.fail('Invalid ticket reached Linear')
    monkeypatch.setattr(app.state.connectors, 'request', forbidden)
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
        json={'name': 'linear_create_issue', 'arguments': {**ARGS, 'team_id': 'unknown-team'}})
    assert response.status_code == 422 and not app.state.store.approvals(run_id)
