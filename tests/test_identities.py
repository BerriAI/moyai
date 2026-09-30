import asyncio
import sqlite3
import time
from threading import Event
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.main import create_app
from test_slack import event, signed, slack_app, wait_for
from test_spend import sign_in


TEAM, USER = 'T12345678', 'U12345678'
SCOPES = 'users:read,users:read.email,reactions:write'


def google(store, sub='alice', email='alice@berri.ai'):
    return store.identity({'method': 'google', 'identity': {'sub': sub, 'email': email, 'name': sub.title()}})


def sender(store, user=USER, team=TEAM, referenced=True):
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, team, user)
    if referenced:
        run = store.create_run('Identity test', '', 'modal', [], chat_enabled=True, user_id=actor)
        store.claim_message(run['id'])
    return actor


def account(app, actor=f'slack:{TEAM}:{USER}'):
    return app.state.store.rows('SELECT * FROM users WHERE id=?', (actor,))[0]


def install(app, scopes=SCOPES):
    app.state.connectors.save('slack', {'access_token': 'unused-user-token', 'kind': 'oauth',
        'bot': {'access_token': 'profile-bot-token', 'scope': scopes, 'team': {'id': TEAM}, 'bot_user_id': 'U99999999'}}, 'Test team')


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, public_url='https://workspace.example',
        workspace_password='admin-test-password', slack_bot_enabled=True, google_allowed_domains='berri.ai'))
    install(app)
    control = {'calls': [], 'profile': {'id': USER, 'team_id': TEAM,
               'profile': {'email': ' Alice@BERRI.AI ', 'real_name': 'Alice'}}}

    async def request(method, url, **kwargs):
        assert method == 'GET' and url == 'https://slack.com/api/users.info'
        assert kwargs['headers']['Authorization'] == 'Bearer profile-bot-token'
        control['calls'].append(kwargs['params']['user'])
        if control.get('fail'):
            raise RuntimeError('Private provider response must not escape')
        return {'ok': True, 'user': control['profile']}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    return app, control


def sync(app):
    asyncio.run(app.state.identities.sync_due())


def refresh(app):
    app.state.store.execute('UPDATE users SET profile_next_check=0')
    sync(app)


@pytest.mark.parametrize('google_first', [False, True])
def test_first_use_provisions_profile_and_google_match_combines_past_costs(profiles, google_first):
    app, control = profiles
    store = app.state.store
    if google_first:
        google(store)
    actor = sender(store)
    run = store.rows('SELECT id FROM runs')[0]
    rid = app.state.spend.begin(store.run(run['id']), 'test-model')
    store.execute("UPDATE model_requests SET cost='0.5',status='completed' WHERE id=?", (rid,))
    sync(app)
    assert account(app)['email'] == 'alice@berri.ai'
    assert account(app)['name'] == 'Alice'
    if not google_first:
        assert account(app)['link_status'] == 'awaiting_google'
        assert not store.rows("SELECT id FROM users WHERE kind='google'")
        assert app.state.spend.report()['users'][0]['email'] == 'alice@berri.ai'
        google(store)
    assert account(app)['linked_user_id'] == 'google:alice'
    assert account(app)['link_method'] == 'email'
    assert account(app)['link_status'] == 'linked'
    report = app.state.spend.report()
    assert report['users'][0]['id'] == 'google:alice'
    assert report['users'][0]['spend'] == report['total']['spend'] == '0.5'
    assert store.run(run['id'])['owner_id'] == actor
    assert store.messages(run['id'])[0]['user_id'] == actor
    assert store.rows('SELECT user_id FROM model_requests')[0]['user_id'] == actor
    google(store)
    refresh(app)
    audit = store.rows('SELECT * FROM identity_audit')
    assert len(audit) == 1 and audit[0]['actor_id'] == 'system:email-match'
    assert audit[0]['reason'] == 'automatic_email_match'


@pytest.mark.parametrize('email', ['a.lice@berri.ai', 'alice+test@berri.ai'])
def test_aliases_and_dots_do_not_merge(profiles, email):
    app, _ = profiles
    sender(app.state.store)
    google(app.state.store, email=email)
    sync(app)
    assert account(app)['linked_user_id'] is None


@pytest.mark.parametrize('field,value', [
    ('id', 'U87654321'), ('team_id', 'T87654321'), ('deleted', True), ('is_bot', True),
    ('is_app_user', True), ('is_restricted', True), ('is_ultra_restricted', True), ('is_stranger', True),
    ('profile', {'email': 'alice@gmail.com'}), ('profile', {'email': 'alice@berri.ai.evil.example'}),
    ('profile', {}), ('profile', {'email': ['alice@berri.ai']}), ('profile', 'malformed'),
])
def test_ineligible_or_mismatched_slack_profile_cannot_link(profiles, field, value):
    app, control = profiles
    sender(app.state.store)
    google(app.state.store)
    control['profile'][field] = value
    sync(app)
    assert not account(app)['profile_eligible']
    assert not account(app)['linked_user_id']
    assert not app.state.store.rows('SELECT * FROM identity_audit')


def test_duplicate_google_or_slack_emails_require_review(profiles):
    app, control = profiles
    store = app.state.store
    sender(store)
    google(store)
    google(store, 'other-sub')
    sync(app)
    assert account(app)['link_status'] == 'review'
    assert not account(app)['linked_user_id']
    google(store, 'other-sub', 'other@berri.ai')
    assert account(app)['linked_user_id'] == 'google:alice'
    second = sender(store, 'U87654321')
    control['profile']['id'] = 'U87654321'
    sync(app)
    assert account(app, second)['link_status'] == 'review'
    assert not account(app, second)['linked_user_id']
    assert account(app)['link_status'] == 'review'
    assert account(app)['linked_user_id'] == 'google:alice'


def test_email_change_never_retargets_and_conflict_survives_api_failure(profiles):
    app, control = profiles
    sender(app.state.store)
    google(app.state.store)
    google(app.state.store, 'bob', 'bob@berri.ai')
    sync(app)
    control['profile']['profile']['email'] = 'bob@berri.ai'
    refresh(app)
    assert account(app)['link_status'] == 'email_changed'
    assert account(app)['linked_user_id'] == 'google:alice'
    control['fail'] = True
    refresh(app)
    control['fail'] = False
    refresh(app)
    assert account(app)['profile_conflict'] == 1
    assert account(app)['link_status'] == 'email_changed'
    assert account(app)['linked_user_id'] == 'google:alice'
    assert len(app.state.store.rows('SELECT * FROM identity_audit')) == 1


def test_google_email_reassignment_preserves_stable_subject(profiles):
    app, _ = profiles
    sender(app.state.store)
    google(app.state.store)
    sync(app)
    google(app.state.store, 'alice', 'renamed@berri.ai')
    google(app.state.store, 'different-human', 'alice@berri.ai')
    refresh(app)
    assert account(app)['linked_user_id'] == 'google:alice'
    assert account(app)['link_status'] == 'review'


def test_profile_must_be_fresh_for_later_google_login(profiles):
    app, _ = profiles
    sender(app.state.store)
    sync(app)
    app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00'")
    google(app.state.store)
    assert account(app)['link_status'] == 'pending_profile'
    assert not account(app)['linked_user_id']
    refresh(app)
    assert account(app)['linked_user_id'] == 'google:alice'


@pytest.mark.parametrize('blocked', ['scopes', 'disabled', 'policy'])
def test_lookup_requires_feature_permission_and_connection(profiles, blocked):
    app, control = profiles
    sender(app.state.store)
    if blocked == 'scopes':
        install(app, 'reactions:write,users:read')
    elif blocked == 'disabled':
        app.state.settings.slack_identity_linking_enabled = False
    else:
        app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    sync(app)
    assert control['calls'] == []
    assert not app.state.identities.status()['ready']


def test_startup_backfills_only_observed_senders_and_retries_failed_lookup(profiles):
    app, control = profiles
    sender(app.state.store)
    sender(app.state.store, user='U87654321', referenced=False)
    sender(app.state.store, user='U11111111', team='T87654321')
    control['fail'] = True
    sync(app)
    assert control['calls'] == [USER]
    assert account(app)['link_status'] == 'unavailable'
    assert account(app)['profile_next_check'] > time.time()
    sync(app)
    assert control['calls'] == [USER]
    control['fail'] = False
    refresh(app)
    assert account(app)['email'] == 'alice@berri.ai'
    assert control['calls'] == [USER, USER]


def test_admin_override_survives_refresh_and_existing_links_migrate(profiles):
    app, _ = profiles
    actor = sender(app.state.store)
    with TestClient(app, base_url=app.state.settings.public_url) as client:
        sign_in(app, client)
        google(app.state.store, 'bob', 'bob@berri.ai')
        response = client.post('/api/admin/spend/link-slack', json={'slack_user_id': actor, 'google_user_id': 'google:bob'})
        assert response.status_code == 200
        assert account(app)['link_method'] == 'manual'
    refresh(app)
    assert account(app)['linked_user_id'] == 'google:bob'
    assert account(app)['link_status'] == 'manual'
    app.state.store.execute("UPDATE users SET link_method='',link_status='' WHERE id=?", (actor,))
    Store(app.state.settings.data_dir)
    assert account(app)['link_method'] == 'manual'
    audit = app.state.store.rows("SELECT * FROM identity_audit WHERE reason='admin_override'")
    assert len(audit) == 1 and audit[0]['actor_id'] == 'google:alice'


def test_admin_routes_require_login_role_and_csrf_but_slack_never_grants_login(profiles):
    app, _ = profiles
    sender(app.state.store)
    sync(app)
    client = TestClient(app, base_url=app.state.settings.public_url)
    assert not client.get('/api/session').json()['authenticated']
    assert client.get('/api/admin/identities/status').status_code == 401
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/identities/status').status_code == 403
    assert client.post('/api/admin/identities/refresh').status_code == 403
    sign_in(app, client)
    assert client.get('/api/admin/identities/status').json()['ready']
    assert client.post('/api/admin/identities/refresh', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post('/api/admin/identities/refresh').json() == {'queued': True}
    install(app, 'reactions:write')
    assert client.post('/api/admin/identities/refresh').status_code == 409


def test_slack_acknowledgement_and_task_creation_do_not_wait_for_profiles(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    install(app)
    release, started = Event(), Event()

    async def request(method, url, **kwargs):
        if url.endswith('users.info'):
            started.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            raise RuntimeError('Profile API unavailable')
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    try:
        assert client.post('/hooks/slack/events', **signed(event())).status_code == 200
        wait_for(started.is_set)
        assert len(runs) == 1
        assert len(app.state.store.messages(runs[0]['id'])) == 1
    finally:
        release.set()
    wait_for(lambda: account(app)['link_status'] == 'unavailable')
    assert len(runs) == 1


def test_oauth_requests_profile_scopes_only_when_enabled(profiles):
    app, _ = profiles
    scopes = parse_qs(urlparse(app.state.connectors.authorization_url('slack', 'test-state')).query)['scope'][0].split(',')
    assert {'users:read', 'users:read.email'} <= set(scopes)
    app.state.settings.slack_identity_linking_enabled = False
    scopes = parse_qs(urlparse(app.state.connectors.authorization_url('slack', 'test-state')).query)['scope'][0].split(',')
    assert 'users:read' not in scopes and 'users:read.email' not in scopes


def test_existing_database_gains_columns_and_retains_admin_link(tmp_path):
    with sqlite3.connect(tmp_path / 'workspace.db') as conn:
        conn.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY,kind TEXT NOT NULL,email TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,linked_user_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            INSERT INTO users VALUES('google:alice','google','alice@berri.ai','Alice',NULL,'old','old');
            INSERT INTO users VALUES('slack:T12345678:U12345678','slack','','Slack user','google:alice','old','old');
            CREATE TABLE identity_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id TEXT NOT NULL,
                source_id TEXT NOT NULL,target_id TEXT NOT NULL,created_at TEXT NOT NULL);
            INSERT INTO identity_audit(actor_id,source_id,target_id,created_at)
                VALUES('admin','slack:T12345678:U12345678','google:alice','old');
        """)
    store = Store(tmp_path, auto_link_identities=True)
    user = store.rows("SELECT * FROM users WHERE kind='slack'")[0]
    assert user['linked_user_id'] == 'google:alice'
    assert user['link_method'] == user['link_status'] == 'manual'
    assert user['profile_next_check'] == 0
    assert store.rows('SELECT * FROM identity_audit')[0]['actor_id'] == 'admin'


def test_malformed_sender_does_not_starve_backfill(profiles):
    app, control = profiles
    actor = sender(app.state.store, user='invalid')
    sync(app)
    assert not control['calls']
    assert account(app, actor)['profile_next_check'] > time.time()
