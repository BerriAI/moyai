import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from test_cloudflare_access import ACCESS, access_key, assertion


@pytest.fixture
def login(tmp_path, access_key):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='https://workspace.example',
                        cloudflare_access_login=True, google_allowed_domains='berri.ai',
                        google_admin_emails='tin@berri.ai', **ACCESS)
    app = create_app(settings)
    app.state.cloudflare_access.keys = {'test-access-key': access_key[0].public_key()}
    app.state.cloudflare_access.expires = time.monotonic() + 300
    # A person already has history, preferences and a Google subject. Access
    # has a different subject; those records must keep their original owner.
    app.state.store.identity({'method': 'google', 'identity': {
        'sub': 'original-google-tin', 'email': 'tin@berri.ai', 'name': 'Tin'}})
    with TestClient(app, base_url=settings.public_url) as client:
        client.headers.update({'Origin': settings.public_url,
                               'Cf-Access-Jwt-Assertion': assertion(access_key[0], sub='access-tin', email='tin@berri.ai')})
        yield app, client


def test_access_opens_existing_account_without_google_round_trip(login):
    app, client = login
    store = app.state.store
    owner = 'google:original-google-tin'
    run = store.create_run('My existing history', '', 'demo', [], user_id=owner)
    store.execute('INSERT INTO user_preferences(user_id,send_immediately) VALUES(?,1)', (owner,))
    assert not client.cookies
    response = client.get('/api/session')
    session = response.json()
    assert session['authenticated'] is True and session['role'] == 'admin'
    assert session['user_id'] == owner and session['identity']['email'] == 'tin@berri.ai'
    assert session['preferences']['send_immediately'] is True
    assert all(flag in response.headers['set-cookie'] for flag in ('HttpOnly', 'Secure', 'SameSite=lax'))
    assert store.run(run['id'])['owner_id'] == owner
    assert len(store.rows('SELECT * FROM users')) == 1
    assert not store.rows('SELECT * FROM login_states')
    assert client.get('/api/admin/users').status_code == 200
    assert client.get('/api/session').json()['csrf'] == session['csrf']
    assert not client.get('/api/session').headers.get('set-cookie')


def test_cookie_never_overrides_access_account_and_csrf_rotates(login, access_key):
    app, client = login
    admin = client.get('/api/session').json()
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-bob', email='bob@berri.ai')
    member = client.get('/api/session').json()
    assert member['authenticated'] and member['role'] == 'member'
    assert member['user_id'].startswith('cloudflare:') and member['user_id'] != admin['user_id']
    assert member['csrf'] != admin['csrf']
    assert client.get('/api/admin/users').status_code == 403
    assert client.put('/api/settings/preferences', json={'send_immediately': True},
                      headers={'X-CSRF-Token': admin['csrf']}).status_code == 403
    assert client.put('/api/settings/preferences', json={'send_immediately': True},
                      headers={'X-CSRF-Token': member['csrf']}).status_code == 200
    assert client.put('/api/settings/preferences', json={'send_immediately': False},
                      headers={'X-CSRF-Token': member['csrf'], 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/spend').json()['scope'] == 'personal'
    assert app.state.memory.owner(member['user_id']) == member['user_id']
    assert client.get('/api/session').json()['preferences']['send_immediately']


def test_roles_are_rechecked_and_access_admin_can_manage_them(login):
    app, client = login
    session = client.get('/api/session').json()
    response = client.put('/api/admin/users/role', headers={'X-CSRF-Token': session['csrf']},
                          json={'email': 'other-admin@berri.ai', 'role': 'admin', 'revision': 0})
    assert response.status_code == 200
    response = client.put('/api/admin/users/role', headers={'X-CSRF-Token': session['csrf']},
                          json={'email': 'tin@berri.ai', 'role': 'member', 'revision': 0})
    assert response.status_code == 200
    assert client.get('/api/session').json()['role'] == 'member'
    assert client.get('/api/admin/users').status_code == 403


@pytest.mark.parametrize('changes', [
    {'email': None}, {'email': ['tin@berri.ai']}, {'email': ''}, {'email': ' tin@berri.ai'},
    {'email': 'tin@berri.ai.evil.example'}, {'email': 'tin@gmail.com'},
    {'sub': ''}, {'sub': None}, {'sub': ['human']}, {'common_name': 'machine'}, {'common_name': ''},
    {'aud': ['broker-audience']}, {'exp': 1},
])
def test_non_employee_or_invalid_claims_cannot_bootstrap(login, access_key, changes):
    _, client = login
    claims = {'sub': 'access-tin', 'email': 'tin@berri.ai'} | changes
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], **claims)
    assert client.get('/api/session').status_code == 401
    assert not client.cookies.get('workspace_session')


def test_valid_cookie_still_requires_fresh_verified_assertion(login):
    _, client = login
    assert client.get('/api/session').json()['authenticated']
    del client.headers['Cf-Access-Jwt-Assertion']
    assert client.get('/api/session').status_code == 401
    client.headers['Cf-Access-Authenticated-User-Email'] = 'tin@berri.ai'
    assert client.get('/api/credentials').status_code == 401
    client.headers['Cf-Access-Jwt-Assertion'] = 'forged'
    assert client.get('/api/session').status_code == 401


def test_assertion_refresh_keeps_session_but_revoked_domain_does_not(login, access_key):
    app, client = login
    session = client.get('/api/session').json()
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-tin', email='TIN@BERRI.AI', exp=int(time.time()) + 600)
    assert client.get('/api/session').json()['csrf'] == session['csrf']
    app.state.settings.google_allowed_domains = 'other.example'
    assert client.get('/api/session').status_code == 401


def test_subject_mapping_is_pinned_and_ambiguous_emails_fail_closed(login, access_key):
    app, client = login
    assert client.get('/api/session').json()['user_id'] == 'google:original-google-tin'
    client.cookies.clear()
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='new-person-same-email', email='tin@berri.ai')
    assert client.get('/api/session').status_code == 401
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-tin', email='renamed@berri.ai')
    assert client.get('/api/session').status_code == 401
    for sub in ('google-alice-1', 'google-alice-2'):
        app.state.store.identity({'method': 'google', 'identity': {'sub': sub, 'email': 'alice@berri.ai'}})
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-alice', email='alice@berri.ai')
    assert client.get('/api/session').status_code == 401
    assert len(app.state.store.rows('SELECT * FROM access_identities')) == 1


def test_logout_clears_cookie_and_exits_cloudflare_session(login):
    _, client = login
    session = client.get('/api/session').json()
    response = client.post('/api/logout', headers={'X-CSRF-Token': session['csrf']})
    assert response.status_code == 200
    assert response.json()['logout_url'] == '/cdn-cgi/access/logout'
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert not client.cookies.get('workspace_session')


def test_health_and_webhooks_do_not_establish_employee_sessions(login):
    _, client = login
    del client.headers['Cf-Access-Jwt-Assertion']
    assert client.get('/health').status_code == 200
    assert client.post('/hooks/slack/events', json={}).status_code == 401
    assert not client.cookies.get('workspace_session')


def test_new_access_person_keeps_personal_tools_and_slack_identity_rules(login, access_key):
    from app.db import now
    from test_credentials import save
    from test_skills import create

    app, client = login
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-new', email='new@berri.ai')
    session = client.get('/api/session').json()
    client.headers['X-CSRF-Token'] = session['csrf']
    owner = session['user_id']
    assert owner.startswith('cloudflare:')
    assert create(client).status_code == 201
    assert save(client).status_code == 201
    assert create(client, scope='organization', name='cannot-write-org').status_code == 403
    with app.state.store.connect() as conn:
        slack_id = app.state.store.slack_identity_in(conn, 'TTEST', 'UTEST')
    app.state.store.execute('''UPDATE users SET email='new@berri.ai',profile_eligible=1,
        profile_checked_at=?,profile_conflict=0 WHERE id=?''', (now(), slack_id))
    app.state.store.identity(app.state.security.signer.loads(client.cookies['workspace_session']))
    assert app.state.credentials.same_requester(owner, slack_id)
    assert app.state.store.rows('SELECT linked_user_id FROM users WHERE id=?', (slack_id,))[0]['linked_user_id'] == owner
    from app.model_preferences import preferred_model, save_model
    from test_models import OPUS
    with app.state.store.connect() as conn:
        save_model(conn, slack_id, OPUS)
        assert preferred_model(conn, app.state.settings, owner) == OPUS
    app.state.store.execute('UPDATE users SET profile_conflict=1 WHERE id=?', (slack_id,))
    assert not app.state.credentials.same_requester(owner, slack_id)


def test_legacy_admin_cookie_does_not_override_current_employee(login, access_key):
    app, client = login
    from starlette.responses import Response
    response = Response()
    app.state.security.new_session(response, role='admin')
    client.cookies.set('workspace_session', response.headers['set-cookie'].split(';')[0].split('=', 1)[1])
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], sub='access-member', email='member@berri.ai')
    assert client.get('/api/admin/users').status_code == 403
    assert client.get('/api/session').json()['role'] == 'member'


def test_access_login_requires_complete_policy_configuration():
    for overrides in ({}, ACCESS, ACCESS | {'google_admin_emails': 'person@evil.example'}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, public_url='https://workspace.example', cloudflare_access_login=True, **overrides)


def test_broker_cannot_bootstrap_employee_session_when_login_enabled(login, access_key):
    from app.security import digest
    app, client = login
    run = app.state.store.create_run('Broker boundary', '', 'modal', [], model='test-model')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('test-run-token'))
    path = '/broker/' + run['id'] + '/v1/models'
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0], 'broker-audience', sub='', common_name='test-service')
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer test-run-token'}).status_code == 200
    assert not client.cookies.get('workspace_session')
    assert client.get('/api/session').status_code == 401
    assert not app.state.store.rows('SELECT * FROM access_identities')


def test_mapping_is_rechecked_even_with_valid_session_cookie(login):
    app, client = login
    assert client.get('/api/session').json()['authenticated']
    app.state.store.execute("UPDATE users SET email='reassigned@berri.ai' WHERE id='google:original-google-tin'")
    assert client.get('/api/session').status_code == 401


def test_legacy_google_routes_reuse_access_without_creating_another_identity(login):
    app, client = login
    response = client.post('/api/auth/google/start', json={'return_to': '/#tasks'})
    assert response.json() == {'url': '/#tasks'}
    response = client.get('/auth/google/callback?code=unused&state=unused', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/#tasks'
    assert len(app.state.store.rows('SELECT * FROM users')) == 1
    assert not app.state.store.rows('SELECT * FROM login_states')


def test_access_login_is_not_a_local_preview(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='https://localhost:8000',
                        cloudflare_access_login=True, google_allowed_domains='berri.ai',
                        google_admin_emails='tin@berri.ai', **ACCESS)
    assert not create_app(settings).state.security.local_preview()
