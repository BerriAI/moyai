from app.db import Store
from app.model_preferences import preferred_model, save_model
from test_workspace import workspace
from test_models import ASTRA, OPUS, GLM
from test_model_tools import call
from test_spend import active
from test_slack import slack_app, event, signed


def test_picker_preference_persists_and_defaults_new_sessions(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    before = client.post('/api/runs', json={'prompt': 'Existing session'}).json()
    response = client.put('/api/settings/model-preference', json={'model': 'opus'})
    assert response.status_code == 200
    assert client.get('/api/config').json()['model'] == OPUS
    new = client.post('/api/runs', json={'prompt': 'New session'}).json()
    assert new['model'] == OPUS
    assert app.state.store.run(before['id'])['model'] == before['model']
    assert client.post('/api/runs', json={'prompt': 'Explicit choice', 'model': GLM}).json()['model'] == GLM
    reopened = Store(app.state.settings.data_dir, default_model=ASTRA)
    with reopened.connect() as conn:
        assert preferred_model(conn, app.state.settings, new['owner_id']) == OPUS
    assert client.put('/api/settings/model-preference', json={'model': 'unknown'}).status_code == 422
    assert client.put('/api/settings/model-preference', json={'model': GLM, 'user_id': 'other'}).status_code == 422
    assert client.get('/api/config').json()['model'] == OPUS


def test_person_isolation_linked_slack_and_removed_model(workspace):
    app, _ = workspace
    store, settings = app.state.store, app.state.settings
    alice = store.identity({'method': 'google', 'identity': {'sub': 'alice', 'email': 'alice@example.com'}})
    bob = store.identity({'method': 'google', 'identity': {'sub': 'bob', 'email': 'bob@example.com'}})
    with store.connect() as conn:
        slack = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute('UPDATE users SET linked_user_id=? WHERE id=?', (alice, slack))
        save_model(conn, slack, OPUS)
        assert preferred_model(conn, settings, alice) == OPUS
        assert preferred_model(conn, settings, bob) == settings.resolve_model()
        save_model(conn, alice, GLM)
        assert preferred_model(conn, settings, slack) == GLM
        save_model(conn, alice, 'removed-model')
        assert preferred_model(conn, settings, alice) == settings.resolve_model()


def test_switch_uses_active_author_and_retry_does_not_overwrite(workspace):
    app, client = workspace
    app.state.store.identity({'method': 'google', 'identity': {'sub': 'alice', 'email': 'alice@example.com'}})
    bob = app.state.store.identity({'method': 'google', 'identity': {'sub': 'bob', 'email': 'bob@example.com'}})
    run = active(app, 'google:alice')
    app.state.store.execute('UPDATE runs SET owner_id=? WHERE id=?', (bob, run['id']))
    assert call(client, run).status_code == 200
    with app.state.store.connect() as conn:
        assert preferred_model(conn, app.state.settings, bob) == app.state.settings.resolve_model()
        assert preferred_model(conn, app.state.settings, 'google:alice') == GLM
        save_model(conn, 'google:alice', OPUS)
    assert call(client, run).json()['replayed']
    with app.state.store.connect() as conn:
        assert preferred_model(conn, app.state.settings, 'google:alice') == OPUS


def test_new_slack_thread_inherits_saved_model(slack_app):
    app, client, submitted, _ = slack_app
    first = event(text='<@U99999999> model opus\nFirst task')
    assert client.post('/hooks/slack/events', **signed(first)).status_code == 200
    assert submitted[-1]['model'] == OPUS
    second = event(text='<@U99999999> Another task')
    second['event_id'] = 'EvDifferentThread'
    second['event']['ts'] = '1700000999.000001'
    second['event'].pop('thread_ts', None)
    assert client.post('/hooks/slack/events', **signed(second)).status_code == 200
    assert len(submitted) == 2
    assert submitted[-1]['model'] == OPUS
