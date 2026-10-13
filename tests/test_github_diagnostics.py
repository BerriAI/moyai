"""Regressions from live publication, CI-access and close-PR blockers."""
import copy
import hashlib
import json

import httpx
import pytest

from test_github_coding import coding
from test_github import select
from test_github_identity import identity, call as identity_call
from test_workspace import workspace
from scripts.github_coding_fixture import COMMIT, PREFIX


def pending(coding, *, attempted=1, commit=COMMIT, result='', version=None):
    app, _, rid, _, github, provider, _ = coding
    key = 'lost-publication'
    identity = hashlib.sha256(f'{rid}:{key}'.encode()).hexdigest()
    app.state.store.execute('''INSERT INTO github_publications
        (id,run_id,message_id,arguments_hash,branch,commit_sha,result,connection_version,created_at,attempted)
        VALUES(?,?,0,'fixture',?,?,?,?, 'fixture',?)''',
        (identity, rid, provider.pr['head']['ref'], commit, result, version or github.connection_version(), attempted))
    provider.pr['html_url'] = 'https://github.com/BerriAI/litellm/pull/100'
    return key


def readback(coding, monkeypatch, value, callback=None):
    provider = coding[5]
    original = provider.handle
    def handle(request):
        if request.method == 'GET' and request.url.path == PREFIX + '/pulls':
            assert request.url.params['head'] == 'BerriAI:' + provider.pr['head']['ref']
            assert request.url.params['state'] == 'all'
            if callback:
                callback()
            return httpx.Response(200, json=value)
        return original(request)
    monkeypatch.setattr(provider, 'handle', handle)


@pytest.mark.parametrize('visible', [False, True])
def test_publication_readback_never_retries_or_creates_write_authority(coding, monkeypatch, visible):
    app, _, _, _, _, provider, call = coding
    key = pending(coding)
    readback(coding, monkeypatch, [provider.pr] if visible else [])
    # It remains usable when the organization disables writes.
    app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    for _ in range(2):
        result = call('github_publication_status', request_key=key).json()
        assert result['status'] == ('observed_match' if visible else 'unconfirmed'), result
        assert result['receipt_saved'] is False and result['creation_attempted'] is True
    assert provider.dispatches == []
    assert [scope for scope in provider.minted if scope != {'metadata': 'read'}] == [{'pull_requests': 'read'}]
    assert app.state.store.rows("SELECT result FROM github_publications WHERE id!='publication'")[0]['result'] == ''


@pytest.mark.parametrize('field,value', [('head', None), ('head', []), ('number', True),
    ('html_url', 'https://other.invalid'), ('state', 'merged'), ('draft', None),
    ('head', {'repo': []}), ('head', {'ref': 'foreign', 'sha': COMMIT, 'repo': {'id': 101}})])
def test_publication_readback_rejects_mismatches(coding, monkeypatch, field, value):
    key = pending(coding)
    pr = copy.deepcopy(coding[5].pr)
    pr[field] = value
    readback(coding, monkeypatch, [pr])
    result = coding[6]('github_publication_status', request_key=key).json()
    assert 'error' in result and not result['outcome_uncertain']
    assert coding[5].dispatches == []


@pytest.mark.parametrize('kind', ['missing', 'preparation', 'confirmed', 'connection_changed', 'wrong_repository'])
def test_publication_local_receipt_states(coding, kind):
    provider, call = coding[5:]
    saved = json.dumps({'number': 100, 'repository_id': 999 if kind == 'wrong_repository' else 101})
    key = 'not-started' if kind == 'missing' else pending(coding,
        commit='' if kind == 'preparation' else COMMIT,
        result=saved if kind in {'confirmed', 'wrong_repository'} else '',
        version='old' if kind == 'connection_changed' else None)
    result = call('github_publication_status', request_key=key).json()
    if kind in {'connection_changed', 'wrong_repository'}:
        assert 'error' in result
    else:
        assert result['status'] == {'missing': 'not_recorded', 'preparation': 'preparation_unconfirmed',
                                    'confirmed': 'confirmed_receipt'}[kind]
    assert not provider.calls and all(scope == {'metadata': 'read'} for scope in provider.minted)


def test_readback_rechecks_access_after_provider_read(coding, monkeypatch):
    app, _, _, _, _, _, call = coding
    key = pending(coding)
    readback(coding, monkeypatch, [], lambda: app.state.store.execute(
        "INSERT INTO connection_policies(provider,enabled) VALUES('github',0)"))
    result = call('github_publication_status', request_key=key).json()
    assert 'error' in result


def test_capabilities_reports_real_missing_scopes_and_policy_without_writes(coding):
    app, _, _, _, _, provider, call = coding
    for name in ('checks', 'statuses', 'actions', 'issues', 'workflows'):
        provider.permissions.pop(name, None)
    app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    result = call('github_capabilities').json()
    entries = {item['tool']: item for item in result['tools']}
    assert entries['github_ci_checks']['missing_permissions'] == {'checks': 'read', 'statuses': 'read'}
    assert entries['github_workflow_action']['missing_permissions'] == {'actions': 'write'}
    assert entries['github_create_issue']['missing_permissions'] == {'issues': 'write'}
    assert entries['github_close_pull_request']['policy_allowed'] is False
    assert entries['github_ci_checks']['policy_allowed'] is True
    assert entries['github_search']['requirements_by_kind']['issues'] == {'issues': 'read'}
    assert result['workflow_file_permission']['installed'] is False
    assert result['prohibited_operations'] == ['merge', 'auto_merge', 'approve_review']
    assert not provider.dispatches and all(scope == {'metadata': 'read'} for scope in provider.minted)
    assert 'fixture-jwt' not in json.dumps(result)


@pytest.mark.parametrize('ids', [(101,), (202, 101)])
def test_untargeted_capabilities_report_installation_choices_without_binding(identity, ids):
    app, client, run, headers, github, provider = identity
    select(app, ids)
    before = app.state.store.run(run)
    result = identity_call(client, run, headers, 'github_capabilities')
    after = app.state.store.run(run)
    assert all(after[key] == before[key] for key in ('github_repository_id', 'repo_url', 'environment_id', 'environment_build_id'))
    assert result['scope'] == 'installation'
    assert result['repository_id'] is None and result['repository'] is None
    assert result['repositories'] == github.repository_options()
    assert [item['id'] for item in result['repositories']] == list(ids)
    assert result['installation_active'] and result['tools']
    assert provider.calls == [('GET', '/app/installations/10', None)]
    assert not app.state.store.rows('SELECT 1 FROM environment_builds')
    assert not app.state.store.rows('SELECT 1 FROM github_publications')


@pytest.mark.parametrize('selection', ['explicit', 'saved_id', 'saved_name'])
def test_capabilities_respects_explicit_and_saved_repository(identity, selection):
    app, client, run, headers, github, _ = identity
    select(app, (101, 202))
    arguments = {'repository_id': 202} if selection == 'explicit' else {}
    if selection != 'explicit':
        app.state.store.execute('UPDATE runs SET github_repository_id=?,repo_url=? WHERE id=?',
                                (202 if selection == 'saved_id' else None, 'https://github.com/BerriAI/moyai', run))
    result = identity_call(client, run, headers, 'github_capabilities', **arguments)
    assert result['scope'] == 'repository'
    assert result['repository_id'] == 202 and result['repository'] == 'BerriAI/moyai'
    current = app.state.store.run(run)
    assert current['github_repository_id'] == (None if selection == 'explicit' else 202)
    assert current['repo_url'] == ('' if selection == 'explicit' else 'https://github.com/BerriAI/moyai')
    assert github.saved_credentials()['repository_ids'] == [101, 202]


@pytest.mark.parametrize('selection', ['explicit', 'saved'])
def test_capabilities_rejects_unauthorized_target_instead_of_installation_fallback(identity, selection):
    app, client, run, headers, _, provider = identity
    arguments = {'repository_id': 202} if selection == 'explicit' else {}
    if selection == 'saved':
        app.state.store.execute('UPDATE runs SET github_repository_id=202 WHERE id=?', (run,))
    result = identity_call(client, run, headers, 'github_capabilities', **arguments)
    assert 'not selected' in result['error']
    assert 'repositories' not in result and not provider.calls


@pytest.mark.parametrize('change', ['actor', 'capability', 'message', 'status', 'plugins', 'deleted', 'connection', 'policy'])
def test_untargeted_capabilities_rechecks_authority_after_installation_read(identity, change):
    app, client, run, headers, _, provider = identity
    select(app, (101, 202))
    def revoke(method, path):
        if method != 'GET' or path != '/app/installations/10':
            return
        if change == 'connection':
            select(app, (101,))
        elif change == 'policy':
            app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0)")
        else:
            field, value = {'actor': ('active_user_id', 'different-user'), 'capability': ('token_hash', 'different-token'),
                'message': ('active_message_id', 999), 'status': ('status', 'idle'), 'plugins': ('plugins', '[]'),
                'deleted': ('deleted_at', 'deleted')}[change]
            app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, run))
    provider.on_request = revoke
    result = identity_call(client, run, headers, 'github_capabilities')
    assert 'error' in result and 'repositories' not in result
    assert provider.calls == [('GET', '/app/installations/10', None)]


@pytest.mark.parametrize('restriction', ['foreign', 'head', 'merged', 'scope', 'readonly'])
def test_close_requires_authority_head_and_permissions(coding, restriction):
    app, _, _, _, _, provider, call = coding
    args = {'number': 100, 'head_sha': COMMIT, 'request_key': 'close-test'}
    if restriction == 'foreign':
        app.state.store.execute('DELETE FROM github_publications')
    elif restriction == 'head':
        args['head_sha'] = 'a' * 40
    elif restriction == 'merged':
        provider.pr['merged'] = True
    elif restriction == 'scope':
        provider.permissions['pull_requests'] = 'read'
    else:
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    result = call('github_close_pull_request', **args)
    assert result.status_code != 200 or 'error' in result.json()
    assert not provider.dispatches


def test_close_cannot_smuggle_merge_or_approval(coding):
    provider, call = coding[5:]
    for extra in ({'state': 'merged'}, {'merge': True}, {'event': 'APPROVE'}):
        response = call('github_close_pull_request', number=100, head_sha=COMMIT, request_key='close-extra', **extra)
        assert response.status_code != 200 or 'error' in response.json()
    assert not provider.dispatches
    result = call('github_close_pull_request', number=100, head_sha=COMMIT, request_key='close-valid').json()
    assert result['confirmed'] and provider.dispatches == [('PATCH', PREFIX + '/pulls/100', {'state': 'closed'})]
