import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.security import digest

ACCESS = dict(cloudflare_access_team_domain='test-team.cloudflareaccess.com',
              cloudflare_access_audience='employee-audience',
              cloudflare_access_broker_audience='broker-audience')


@pytest.fixture(scope='module')
def access_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid='test-access-key', use='sig', alg='RS256')
    return private, jwk


def assertion(key, audience='employee-audience', **claims):
    payload = dict(iss='https://test-team.cloudflareaccess.com', aud=[audience],
                   exp=int(time.time()) + 300, iat=int(time.time()), type='app', sub='test-person')
    payload.update(claims)
    return jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'test-access-key'})


@pytest.fixture
def guarded(tmp_path, monkeypatch, access_key):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='https://workspace.example',
                        workspace_password='test-workspace-password', **ACCESS)
    app = create_app(settings)
    upstream = {'calls': 0, 'status': 200, 'keys': [access_key[1]]}
    def response(request):
        assert str(request.url) == 'https://test-team.cloudflareaccess.com/cdn-cgi/access/certs'
        upstream['calls'] += 1
        return httpx.Response(upstream['status'], json={'keys': upstream['keys']})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.cloudflare_access.httpx.AsyncClient',
                        lambda **kw: actual(transport=httpx.MockTransport(response), **kw))
    with TestClient(app, base_url=settings.public_url) as client:
        client.headers['Origin'] = settings.public_url
        yield app, client, upstream


def test_gate_precedes_app_auth_and_keeps_existing_login(guarded, access_key):
    app, client, upstream = guarded
    for path in ('/', '/static/app.js', '/api/session', '/api/credentials', '/api/runs'):
        result = client.get(path)
        assert result.status_code == 401
        assert result.json()['detail'] == 'Cloudflare Access authentication required.'
        assert result.headers['cache-control'] == 'no-store'
    assert upstream['calls'] == 0
    client.headers['Cf-Access-Jwt-Assertion'] = assertion(access_key[0])
    assert client.get('/api/session').json()['authenticated'] is False
    assert client.get('/api/credentials').json()['detail'] == 'Sign in to the workspace.'
    assert client.post('/api/login', json={'password': 'test-workspace-password'}).status_code == 200
    assert client.get('/api/credentials').status_code == 200
    del client.headers['Cf-Access-Jwt-Assertion']
    assert client.get('/api/credentials').status_code == 401  # Session cookie cannot bypass Access.
    assert upstream['calls'] == 1


@pytest.mark.parametrize('claims', [
    {'exp': 1}, {'iat': 9999999999}, {'iss': 'https://attacker.example'},
    {'aud': ['unrelated-audience']}, {'aud': ['employee-audience', 'broker-audience']},
    {'type': 'org'},
])
def test_invalid_assertions_cannot_open_the_app(guarded, access_key, claims):
    _, client, _ = guarded
    result = client.get('/api/session', headers={'Cf-Access-Jwt-Assertion': assertion(access_key[0], **claims)})
    assert result.status_code == 401


def test_forged_and_unsigned_assertions_are_rejected(guarded, access_key):
    _, client, _ = guarded
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (assertion(attacker), 'not-a-token', jwt.encode({'aud': 'employee-audience'}, key='', algorithm='none')):
        assert client.get('/', headers={'Cf-Access-Jwt-Assertion': token}).status_code == 401
    assert client.get('/', headers={'Cf-Access-Authenticated-User-Email': 'tin@berri.ai'}).status_code == 401


def test_machine_assertion_and_run_capability_are_both_required(guarded, access_key):
    app, client, _ = guarded
    run = app.state.store.create_run('Synthetic task', '', 'modal', [], model='test-model')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('test-run-token'))
    path = '/broker/' + run['id'] + '/v1/models'
    employee = assertion(access_key[0])
    machine = assertion(access_key[0], 'broker-audience', sub='', common_name='test-service')
    assert client.get(path, headers={'Authorization': 'Bearer test-run-token'}).status_code == 401
    assert client.get(path, headers={'Cf-Access-Jwt-Assertion': employee, 'Authorization': 'Bearer test-run-token'}).status_code == 401
    assert client.get(path, headers={'Cf-Access-Jwt-Assertion': machine}).status_code == 401
    headers = {'Cf-Access-Jwt-Assertion': machine, 'Authorization': 'Bearer test-run-token'}
    assert client.get(path, headers=headers).status_code == 200
    assert client.get('/api/session', headers=headers).status_code == 401
    app.state.store.update_run(run['id'], status='completed')
    assert client.get(path, headers=headers).status_code == 401


def test_only_exact_webhook_methods_bypass_access_and_still_require_signatures(guarded):
    _, client, upstream = guarded
    assert client.get('/health').json() == {'status': 'ok'}
    for path in ('/hooks/slack/events', '/hooks/slack/interactions'):
        response = client.post(path, json={'type': 'url_verification', 'challenge': 'must-not-echo'})
        assert response.status_code == 401
        assert 'Cloudflare' not in response.text and 'must-not-echo' not in response.text
        assert client.get(path).status_code == 401
        assert client.post(path + '/').json()['detail'] == 'Cloudflare Access authentication required.'
    assert client.post('/hooks/unknown').status_code == 401
    assert upstream['calls'] == 0


def test_key_fetch_failure_fails_closed(guarded, access_key):
    _, client, upstream = guarded
    upstream['status'] = 503
    token = assertion(access_key[0])
    for _ in range(2):
        response = client.get('/api/session', headers={'Cf-Access-Jwt-Assertion': token})
        assert response.status_code == 503
        assert token not in response.text
    assert upstream['calls'] == 1


def test_unknown_keys_are_throttled_and_rotations_can_refresh(guarded, access_key):
    app, client, upstream = guarded
    headers = {'Cf-Access-Jwt-Assertion': assertion(access_key[0])}
    assert client.get('/api/session', headers=headers).status_code == 200
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(new_key.public_key()))
    jwk.update(kid='rotated', use='sig', alg='RS256')
    claims = jwt.decode(assertion(new_key), options={'verify_signature': False})
    token = jwt.encode(claims, new_key, algorithm='RS256', headers={'kid': 'rotated'})
    headers = {'Cf-Access-Jwt-Assertion': token}
    for _ in range(3):
        assert client.get('/api/session', headers=headers).status_code == 401
    assert upstream['calls'] == 1
    upstream['keys'] = [jwk]
    app.state.cloudflare_access.last_fetch -= 11
    assert client.get('/api/session', headers=headers).status_code == 200
    assert upstream['calls'] == 2


@pytest.mark.parametrize('overrides', [
    {'cloudflare_access_team_domain': ''}, {'cloudflare_access_team_domain': 'attacker.example'},
    {'cloudflare_access_broker_audience': 'employee-audience'},
    {'cloudflare_access_client_id': 'unpaired'},
    {'cloudflare_access_webhook_paths': ['/api/credentials']},
    {'cloudflare_access_webhook_paths': ['/hooks/automations/*']},
    {'public_url': 'https://user:password@workspace.example'},
    {'public_url': 'https://workspace.example/api'},
    {'public_url': 'https://workspace.example?leak=1'},
    {'public_url': 'https://'},
])
def test_unsafe_or_partial_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **({'public_url': 'https://workspace.example'} | ACCESS | overrides))


def test_runtime_credentials_are_not_in_settings_repr_and_clear_stale_environment():
    settings = Settings(_env_file=None, public_url='https://workspace.example', **ACCESS,
                        cloudflare_access_client_id='sensitive-client', cloudflare_access_client_secret='sensitive-secret')
    assert 'sensitive-client' not in repr(settings) and 'sensitive-secret' not in repr(settings)
    assert settings.broker_environment('run-token')['WORKSPACE_ACCESS_CLIENT_SECRET'] == 'sensitive-secret'
    plain = Settings(_env_file=None).broker_environment('new-run')
    assert plain['WORKSPACE_RUN_TOKEN'] == 'new-run'
    assert plain['WORKSPACE_ACCESS_CLIENT_SECRET'] == ''
