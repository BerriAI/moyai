"""Rename, migration and authorization invariants through real broker/HTTP code."""
import asyncio
import hashlib
import json

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.github import MANIFEST_PERMISSIONS, Publish
from app.automation_sources import EventTrigger, normalize
from test_workspace import workspace
from test_github import connected, select, credentials, repository_data, GitHubAPI, PAYLOAD, OWNER_ID
from test_github_followups import published, comment_args


from scripts.github_identity_fixture import IdentityProvider

@pytest.fixture
def identity(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    provider = IdentityProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'fixture-jwt')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(provider.handle), **kwargs))
    return app, client, run_id, headers, github, provider


def call(client, run_id, headers, name, **arguments):
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers, json={'name': name, 'arguments': arguments})
    assert response.status_code == 200, response.text
    return response.json()


def test_rename_updates_labels_without_changing_selection_or_authorization(identity):
    app, client, run, headers, github, provider = identity
    select(app, (202,))
    app.state.store.execute('UPDATE runs SET repo_url=? WHERE id=?', ('https://github.com/BerriAI/moyai', run))
    first = call(client, run, headers, 'github_checkout')
    version = github.connection_version()
    provider.repos[202]['full_name'] = 'BerriAI/renamed'
    provider.repos[202]['html_url'] = 'https://github.com/BerriAI/renamed'
    # Another repository occupies the old name, but is outside the saved selection.
    provider.repos[999] = {**repository_data('BerriAI/moyai'), 'id': 999}
    provider.installed.append(999)
    second = call(client, run, headers, 'github_checkout')
    assert first['repository_id'] == second['repository_id'] == 202
    assert first['git_path'] == second['git_path'] == '/github/repositories/202.git'
    assert second['repository'] == 'BerriAI/renamed'
    assert app.state.store.run(run)['github_repository_id'] == 202
    assert github.connection_version() == version
    assert github.saved_credentials()['repository_ids'] == [202]
    assert all(body['repository_ids'] == [202] for method, path, body in provider.calls if method == 'POST')
    assert not any(path.startswith('/repos/') for _, path, _ in provider.calls)


def test_legacy_migration_preserves_subset_and_resolves_redirect_once(identity):
    app, client, run, headers, github, provider = identity
    github.save_app({'id': 123, 'slug': 'fixture', 'pem': 'fixture-key'})
    old = {'kind': 'github_app', 'installation_id': 10, 'repositories': ['BerriAI/moyai-devin']}
    app.state.connectors.save('github', old, 'Legacy')
    app.state.store.execute('UPDATE runs SET repo_url=? WHERE id=?', ('https://github.com/BerriAI/moyai-devin', run))
    result = call(client, run, headers, 'github_checkout')
    assert result['repository_id'] == 202 and 'error' not in result
    assert github.saved_credentials() == credentials((202,))
    assert app.state.store.run(run)['github_repository_id'] == 202
    assert not github.tokens.keys() or all(key[2] == 202 for key in github.tokens)
    assert client.post('/api/connections/github/oauth').json() == {'connected': True}
    assert sum(path == '/repos/BerriAI/moyai-devin' for _, path, _ in provider.calls) == 1


@pytest.mark.parametrize('change', ['disconnect', 'reconnect', 'key'])
def test_concurrent_change_wins_over_legacy_migration(identity, change):
    app, client, run, headers, github, provider = identity
    app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/litellm'}, 'Legacy')
    def mutate(method, path):
        if path != '/repos/BerriAI/litellm':
            return
        if change == 'disconnect':
            app.state.store.execute("DELETE FROM connections WHERE provider='github'")
        elif change == 'reconnect':
            select(app, (202,))
        else:
            github.save_app({**github.app_config(), 'pem': 'rotated-fixture-key'})
    provider.on_request = mutate
    result = call(client, run, headers, 'github_checkout')
    assert 'changed' in result['error']
    if change == 'disconnect':
        assert github.saved_credentials() == {}
    elif change == 'reconnect':
        assert github.saved_credentials()['repository_ids'] == [202]
    else:
        assert 'repository_ids' not in github.saved_credentials()


def test_transferred_repository_is_rejected_before_write(identity):
    _, client, run, headers, _, provider = identity
    provider.repos[101]['owner'] = {'id': 999}
    result = call(client, run, headers, 'github_create_pull_request', **PAYLOAD)
    assert 'owner' in result['error']
    assert not any(method == 'POST' and '/access_tokens' not in path for method, path, _ in provider.calls)


def test_picker_is_admin_only_persists_ids_and_does_not_select_discovered_repos(identity):
    app, client, _, _, github, _ = identity
    options = client.get('/api/connections/github/repositories').json()
    assert options['selected_ids'] == [101]
    assert {r['id'] for r in options['repositories']} == {101, 202}
    assert github.saved_credentials()['repository_ids'] == [101]
    before = github.connection_version()
    assert client.post('/api/connections/github/repositories', json={'repository_ids': [202]}).status_code == 200
    assert github.saved_credentials()['repository_ids'] == [202]
    assert github.connection_version() != before
    for invalid in [[999], ['202'], [True], [], [-1]]:
        assert client.post('/api/connections/github/repositories', json={'repository_ids': invalid}).status_code in (403, 422)
    assert github.saved_credentials()['repository_ids'] == [202]
    cookie = app.state.security.signer.dumps({'sid': 'member', 'role': 'member', 'method': 'local'})
    client.cookies.clear(); client.cookies.set('workspace_session', cookie)
    assert client.get('/api/connections/github/repositories').status_code == 403
    assert client.post('/api/connections/github/repositories', json={'repository_ids': [101]}).status_code == 403


def test_discovery_paginates_and_never_changes_selection(identity):
    _, _, _, _, github, provider = identity
    provider.metadata_pages = [[{**repository_data('BerriAI/litellm'), 'id': i, 'full_name': f'BerriAI/repo-{i}'} for i in range(1000, 1100)],
                               [{**repository_data('BerriAI/moyai'), 'id': 1100}]]
    result = asyncio.run(github.discover_repositories(10))
    assert len(result['repository_ids']) == 101 and result['repository_ids'][-1] == 1100
    assert github.saved_credentials()['repository_ids'] == [101]
    assert sum(path == '/installation/repositories' for _, path, _ in provider.calls) == 2


def test_ambiguous_old_name_requires_an_id(identity):
    app, _, _, _, github, provider = identity
    select(app, (101, 202))
    github.remember_repository({**provider.repos[101], 'full_name': 'BerriAI/another'}, credentials((101, 202)))
    github.remember_repository({**provider.repos[202], 'full_name': 'BerriAI/litellm'}, credentials((101, 202)))
    with pytest.raises(ConnectorError, match='multiple'):
        github.target('BerriAI/litellm')
    assert github.target(101) == 101 and github.target(202) == 202


def test_event_filter_uses_repository_id_and_rejects_reused_name():
    trigger = EventTrigger(provider='github', repository='BerriAI/old', repository_id=202, event='issues')
    payload = {'action': 'opened', 'repository': {'id': 202, 'full_name': 'BerriAI/new'}, 'issue': {'number': 7, 'title': 'Issue'}}
    assert normalize(trigger, payload, 'issues')
    payload['repository'] = {'id': 999, 'full_name': 'BerriAI/old'}
    assert normalize(trigger, payload, 'issues') is None
    assert normalize(trigger.model_copy(update={'repository_id': None}), payload, 'issues') is None


def test_pr_ownership_and_idempotency_survive_rename(published, monkeypatch):
    app, client, run_data, headers, github, api = published
    run = run_data['id']
    # Fixture identity is unchanged while its display name changes.
    api.repository = 'BerriAI/renamed'
    original = api.request
    async def renamed(method, path, **kwargs):
        if path == '/repositories/101':
            return {**repository_data('BerriAI/litellm'), 'full_name': api.repository}
        return await original(method, path, **kwargs)
    monkeypatch.setattr(github, 'request', renamed)
    args = {**PAYLOAD, 'repository': '', 'repository_id': 101}
    result = call(client, run, headers, 'github_create_pull_request', **args)
    assert result['number'] == 100 and result['repository_id'] == 101
    assert result['repository'] == 'BerriAI/renamed' and '/BerriAI/renamed/pull/' in result['url']
    comment = comment_args().model_dump() | {'repository': '', 'repository_id': 101}
    assert 'error' not in call(client, run, headers, 'github_comment_pull_request', **comment)
    assert not any(m == 'POST' and p.endswith('/pulls') for m, p, _ in api.calls)


def test_numeric_git_route_refreshes_upstream_name_and_is_read_only(identity, monkeypatch):
    app, client, run, headers, github, provider = identity
    select(app, (202,))
    provider.repos[202]['full_name'] = 'BerriAI/renamed'
    paths = []
    original_handler = provider.handle
    def upstream(request):
        if request.url.host == 'api.github.com':
            return original_handler(request)
        paths.append(str(request.url))
        assert request.headers['authorization'].startswith('Basic ')
        return httpx.Response(200, content=b'0000', headers={'Content-Type': 'application/x-git-upload-pack-advertisement'})
    provider.handle = upstream
    base = f'/broker/{run}/github/repositories/202.git/'
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).content == b'0000'
    assert paths == ['https://github.com/BerriAI/renamed.git/info/refs?service=git-upload-pack']
    assert client.post(base + 'git-receive-pack', headers=headers).status_code == 403
    assert client.get(f'/broker/{run}/github/repositories/999.git/info/refs?service=git-upload-pack', headers=headers).status_code == 403


def test_completed_legacy_receipt_migrates_ownership_only_for_matching_connection(identity):
    app, _, run, _, github, provider = identity
    old = {'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/litellm'}
    app.state.connectors.save('github', old, 'Legacy')
    encrypted = app.state.store.rows("SELECT encrypted FROM connections WHERE provider='github'")[0]['encrypted']
    app_encrypted = app.state.store.rows('SELECT encrypted FROM github_app')[0]['encrypted']
    version = hashlib.sha256((encrypted + app_encrypted + json.dumps(['BerriAI/litellm'])).encode()).hexdigest()
    result = {'repository': 'BerriAI/litellm', 'number': 100, 'branch': 'moyai/legacy'}
    for identity_key, receipt_version in [('valid', version), ('revoked', 'other-connection')]:
        app.state.store.execute('''INSERT INTO github_publications(id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
            VALUES(?,?,0,'legacy','moyai/legacy',?,?, '2026-10-01')''', (identity_key, run, json.dumps(result), receipt_version))
    asyncio.run(github.ensure_connection())
    rows = {r['id']: r for r in app.state.store.rows('SELECT * FROM github_publications')}
    assert rows['valid']['connection_version'] == github.connection_version()
    assert rows['revoked']['connection_version'] == 'other-connection'
    assert json.loads(rows['valid']['result'])['repository_id'] == 101


def test_saved_environment_and_event_references_follow_id_after_rename(identity):
    app, _, run, _, github, provider = identity
    from app.environments import SaveRecipe, Recipe
    from app.automations import Definition, Save
    app.state.environments.save_recipe('e' * 32, SaveRecipe(recipe=Recipe(name='Project', repository='BerriAI/litellm', clone_access='github', verify='true')), 'admin')
    build = app.state.environments.enqueue('e' * 32, 1, 'admin')
    body = Save(definition=Definition(name='PR checks', prompt='Inspect new issues', repo_url='https://github.com/BerriAI/litellm',
        triggers=[{'id': 'issues', 'event': {'provider': 'github', 'repository': 'BerriAI/litellm', 'event': 'issues'}}]))
    row = app.state.automations.save(body, app.state.store.identity({'method': 'local'}))
    asyncio.run(github.ensure_connection())
    provider.repos[101]['full_name'] = 'BerriAI/renamed'
    asyncio.run(github.selected_target({}, repository_id=101))
    saved = json.loads(app.state.automations.row(row['id'])['definition'])
    assert saved['github_repository_id'] == saved['triggers'][0]['event']['repository_id'] == 101
    assert saved['repo_url'] == 'https://github.com/BerriAI/renamed'
    recipe = json.loads(app.state.environments.get('e' * 32)['recipe'])
    assert recipe['repository_id'] == 101 and recipe['repository'] == 'BerriAI/renamed'
    build_recipe = json.loads(app.state.environments.build(build['id'])['recipe'])
    assert build_recipe['repository_id'] == 101 and build_recipe['repository'] == 'BerriAI/litellm'
