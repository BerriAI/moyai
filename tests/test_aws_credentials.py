import json
import os
import shlex
import sys
import time
from contextlib import nullcontext

import pytest
from botocore.exceptions import ClientError

from app import aws_login
from app.credentials import AWSWriteback, CredentialRequest, Materialize, ReportFailure
from agent.tools import credential_tools
from sandbox.aws_credentials import bundle, cache_paths
from test_spend import active, sign_in
from test_workspace import workspace

PROFILE = {'profile': 'audit', 'start_url': 'https://example.awsapps.com/start',
           'sso_region': 'us-east-1', 'account_id': '123456789012',
           'role_name': 'ReadOnly', 'region': 'us-west-2'}
TOKEN = 'synthetic-sso-access-token'
REFRESH = 'synthetic-refreshed-sso-token'


def saved_bundle():
    return {'version': 1, 'profile': dict(PROFILE), 'cache': {cache_paths(PROFILE)[0]: {
        'accessToken': TOKEN, 'expiresAt': '2030-01-01T00:00:00Z',
        'startUrl': PROFILE['start_url'], 'region': PROFILE['sso_region'],
        'refreshToken': 'synthetic-refresh-token', 'clientId': 'synthetic-client',
        'clientSecret': 'synthetic-client-secret', 'registrationExpiresAt': '2031-01-01T00:00:00Z'}}}


def save(client, value=None, **changes):
    return client.post('/api/credentials/secrets', json={
        'provider': 'generic', 'name': 'aws-sso/audit', 'format': 'aws-sso', 'label': 'AWS audit',
        'scope': 'personal', 'lifetime': 'persistent', 'client_id': 'aws-test-save',
        'value': json.dumps(value or saved_bundle()), **changes})


def request(vault, run):
    return vault.request(run, CredentialRequest(provider='generic', name='aws-sso/audit',
        format='aws-sso', reason='Inspect approved test resources', request_key='aws-test'))


def bound(workspace):
    app, client = workspace
    sign_in(app, client)
    app.state.settings.temporal_enabled = True
    secret_id = save(client).json()['id']
    run = active(app)
    handle = request(app.state.credentials, run)['request_id']
    material = app.state.credentials.materialize(run, Materialize(request_ids=[handle]))
    return app, client, run, secret_id, material['bindings'][0]


def refreshed(binding):
    value = json.loads(binding['value'])
    value['cache'][cache_paths(PROFILE)[0]]['accessToken'] = REFRESH
    return AWSWriteback(request_id=binding['request_id'], revision=binding['revision'], value=json.dumps(value))


def test_encrypted_personal_profile_reused_across_sessions_metadata_only(workspace):
    app, client, run, identity, binding = bound(workspace)
    vault = app.state.credentials
    assert TOKEN not in str(app.state.store.rows('SELECT * FROM provider_secrets'))
    assert TOKEN not in client.get('/api/credentials').text
    assert request(vault, active(app))['status'] == 'provided'
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert not client.get('/api/credentials').json()['secrets']
    assert request(vault, active(app, 'google:bob'))['status'] == 'login_required'
    assert client.delete('/api/credentials/secrets/' + identity).status_code == 404
    sign_in(app, client)
    assert save(client, scope='organization', client_id='aws-org-save').status_code == 422
    assert client.patch('/api/credentials/secrets/' + identity,
                        json={'revision': 1, 'scope': 'organization'}).status_code == 422


def test_writeback_persists_refresh_rejects_stale_and_retains_authorized_profile(workspace):
    app, client, run, identity, binding = bound(workspace)
    endpoint = '/broker/' + run['id'] + '/credentials/aws/writeback'
    data = refreshed(binding)
    headers = {'Authorization': 'Bearer capability'}
    response = client.post(endpoint, json={**data.model_dump(exclude={'value'}), 'value': data.value.get_secret_value()}, headers=headers)
    assert response.status_code == 200 and response.json() == {'status': 'saved', 'revision': 2}
    assert response.headers['cache-control'] == 'no-store'
    assert TOKEN not in response.text and REFRESH not in response.text
    vault = app.state.credentials
    assert vault.aws_writeback(run, data)['status'] == 'stale'
    new = vault.materialize(run, Materialize(request_ids=[binding['request_id']]))['bindings'][0]
    assert REFRESH in new['value'] and new['revision'] == 2
    other = json.loads(new['value'])
    other['profile']['region'] = 'eu-west-1'
    with pytest.raises(ValueError, match='cannot change'):
        vault.aws_writeback(run, AWSWriteback(request_id=binding['request_id'], revision=2, value=json.dumps(other)))


@pytest.mark.parametrize('change', ['revoke', 'replace', 'requester', 'expiry'])
def test_late_command_cannot_resurrect_or_overwrite_changed_access(workspace, change):
    app, client, run, identity, binding = bound(workspace)
    if change == 'revoke':
        assert client.delete('/api/credentials/secrets/' + identity).status_code == 200
    elif change == 'replace':
        assert client.patch('/api/credentials/secrets/' + identity, json={
            'revision': 1, 'value': json.dumps(saved_bundle())}).status_code == 200
    elif change == 'expiry':
        app.state.store.execute("UPDATE provider_secrets SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (identity,))
    else:
        app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
    endpoint = '/broker/' + run['id'] + '/credentials/aws/writeback'
    args = refreshed(binding)
    response = client.post(endpoint, json={**args.model_dump(exclude={'value'}), 'value': args.value.get_secret_value()},
                           headers={'Authorization': 'Bearer capability'})
    if change == 'requester':
        assert response.status_code == 403
    else:
        assert response.json()['status'] == 'stale'
    row = app.state.store.rows('SELECT * FROM provider_secrets WHERE id=?', (identity,))[0]
    if row['encrypted']:
        assert REFRESH not in app.state.security.decrypt(row['encrypted'])


def test_expired_sso_reauth_does_not_create_generic_form_or_replay(workspace):
    app, _, run, _, binding = bound(workspace)
    result = app.state.credentials.report_failure(run, ReportFailure(
        request_id=binding['request_id'], revision=binding['revision'], failure='expired'))
    assert result['status'] == 'login_required'
    assert request(app.state.credentials, run)['status'] == 'login_required'
    assert not app.state.credentials.pending(run, 'google:alice', True)


@pytest.mark.parametrize('field,value', [('active_user_id', 'google:bob'), ('token_hash', 'rotated'),
                                       ('status', 'cancelled')])
def test_requester_fence_rechecked_after_admission_before_loading_or_saving(workspace, field, value):
    from fastapi import HTTPException
    app, _, run, _, binding = bound(workspace)
    app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, run['id']))
    vault = app.state.credentials
    if field == 'status':
        assert vault.materialize(run, Materialize(request_ids=[binding['request_id']]))['status'] == 'stale'
    else:
        with pytest.raises(HTTPException) as rejected:
            vault.materialize(run, Materialize(request_ids=[binding['request_id']]))
        assert rejected.value.status_code == 409
    assert vault.aws_writeback(run, refreshed(binding))['status'] == 'stale'


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(config='credential_process = steal'),
    lambda value: value['profile'].update(profile='x\n[default]'),
    lambda value: value['profile'].update(start_url='http://169.254.169.254/'),
    lambda value: value['cache'].update({'../../credentials': {}}),
    lambda value: value['cache'][cache_paths(PROFILE)[0]].update(startUrl='https://other.awsapps.com/start'),
    lambda value: value['cache'][cache_paths(PROFILE)[0]].update(accessToken='x' * 131073),
])
def test_unsafe_bundles_are_rejected(mutate):
    value = saved_bundle()
    mutate(value)
    with pytest.raises(ValueError, match='Invalid AWS'):
        bundle(json.dumps(value))


class OIDC:
    def __init__(self):
        self.polls = 0
        self.error = None
    def register_client(self, **kwargs):
        assert kwargs['scopes'] == ['sso:account:access']
        return {'clientId': 'synthetic-client', 'clientSecret': 'synthetic-client-secret',
                'clientSecretExpiresAt': int(time.time()) + 86400}
    def start_device_authorization(self, **kwargs):
        return {'deviceCode': 'private-device-secret', 'userCode': 'TEST-CODE',
                'verificationUri': 'https://device.sso.us-east-1.amazonaws.com/',
                'verificationUriComplete': 'https://device.sso.us-east-1.amazonaws.com/?user_code=TEST-CODE',
                'expiresIn': 600, 'interval': 5}
    def create_token(self, **kwargs):
        self.polls += 1
        if self.error:
            raise self.error
        return {'accessToken': TOKEN, 'refreshToken': 'synthetic-refresh-token', 'expiresIn': 3600}


def start_login(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    stub = OIDC()
    monkeypatch.setattr(aws_login, 'oidc', lambda region: nullcontext(stub))
    vault = app.state.credentials
    args = aws_login.AWSLogin(action='start', profile=PROFILE, lifetime='persistent', request_key='device-test')
    result = aws_login.start(vault, run, args)
    assert result['status'] == 'pending' and 'private-device-secret' not in str(result)
    assert 'clientSecret' not in str(result)
    return app, client, run, stub, args, result


def test_device_login_durable_poll_complete_and_revoke(workspace, monkeypatch):
    app, client, run, stub, args, result = start_login(workspace, monkeypatch)
    vault = app.state.credentials
    identity = result['login_id']
    assert aws_login.start(vault, run, args) == result
    row = app.state.store.rows('SELECT * FROM aws_login_requests')[0]
    assert 'private-device-secret' not in str(row)
    poll = aws_login.AWSLogin(action='poll', login_id=identity)
    assert aws_login.poll_or_cancel(vault, run, poll)['status'] == 'pending' and stub.polls == 0
    app.state.store.execute('UPDATE aws_login_requests SET next_poll=0 WHERE id=?', (identity,))
    result = aws_login.poll_or_cancel(vault, run, poll)
    assert result['status'] == 'complete' and stub.polls == 1
    assert TOKEN not in str(result) and request(vault, run)['status'] == 'provided'
    assert aws_login.poll_or_cancel(vault, run, poll) == result and stub.polls == 1
    assert app.state.store.rows('SELECT encrypted FROM aws_login_requests')[0]['encrypted'] == ''
    client.delete('/api/credentials/secrets/' + result['secret_id'])
    assert aws_login.poll_or_cancel(vault, run, poll)['status'] == 'login_required'


@pytest.mark.parametrize('error,status,interval', [
    ('AuthorizationPendingException', 'pending', 5), ('SlowDownException', 'pending', 10),
    ('ExpiredTokenException', 'failed', 5), ('AccessDeniedException', 'failed', 5),
    ('network', 'uncertain', 5),
])
def test_device_poll_backoff_and_uncertain_exchange_not_replayed(workspace, monkeypatch, error, status, interval):
    app, _, run, stub, _, started = start_login(workspace, monkeypatch)
    stub.error = OSError('sensitive network error') if error == 'network' else ClientError(
        {'Error': {'Code': error, 'Message': 'sensitive AWS response'}}, 'CreateToken')
    app.state.store.execute('UPDATE aws_login_requests SET next_poll=0 WHERE id=?', (started['login_id'],))
    args = aws_login.AWSLogin(action='poll', login_id=started['login_id'])
    result = aws_login.poll_or_cancel(app.state.credentials, run, args)
    assert result['status'] == status and 'sensitive' not in str(result)
    assert app.state.store.rows('SELECT interval_seconds FROM aws_login_requests')[0]['interval_seconds'] == interval
    aws_login.poll_or_cancel(app.state.credentials, run, args)
    assert stub.polls == 1


def test_device_cancel_requester_change_and_expiry(workspace, monkeypatch):
    from fastapi import HTTPException
    app, _, run, stub, _, started = start_login(workspace, monkeypatch)
    poll = aws_login.AWSLogin(action='poll', login_id=started['login_id'])
    bob = active(app, 'google:bob')
    with pytest.raises(HTTPException):
        aws_login.poll_or_cancel(app.state.credentials, bob, poll)
    assert aws_login.poll_or_cancel(app.state.credentials, run,
        aws_login.AWSLogin(action='cancel', login_id=started['login_id']))['status'] == 'cancelled'
    assert aws_login.poll_or_cancel(app.state.credentials, run, poll)['status'] == 'cancelled'
    assert not app.state.store.rows('SELECT encrypted FROM aws_login_requests')[0]['encrypted']
    assert stub.polls == 0


@pytest.mark.skipif(sys.platform != 'linux', reason='Production AWS cache homes require Linux tmpfs')
@pytest.mark.parametrize('outcome', ['success', 'failure', 'timeout', 'lost_writeback', 'symlink', 'parent_symlink', 'malformed'])
def test_command_tmpfs_refresh_redaction_cleanup_and_no_replay(monkeypatch, outcome):
    monkeypatch.setattr(credential_tools, 'ensure_tools', lambda command: None)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'infrastructure-key-must-not-reach-command')
    monkeypatch.setenv('AWS_ENDPOINT_URL', 'https://infrastructure-endpoint.invalid')
    identity, calls, homes = 'a' * 32, [], []
    value = saved_bundle()
    binding = {'request_id': identity, 'revision': 1, 'name': 'aws-sso/audit',
               'format': 'aws-sso', 'env_var': '', 'value': json.dumps(value)}
    def broker(path, body):
        calls.append((path, body))
        if path.endswith('/materialize'):
            return {'status': 'ready', 'bindings': [dict(binding)]}
        if path.endswith('/writeback'):
            if outcome == 'lost_writeback':
                raise OSError('lost ack')
            assert REFRESH in body['value']
            return {'status': 'saved', 'revision': 2}
        return {'status': 'unknown'}
    source = (
        "import os,json,pathlib,stat,sys\n"
        "home=pathlib.Path(os.environ['HOME']); print('home='+str(home))\n"
        "assert str(home).startswith('/dev/shm/moyai-aws-')\n"
        "assert 'AWS_ACCESS_KEY_ID' not in os.environ and 'AWS_ENDPOINT_URL' not in os.environ\n"
        "assert os.environ['AWS_EC2_METADATA_DISABLED']=='true'\n"
        f"path=home/'.aws'/{cache_paths(PROFILE)[0]!r}\n"
        "assert stat.S_IMODE(path.stat().st_mode)==0o600\n"
        "data=json.loads(path.read_text()); print(data['accessToken'])\n"
        f"data['accessToken']={REFRESH!r}; path.write_text(json.dumps(data)); print(data['accessToken'])\n"
    )
    if outcome == 'failure':
        source += "print('AccessDeniedException'); sys.exit(1)\n"
    elif outcome == 'timeout':
        source += "import time; time.sleep(30)\n"
    elif outcome == 'symlink':
        source += "path.unlink(); path.symlink_to('/etc/passwd')\n"
    elif outcome == 'parent_symlink':
        source += "import shutil; shutil.rmtree(path.parent); path.parent.symlink_to('/etc')\n"
    elif outcome == 'malformed':
        source += "path.write_text('not JSON '+data['accessToken'])\n"
    original = credential_tools.execute
    executions = []
    def execute(command, env, fds, timeout):
        homes.append(env['HOME'])
        executions.append(command)
        return original(command, env, fds, timeout)
    monkeypatch.setattr(credential_tools, 'execute', execute)
    result = credential_tools.run({'request_ids': [identity],
        'command': shlex.join([sys.executable, '-u', '-c', source]),
        'timeout': 1 if outcome == 'timeout' else 30}, broker)
    assert len(executions) == 1 and not any(os.path.exists(path) for path in homes)
    assert TOKEN not in str(result) and REFRESH not in str(result)
    if outcome in {'malformed', 'symlink', 'parent_symlink'}:
        assert result['output'] == '' and result['cache_status'] == 'not_saved'
        assert len(calls) == 1
    else:
        assert result['cache_status'] == ('unknown' if outcome == 'lost_writeback' else 'saved')
        assert result['output'].count('[credential redacted]') == 2


def test_concurrent_device_poll_and_cancellation_do_not_save_late_tokens(workspace, monkeypatch):
    from threading import Event, Thread
    app, _, run, stub, _, started = start_login(workspace, monkeypatch)
    app.state.store.execute('UPDATE aws_login_requests SET next_poll=0 WHERE id=?', (started['login_id'],))
    args = aws_login.AWSLogin(action='poll', login_id=started['login_id'])
    entered, release, results = Event(), Event(), []
    original = stub.create_token
    def blocked(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    stub.create_token = blocked
    worker = Thread(target=lambda: results.append(aws_login.poll_or_cancel(app.state.credentials, run, args)))
    worker.start()
    try:
        assert entered.wait(10)
        assert aws_login.poll_or_cancel(app.state.credentials, run, args)['status'] == 'polling'
        assert aws_login.poll_or_cancel(app.state.credentials, run,
            aws_login.AWSLogin(action='cancel', login_id=started['login_id']))['status'] == 'cancelled'
    finally:
        release.set()
        worker.join(10)
    assert results == [{'status': 'cancelled', 'login_id': started['login_id']}]
    assert stub.polls == 1 and not app.state.store.rows('SELECT * FROM provider_secrets')


def test_live_requester_change_during_device_exchange_drops_tokens(workspace, monkeypatch):
    app, _, run, stub, _, started = start_login(workspace, monkeypatch)
    app.state.store.execute('UPDATE aws_login_requests SET next_poll=0 WHERE id=?', (started['login_id'],))
    original = stub.create_token
    def changed(**kwargs):
        app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
        return original(**kwargs)
    stub.create_token = changed
    result = aws_login.poll_or_cancel(app.state.credentials, run,
        aws_login.AWSLogin(action='poll', login_id=started['login_id']))
    assert result['status'] == 'uncertain'
    assert not app.state.store.rows('SELECT * FROM provider_secrets')
    assert app.state.store.rows('SELECT encrypted FROM aws_login_requests')[0]['encrypted'] == ''


def test_oidc_ignores_infrastructure_profile_endpoints_and_credential_chain(monkeypatch):
    import botocore.session
    from botocore import UNSIGNED
    monkeypatch.setenv('AWS_PROFILE', 'must-not-load-infrastructure')
    monkeypatch.setenv('AWS_ENDPOINT_URL', 'http://127.0.0.1:1')
    monkeypatch.setenv('AWS_ENDPOINT_URL_SSO_OIDC', 'http://127.0.0.1:1')
    def forbidden(*args):
        raise AssertionError('Must not resolve infrastructure credentials')
    monkeypatch.setattr(botocore.session.Session, 'get_credentials', forbidden)
    with aws_login.oidc('us-east-1') as client:
        assert client.meta.endpoint_url == 'https://oidc.us-east-1.amazonaws.com'
        assert client.meta.config.signature_version == UNSIGNED
        assert client.meta.config.retries['total_max_attempts'] == 1


def test_device_flow_uses_real_sdk_request_and_response_shapes(workspace, monkeypatch):
    from botocore.stub import Stubber
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    expiry = int(time.time()) + 86400
    with aws_login.oidc('us-east-1') as aws:
        with Stubber(aws) as responses:
            responses.add_response('register_client', {
                'clientId': 'client-id', 'clientSecret': 'client-secret', 'clientSecretExpiresAt': expiry},
                {'clientName': 'Moyai personal AWS access', 'clientType': 'public',
                 'scopes': ['sso:account:access'],
                 'grantTypes': ['urn:ietf:params:oauth:grant-type:device_code', 'refresh_token']})
            responses.add_response('start_device_authorization', {
                'deviceCode': 'private-device-code', 'userCode': 'USER-CODE',
                'verificationUri': 'https://device.sso.us-east-1.amazonaws.com/',
                'expiresIn': 600, 'interval': 5},
                {'clientId': 'client-id', 'clientSecret': 'client-secret', 'startUrl': PROFILE['start_url']})
            responses.add_response('create_token', {
                'accessToken': TOKEN, 'refreshToken': REFRESH, 'expiresIn': 3600, 'tokenType': 'Bearer'},
                {'clientId': 'client-id', 'clientSecret': 'client-secret', 'deviceCode': 'private-device-code',
                 'grantType': 'urn:ietf:params:oauth:grant-type:device_code'})
            monkeypatch.setattr(aws_login, 'oidc', lambda region: nullcontext(aws))
            result = aws_login.start(app.state.credentials, run,
                aws_login.AWSLogin(action='start', profile=PROFILE, request_key='sdk-shape'))
            assert result['status'] == 'pending'
            app.state.store.execute('UPDATE aws_login_requests SET next_poll=0 WHERE id=?', (result['login_id'],))
            result = aws_login.poll_or_cancel(app.state.credentials, run,
                aws_login.AWSLogin(action='poll', login_id=result['login_id']))
            assert result['status'] == 'complete'
            saved = app.state.store.rows('SELECT * FROM provider_secrets')[0]
            assert saved['lifetime'] == 'session' and saved['root_id'] == run['id']
            assert TOKEN not in str(result) and REFRESH not in str(result)
            responses.assert_no_pending_responses()
