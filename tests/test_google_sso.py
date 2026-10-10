import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.google_sso import KEYS_URL, TOKEN_URL
from app.main import create_app


@pytest.fixture(scope='module')
def google_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='google-test-key', use='sig', alg='RS256')
    return private, jwk


@pytest.fixture
def sso(tmp_path, monkeypatch, google_key):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='https://workspace.example',
                        workspace_password='test-workspace-password', google_client_id='moyai-client',
                        google_client_secret='private-client-secret', google_allowed_domains='berri.ai',
                        google_admin_emails='tin@berri.ai')
    app = create_app(settings)
    control = {'claims': {}, 'exchanges': 0, 'key_fetches': 0}

    def upstream(request):
        if str(request.url) == KEYS_URL:
            control['key_fetches'] += 1
            return httpx.Response(200, json={'keys': [google_key[1]]})
        assert str(request.url) == TOKEN_URL
        body = parse_qs(request.content.decode())
        row = control['transaction']
        assert body['code_verifier'] == [row['verifier']]
        assert body['client_secret'] == [settings.google_client_secret]
        assert body['redirect_uri'] == [settings.public_url + '/auth/google/callback']
        control['exchanges'] += 1
        claims = {'iss': 'https://accounts.google.com', 'aud': settings.google_client_id,
                  'sub': 'google-user-123', 'exp': int(time.time()) + 300, 'iat': int(time.time()),
                  'nonce': row['nonce'], 'email': 'tin@berri.ai', 'email_verified': True,
                  'hd': 'berri.ai', 'name': 'Tin'} | control['claims']
        for key in control.get('omit', []):
            claims.pop(key)
        token = jwt.encode(claims, control.get('key', google_key[0]), algorithm='RS256', headers={'kid': 'google-test-key'})
        return httpx.Response(200, json={'id_token': token, 'access_token': 'not-retained'})

    actual = httpx.AsyncClient
    monkeypatch.setattr('app.google_sso.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    with TestClient(app, base_url=settings.public_url) as client:
        client.headers['Origin'] = settings.public_url
        yield app, client, control


def begin(sso, return_to='/#tasks'):
    app, client, control = sso
    response = client.post('/api/auth/google/start', json={'return_to': return_to})
    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()['url']).query)
    control['transaction'] = app.state.store.rows('SELECT * FROM login_states ORDER BY expires DESC')[0]
    return '/auth/google/callback?state=' + query['state'][0] + '&code=test-code', query, response


def test_google_login_preserves_deep_link_and_uses_pkce(sso):
    app, client, control = sso
    target = '/#run=' + 'a' * 32
    callback, query, start = begin(sso, target)
    assert query['scope'] == ['openid email profile']
    assert query['hd'] == ['berri.ai']
    assert 'offline' not in str(query)
    verifier = control['transaction']['verifier']
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == [challenge]
    assert query['code_challenge_method'] == ['S256']
    assert all(flag in start.headers['set-cookie'] for flag in ['HttpOnly', 'Secure', 'SameSite=lax'])
    assert client.get('/api/runs').status_code == 401
    response = client.get(callback, follow_redirects=False)
    assert response.headers['location'] == target
    assert response.headers['cache-control'] == 'no-store'
    session = client.get('/api/session').json()
    assert session['identity']['email'] == 'tin@berri.ai'
    assert session['role'] == 'admin'
    assert client.get('/api/runs').status_code == 200
    assert 'not-retained' not in client.cookies.get('workspace_session')
    assert not app.state.store.rows('SELECT * FROM login_states')
    assert client.get(callback, follow_redirects=False).headers['location'].startswith('/?signin=failed')
    assert control['exchanges'] == 1


@pytest.mark.parametrize('claims,omit', [
    ({'email': 'person@gmail.com', 'hd': 'gmail.com'}, []),
    ({'hd': 'evil.example'}, []),
    ({'email': 'tin@berri.ai.evil.example'}, []),
    ({'email_verified': False}, []), ({'email_verified': 'true'}, []),
    ({'aud': 'other-client'}, []), ({'iss': 'https://evil.example'}, []),
    ({'nonce': 'another-browser'}, []), ({'exp': 1}, []),
    ({'iat': int(time.time()) + 3600}, []), ({'azp': 'another-client'}, []),
    ({'aud': ['moyai-client', 'other']}, []),
    ({}, ['hd']), ({}, ['nonce']), ({}, ['email_verified']), ({}, ['exp']),
])
def test_untrusted_or_non_org_identity_never_gets_a_session(sso, claims, omit):
    _, client, control = sso
    control.update(claims=claims, omit=omit)
    callback, _, _ = begin(sso)
    assert 'signin=failed' in client.get(callback, follow_redirects=False).headers['location']
    assert not client.get('/api/session').json()['authenticated']
    assert client.get('/api/connections').status_code == 401


def test_wrong_signature_is_rejected(sso):
    _, client, control = sso
    control['key'] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    callback, _, _ = begin(sso)
    assert 'signin=failed' in client.get(callback, follow_redirects=False).headers['location']
    assert not client.get('/api/session').json()['authenticated']


def test_members_share_connections_but_cannot_administer_or_approve(sso):
    app, client, control = sso
    app.state.connectors.save('linear', {'access_token': 'test-only'}, 'Shared Linear')
    control['claims'] = {'email': 'teammate@berri.ai'}
    callback, _, _ = begin(sso)
    client.get(callback)
    session = client.get('/api/session').json()
    client.headers['X-CSRF-Token'] = session['csrf']
    assert session['role'] == 'member'
    assert client.get('/api/connections').json()[0]['connected']
    assert client.delete('/api/connections/linear').status_code == 403
    assert client.post('/api/approvals/fake', json={'decision': 'approve'}).status_code == 403
    app.state.settings.google_admin_emails = 'teammate@berri.ai'
    assert client.get('/api/session').json()['role'] == 'admin'
    app.state.settings.google_allowed_domains = 'different.example'
    assert client.get('/api/runs').status_code == 401


def test_state_is_bound_to_browser_expires_and_rejects_open_redirect(sso):
    app, client, control = sso
    callback, _, _ = begin(sso, '//evil.example')
    assert control['transaction']['return_path'] == '/#tasks'
    saved = dict(client.cookies)
    client.cookies.clear()
    assert 'signin=failed' in client.get(callback, follow_redirects=False).headers['location']
    assert control['exchanges'] == 0
    client.cookies.update(saved)
    app.state.store.execute('UPDATE login_states SET expires=0')
    assert 'signin=failed' in client.get(callback, follow_redirects=False).headers['location']
    assert control['exchanges'] == 0
    assert client.post('/api/auth/google/start', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403


def test_cancellation_does_not_call_google_and_can_retry(sso):
    _, client, control = sso
    callback, _, _ = begin(sso)
    assert 'signin=cancelled' in client.get(callback + '&error=access_denied', follow_redirects=False).headers['location']
    assert control['exchanges'] == 0
    callback, _, _ = begin(sso)
    assert client.get(callback, follow_redirects=False).headers['location'] == '/#tasks'


def test_disable_password_invalidates_password_sessions_without_locking_out_sso(sso):
    app, client, _ = sso
    assert client.post('/api/login', json={'password': 'test-workspace-password'}).status_code == 200
    app.state.settings.password_login_enabled = False
    assert client.get('/api/runs').status_code == 401
    assert client.post('/api/login', json={'password': 'test-workspace-password'}).status_code == 403
    callback, _, _ = begin(sso)
    client.get(callback)
    assert client.get('/api/session').json()['authenticated']
    app.state.settings.workspace_password = ''
    assert client.get('/api/runs').status_code == 200


def test_remote_sso_only_configuration_is_valid(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, public_url='https://workspace.example',
                             google_client_id='test-client', google_client_secret='test-secret',
                             google_admin_emails='tin@berri.ai', password_login_enabled=False))
    with TestClient(app, base_url='https://workspace.example') as client:
        assert not client.get('/api/session').json()['authenticated']
        assert client.get('/api/runs').status_code == 401


def test_google_login_respects_saved_role_and_preserves_users_link(sso):
    from app.user_roles import RoleChange
    app, client, _ = sso
    actor = {'method': 'google', 'identity': {'email': 'tin@berri.ai'}}
    app.state.user_roles.change(RoleChange(email='ishaan@berri.ai', role='admin', revision=0), actor)
    app.state.user_roles.change(RoleChange(email='tin@berri.ai', role='member', revision=0), actor)
    callback, _, _ = begin(sso, '/#users')
    assert client.get(callback, follow_redirects=False).headers['location'] == '/#users'
    assert client.get('/api/session').json()['role'] == 'member'
    assert client.get('/api/admin/users').status_code == 403


@pytest.mark.parametrize('generation', [0, 2, 999999999999999])
def test_google_login_returns_to_the_exact_credential_request(sso, generation):
    _, client, _ = sso
    target = '/#run=' + 'a' * 32 + '&credential=' + 'b' * 32 + f'&generation={generation}'
    callback, _, _ = begin(sso, target)
    response = client.get(callback, follow_redirects=False)
    assert response.headers['location'] == target


@pytest.mark.parametrize('suffix', [
    '&credential=' + 'b' * 32,
    '&credential=' + 'b' * 32 + '&generation=-1',
    '&credential=' + 'b' * 32 + '&generation=02',
    '&credential=' + 'b' * 32 + '&generation=9007199254740993',
    '&credential=' + 'b' * 32 + '&generation=2&token=secret',
    '&credential=' + 'b' * 32 + '&generation=2&credential=' + 'c' * 32,
    '&credential=%62' + 'b' * 31 + '&generation=2',
    '&credential=' + 'b' * 32 + '&generation=2\n',
])
def test_google_login_rejects_malformed_credential_targets(sso, suffix):
    _, client, _ = sso
    callback, _, _ = begin(sso, '/#run=' + 'a' * 32 + suffix)
    assert client.get(callback, follow_redirects=False).headers['location'] == '/#tasks'


def test_cancelled_google_login_preserves_credential_target_for_retry(sso):
    _, client, _ = sso
    target = '/#run=' + 'a' * 32 + '&credential=' + 'b' * 32 + '&generation=2'
    callback, _, _ = begin(sso, target)
    response = client.get(callback + '&error=access_denied', follow_redirects=False)
    assert response.headers['location'] == '/?signin=cancelled' + target[1:]
    assert client.get('/api/session').json()['authenticated'] is False


def test_google_sign_in_limits_are_per_client(sso):
    app, client, control = sso
    def start(host):
        visitor = TestClient(app, base_url=app.state.settings.public_url, client=(host, 50000))
        return visitor.post('/api/auth/google/start', json={}, headers={'Origin': app.state.settings.public_url})
    for _ in range(30):
        assert start('203.0.113.9').status_code == 200
    assert start('203.0.113.9').status_code == 429
    assert start('192.0.2.10').status_code == 200
    # Unfinished sign-ins are capped per client, so one client cannot fill the table for others.
    counts = {row['client']: row['n'] for row in app.state.store.rows('SELECT client, COUNT(*) AS n FROM login_states GROUP BY client')}
    assert counts == {'203.0.113.9': 5, '192.0.2.10': 1}


@pytest.mark.parametrize('cancelled', [False, True])
def test_google_login_preserves_markdown_file_target(sso, cancelled):
    from app.file_links import file_link
    _, client, _ = sso
    target = file_link('https://workspace.example', 'a' * 32, '/workspace/' + 'nested/' * 30 + 'report%20one.md').removeprefix('https://workspace.example')
    callback, _, _ = begin(sso, target)
    response = client.get(callback + ('&error=access_denied' if cancelled else ''), follow_redirects=False)
    assert response.headers['location'] == ('/?signin=cancelled' + target[1:] if cancelled else target)
