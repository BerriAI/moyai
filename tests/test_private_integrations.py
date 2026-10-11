"""Private sessions stay private across the features added since Slack OAuth."""
import pytest
from fastapi import HTTPException

from app.security import digest
from test_user_roles import users_app, sign_as
from test_slack_notifications import notifications, finish, poll, posts
from test_memory_review import review_app
from test_skill_learning import enable


def active_private(store, owner, *, private=True):
    run = store.create_run('Private integration canary', '', 'modal', [], chat_enabled=True,
                           user_id=owner, private_owner_id=owner if private else '')
    message = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('capability'))
    return store.run(run['id']), message['id']


@pytest.mark.parametrize('caller_owner,private,expected', [
    ('google:maya', False, 404), ('google:alice', False, 404),
    ('google:maya', True, 404), ('google:alice', True, 200),
])
def test_broker_history_requires_same_private_owner_and_destination(users_app, caller_owner, private, expected):
    app, client = users_app
    sign_as(app, client, 'alice@berri.ai')
    sign_as(app, client, 'maya@berri.ai')
    store = app.state.store
    target, _ = active_private(store, 'google:alice')
    caller, _ = active_private(store, caller_owner, private=private)
    response = client.post('/broker/' + caller['id'] + '/tools/call',
        headers={'Authorization': 'Bearer capability'},
        json={'name': 'sessions_read', 'arguments': {'session': target['id']}})
    assert response.status_code == expected, response.text
    assert ('Private integration canary' in response.text) == (expected == 200)


async def test_private_session_never_queues_owner_slack_notification(notifications):
    app, requests = notifications
    run, message = active_private(app.state.store, 'google:alice')
    app.state.store.execute('UPDATE runs SET display_title=? WHERE id=?', ('PRIVATE_TITLE_CANARY', run['id']))
    finish(app, run['id'], message)
    await poll(app)
    assert not posts(requests)
    assert not app.state.store.rows('SELECT * FROM slack_notifications WHERE run_id=?', (run['id'],))


def test_private_settlement_never_becomes_skill_learning_evidence(review_app):
    app = review_app
    enable(app)
    run, message = active_private(app.state.store, 'google:alice')
    app.state.store.finish_message(run['id'], message, 'Private answer canary')
    assert not app.state.store.rows('SELECT * FROM skill_learning_jobs WHERE message_id=?', (message,))


def test_private_swarm_rejected_before_creating_any_sessions(users_app, monkeypatch):
    app, client = users_app
    sign_as(app, client, 'alice@berri.ai')
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/runs', json={'prompt': 'Private swarm canary',
        'private_session': True, 'swarm': {'budget_seconds': 3600}, 'mode': 'modal'})
    assert response.status_code == 422
    assert 'private' in response.text.lower()
    assert not app.state.store.rows('SELECT id FROM runs')


def test_private_feedback_cannot_export_comments(users_app):
    app, client = users_app
    sign_as(app, client, 'alice@berri.ai')
    run, message = active_private(app.state.store, 'google:alice')
    with pytest.raises(HTTPException) as error:
        app.state.lens_feedback.submit(run['id'], message, 'alice', 10, 'Private feedback canary', 'web')
    assert error.value.status_code == 403
    assert not app.state.store.rows('SELECT * FROM lens_feedback')


def test_private_pr_receipts_are_excluded_from_shared_summaries(users_app):
    from test_slack_chat import publication
    app, client = users_app
    sign_as(app, client, 'alice@berri.ai')
    store = app.state.store
    private, _ = active_private(store, 'google:alice')
    shared, _ = active_private(store, 'google:alice', private=False)
    # Model restored publication data, which must never be re-exported.
    publication(app, private['id'], 100, 'Private PR canary')
    publication(app, shared['id'], 101, 'Shared PR')
    receipts = app.state.session_pull_requests.receipts([private['id'], shared['id']])
    assert [receipt.number for _, receipt in receipts] == [101]
    assert app.state.session_pull_requests.receipts([private['id']]) == []


def test_personal_slack_constructor_does_not_attempt_ddl_in_verify_mode():
    from types import SimpleNamespace
    from app.personal_slack import PersonalSlack
    def forbidden(*args, **kwargs):
        pytest.fail('A verified runtime must not attempt schema writes.')
    PersonalSlack(SimpleNamespace(schema_updates=False, execute=forbidden), None, None, None)
