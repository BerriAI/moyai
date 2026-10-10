"""Confirmed publication remains visible when later agent work fails."""
import asyncio
import pytest

from app.github import Publish
from app.connector_errors import ConnectorError
from app.security import digest
from app.activity_history import history_page, projection
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


@pytest.mark.parametrize('source', ['provider_readback', 'saved_receipt'])
def test_later_turn_confirmation_announces_in_recovering_turn_once(workspace, monkeypatch, source):
    app, _ = workspace
    connected(app)
    store, github = app.state.store, app.state.connectors.github
    run = store.create_run('Create the fix PR', '', 'modal', ['github'], chat_enabled=True)
    original_turn = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('test-capability'))
    provider = GitHubAPI(github, monkeypatch)
    args = Publish.model_validate(PAYLOAD)
    if source == 'provider_readback':
        provider.lose = 'pr'

        async def unavailable_readback(method, path, **kwargs):
            if method == 'GET' and path.endswith('/pulls') and provider.pr:
                return []
            return await provider.request(method, path, **kwargs)

        monkeypatch.setattr(github, 'request', unavailable_readback)
        with pytest.raises(ConnectorError, match='Publication unconfirmed'):
            asyncio.run(github.publish(store.run(run['id']), args))
        monkeypatch.setattr(github, 'request', provider.request)
    else:
        asyncio.run(github.publish(store.run(run['id']), args))
        # A receipt saved before milestones were introduced has no announcement.
        store.execute("DELETE FROM events WHERE run_id=? AND json_text(data,'phase')='pr_created'", (run['id'],))
    assert not any(event['data'].get('phase') == 'pr_created' for event in store.events(run['id']))
    store.finish_message(run['id'], original_turn['id'], 'The creation result needs verification.')
    store.enqueue_message(run['id'], 'Check whether the PR was created.', 'recover-publication')
    recovering_turn = store.claim_message(run['id'])
    store.update_run(run['id'], status='running')

    receipt = asyncio.run(github.publish(store.run(run['id']), args))
    milestone, = [event for event in store.events(run['id']) if event['data'].get('phase') == 'pr_created']
    assert milestone['data']['turn_id'] == recovering_turn['id']
    assert receipt['url'] in milestone['message']
    # Publication provenance stays on the original turn; the observation belongs
    # to the turn that confirmed it, including after reloading the conversation.
    assert store.rows('SELECT message_id FROM github_publications')[0]['message_id'] == original_turn['id']
    with store.connect() as conn:
        visible = projection(conn, store.run(run['id']), store.messages(run['id']))
    assert str(original_turn['id']) in visible['deferred_activity']
    assert milestone['id'] in {event['id'] for event in visible['events']}
    assert milestone['id'] in {event['id'] for event in history_page(store, run['id'], recovering_turn['id'])['events']}
    assert milestone['id'] not in {event['id'] for event in history_page(store, run['id'], original_turn['id'])['events']}

    assert asyncio.run(github.publish(store.run(run['id']), args)) == receipt
    store.finish_message(run['id'], recovering_turn['id'], 'Confirmed the PR.')
    store.enqueue_message(run['id'], 'Check the same publication again.', 'repeat-publication')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running')
    assert asyncio.run(github.publish(store.run(run['id']), args)) == receipt
    assert [event for event in store.events(run['id']) if event['data'].get('phase') == 'pr_created'] == [milestone]
    assert len([call for call in provider.calls if call[0] == 'POST' and call[1].endswith('/pulls')]) == 1
