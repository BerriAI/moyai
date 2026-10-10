"""Confirmed publication remains visible when later agent work fails."""
import asyncio
import pytest

from app.github import Publish
from app.connector_errors import ConnectorError
from app.security import digest
from app.activity_history import projection
from test_github import PAYLOAD, GitHubAPI, connected
from test_workspace import workspace


def test_publication_bypasses_spent_update_budget_and_survives_failure(workspace, monkeypatch):
    app, _ = workspace
    connected(app)
    store, github = app.state.store, app.state.connectors.github
    run = store.create_run('Create the fix PR', '', 'modal', ['github'], chat_enabled=True)
    turn = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('test-capability'))
    store.event(run['id'], 'message', 'Opening update')
    store.event(run['id'], 'message', 'Delegating to scouts')
    store.event(run['id'], 'message', 'Suppressed third update')
    provider = GitHubAPI(github, monkeypatch)
    receipt = asyncio.run(github.publish(store.run(run['id']), Publish.model_validate(PAYLOAD)))
    for _ in range(2):
        assert asyncio.run(github.publish(store.run(run['id']), Publish.model_validate(PAYLOAD))) == receipt
    updates = [event for event in store.events(run['id']) if event['kind'] == 'message']
    assert len(updates) == 3
    milestone = updates[-1]
    assert milestone['data']['phase'] == 'pr_created' and milestone['data']['public_update']
    assert milestone['data']['turn_id'] == turn['id'] and receipt['url'] in milestone['message']
    assert 'not complete' in milestone['message']
    store.finish_message(run['id'], turn['id'], 'Codex stopped (TypeError).', status='failed')
    store.update_run(run['id'], status='failed')
    with store.connect() as conn:
        result = projection(conn, store.run(run['id']), store.messages(run['id']))
    assert any(event['id'] == milestone['id'] for event in result['events'])
    assert len([call for call in provider.calls if call[0] == 'POST' and call[1].endswith('/pulls')]) == 1


def test_uncertain_creation_does_not_publish_success(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    provider = GitHubAPI(github, monkeypatch)
    provider.lose = 'branch'
    with pytest.raises(ConnectorError, match='Lost response'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    assert not any(event['data'].get('phase') == 'pr_created' for event in app.state.store.events(run_id))
