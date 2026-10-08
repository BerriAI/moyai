"""Credential prompts must follow authorized saved access and source lookup."""
import json

import httpx
import pytest

from app.credentials import CredentialRequest, Invoke, Materialize, ReportFailure
from test_credentials import KEY, save
from test_spend import active, sign_in
from test_workspace import workspace


def shared(client, *, scope='organization', suffix='shared', **fields):
    response = client.post('/api/credentials/secrets', json={
        'provider': 'generic', 'name': '1password-shared', 'format': 'env',
        'scope': scope, 'lifetime': 'persistent', 'label': 'Shared',
        'value': json.dumps({'OP_SERVICE_ACCOUNT_TOKEN': KEY}),
        'client_id': 'discovery-' + suffix, **fields})
    assert response.status_code == 201, response.text
    return response.json()['id']


def provider_request(**fields):
    return CredentialRequest(provider='openai', reason='Verify the requested workflow',
                             request_key='openai-workflow', **fields)


def use_source(vault, run, secret_id):
    source = vault.request(run, CredentialRequest(
        provider='generic', name='1password-shared', reason='Search Shared for workflow access',
        request_key='source-' + secret_id, secret_id=secret_id,
        input_fields=[{'name': 'OP_SERVICE_ACCOUNT_TOKEN', 'label': 'Service account token'}]))
    assert source['status'] == 'provided'
    material = vault.materialize(run, Materialize(request_ids=[source['request_id']]))
    assert material['status'] == 'ready'
    return {'secret_id': secret_id, 'revision': 1, 'outcome': 'not_found'}


@pytest.mark.parametrize('capability', [
    {'provider': 'openai'}, {'provider': 'generic', 'name': 'arize-ui'},
])
def test_broker_requires_source_lookup_without_creating_form(workspace, capability):
    app, client = workspace
    sign_in(app, client)
    app.state.settings.temporal_enabled = True
    run = active(app)
    source_id = shared(client)
    response = client.post('/broker/' + run['id'] + '/tools/call',
        headers={'Authorization': 'Bearer capability'}, json={
            'name': 'credentials_request', 'arguments': {
                **capability, 'reason': 'Verify workflow access', 'request_key': 'workflow'}})
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'lookup_required'
    assert [item['id'] for item in result['credential_sources']] == [source_id]
    assert 'moyai_wait_credential' not in result and KEY not in response.text
    assert not app.state.store.rows('SELECT * FROM credential_requests')
    assert not client.get('/api/runs/' + run['id']).json()['credential_requests']
    assert app.state.store.run(run['id'])['status'] == 'running'


def test_saved_personal_then_org_before_vault_and_no_automatic_replay(workspace):
    app, client = workspace
    sign_in(app, client)
    personal = save(client, provider='openai').json()['id']
    organization = save(client, 'organization', provider='openai', suffix='org').json()['id']
    shared(client)
    vault, run = app.state.credentials, active(app)
    first = vault.request(run, provider_request())
    assert first['status'] == 'provided'
    assert vault.row(first['request_id'])['secret_id'] == personal
    replacement = vault.report_failure(run, ReportFailure(
        request_id=first['request_id'], revision=1, failure='invalid'))
    assert replacement['status'] == 'provided' and replacement['retry_required']
    row = vault.row(first['request_id'])
    assert row['secret_id'] == organization and row['revision'] == 2
    assert not client.get('/api/runs/' + run['id']).json()['credential_requests']
    lookup = vault.report_failure(run, ReportFailure(
        request_id=first['request_id'], revision=row['revision'], failure='invalid'))
    assert lookup['status'] == 'lookup_required' and 'moyai_wait_credential' not in lookup
    assert not client.get('/api/runs/' + run['id']).json()['credential_requests']
    # A late failure from the personal key must not invalidate its replacement.
    assert vault.report_failure(run, ReportFailure(
        request_id=first['request_id'], revision=1, failure='invalid'))['status'] == 'stale'


def test_only_prompt_after_source_used_and_unsuccessful_outcome_reported(workspace):
    app, client = workspace
    sign_in(app, client)
    source_id = shared(client)
    vault, run = app.state.credentials, active(app)
    check = {'secret_id': source_id, 'revision': 1, 'outcome': 'not_found'}
    with pytest.raises(ValueError, match='credentials_run'):
        vault.request(run, provider_request(source_checks=[check]))
    assert not vault.store.rows('SELECT * FROM credential_requests')
    check = use_source(vault, run, source_id)
    # Loading the source alone does not claim that the requested item is absent.
    assert vault.request(run, provider_request())['status'] == 'lookup_required'
    result = vault.request(run, provider_request(source_checks=[check]))
    assert result['status'] == 'pending' and result['moyai_wait_credential']
    assert vault.request(run, provider_request(source_checks=[check]))['request_id'] == result['request_id']
    assert len(client.get('/api/runs/' + run['id']).json()['credential_requests']) == 1


def test_all_sources_require_checks_and_source_selection_does_not_pause(workspace):
    app, client = workspace
    sign_in(app, client)
    source_a = shared(client, suffix='a')
    source_b = shared(client, suffix='b')
    vault, run = app.state.credentials, active(app)
    check_a = use_source(vault, run, source_a)
    with pytest.raises(ValueError, match='new request_key'):
        vault.request(run, CredentialRequest(provider='generic', name='1password-shared',
            reason='Search Shared for workflow access', request_key='source-' + source_a, secret_id=source_b,
            input_fields=[{'name': 'OP_SERVICE_ACCOUNT_TOKEN', 'label': 'Service account token'}]))
    result = vault.request(run, provider_request(source_checks=[check_a]))
    assert [s['id'] for s in result['credential_sources']] == [source_b]
    assert result['request_arguments']['source_checks'] == [check_a]
    check_b = use_source(vault, run, source_b)
    retry = result['request_arguments']
    retry['source_checks'].append(check_b)
    assert vault.request(run, CredentialRequest(**retry))['status'] == 'pending'


def test_old_pending_form_does_not_block_source_access_or_new_saved_key(workspace):
    app, client = workspace
    sign_in(app, client)
    vault, run = app.state.credentials, active(app)
    pending = vault.request(run, provider_request())
    assert pending['status'] == 'pending'
    source_id = shared(client)
    assert vault.request(run, provider_request())['status'] == 'lookup_required'
    use_source(vault, run, source_id)
    saved = save(client, provider='openai').json()['id']
    ready = vault.request(run, provider_request())
    assert ready['status'] == 'provided' and ready['request_id'] == pending['request_id']
    assert vault.row(ready['request_id'])['secret_id'] == saved
    assert not client.get('/api/runs/' + run['id']).json()['credential_requests']


@pytest.mark.parametrize('source_state', ['foreign', 'expired', 'revoked', 'invalid', 'other_session'])
def test_unavailable_sources_do_not_leak_or_block_prompt(workspace, source_state):
    app, client = workspace
    sign_in(app, client)
    vault, run = app.state.credentials, active(app)
    source_id = shared(client, scope='personal')
    if source_state == 'foreign':
        run = active(app, 'google:bob')
    elif source_state == 'expired':
        vault.store.execute('UPDATE provider_secrets SET expires_at=? WHERE id=?',
                            ('2000-01-01T00:00:00+00:00', source_id))
    elif source_state == 'revoked':
        assert client.delete('/api/credentials/secrets/' + source_id).status_code == 200
    elif source_state == 'invalid':
        vault.store.execute("UPDATE provider_secrets SET invalid_reason='invalid' WHERE id=?", (source_id,))
    else:
        vault.store.execute("UPDATE provider_secrets SET lifetime='session',root_id=? WHERE id=?",
                            (active(app)['id'], source_id))
    result = vault.request(run, provider_request())
    assert result['status'] == 'pending' and source_id not in json.dumps(result)


@pytest.mark.parametrize('change', ['new_run', 'new_turn', 'rotation'])
def test_source_checks_cannot_survive_turn_or_revision_change(workspace, change):
    app, client = workspace
    sign_in(app, client)
    source_id = shared(client)
    vault, run = app.state.credentials, active(app)
    check = use_source(vault, run, source_id)
    if change == 'new_run':
        run = active(app)
    elif change == 'new_turn':
        run = {**run, 'active_message_id': run['active_message_id'] + 100}
    else:
        vault.store.execute('UPDATE provider_secrets SET revision=revision+1 WHERE id=?', (source_id,))
    if change == 'rotation':
        result = vault.request(run, provider_request(source_checks=[check]))
        assert result['status'] == 'lookup_required'
        assert result['credential_sources'][0]['revision'] == 2
    else:
        with pytest.raises(ValueError, match='this turn'):
            vault.request(run, provider_request(source_checks=[check]))


@pytest.mark.parametrize('selection', ['foreign', 'wrong_capability', 'expired'])
def test_saved_selection_cannot_expand_access(workspace, selection):
    app, client = workspace
    sign_in(app, client)
    saved = save(client, provider='openai' if selection != 'wrong_capability' else 'groq').json()['id']
    vault = app.state.credentials
    run = active(app, 'google:bob' if selection == 'foreign' else 'google:alice')
    if selection == 'expired':
        vault.store.execute('UPDATE provider_secrets SET expires_at=? WHERE id=?',
                            ('2000-01-01T00:00:00+00:00', saved))
    with pytest.raises(ValueError, match='authorized saved credential'):
        vault.request(run, provider_request(secret_id=saved))
    assert not vault.store.rows('SELECT * FROM credential_requests')


@pytest.mark.asyncio
async def test_provider_rejection_selects_alternative_without_replaying_http(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    save(client, provider='openai')
    save(client, 'organization', provider='openai', suffix='org')
    vault, run = app.state.credentials, active(app)
    request = vault.request(run, provider_request())
    calls = []

    def provider(req):
        calls.append(req)
        return httpx.Response(401, json={'error': {'code': 'invalid_api_key'}})

    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(provider), **kw))
    status, response = await vault.invoke(run, Invoke(
        request_id=request['request_id'], path='/chat/completions', body={'model': 'demo', 'messages': []}))
    assert status == 401 and response['status'] == 'provided' and response['retry_required']
    assert len(calls) == 1 and 'moyai_wait_credential' not in response


def test_declined_access_is_not_reopened_when_source_is_connected(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    vault, run = app.state.credentials, active(app)
    pending = vault.request(run, provider_request())
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/credentials/requests/' + pending['request_id'], json={'decision': 'decline'})
    assert response.status_code == 200
    shared(client)
    assert vault.request(run, provider_request())['status'] == 'declined'
