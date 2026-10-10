"""Workflow publication exercises real token scoping against a simulated provider."""
import asyncio

import pytest

from app.connector_errors import ConnectorError
from app.github import Change, GitHub, PERMISSIONS, Publish, Update, supports_permissions, workflow_changes
from test_github import PAYLOAD, REPO_IDS, connected, repository_data, select
from test_github_followups import FollowupAPI, update_args
from test_github_write_access import approved, call, external
from test_workspace import workspace


def token_service(github, api, monkeypatch, allowed=True):
    """Keep publication/provider behavior but restore the real token boundary."""
    request = api.request
    scopes, minted = {}, []
    permissions = {**PERMISSIONS, **({'workflows': 'write'} if allowed else {})}

    async def service(method, path, **kwargs):
        if path == '/app/installations/10':
            return {'account': {'id': 44, 'type': 'Organization'}, 'permissions': permissions}
        if path.endswith('/access_tokens'):
            scope = kwargs['json']
            assert supports_permissions(permissions, scope['permissions'])
            token = 'server-token-' + str(len(minted))
            scopes[token] = scope
            minted.append(scope)
            return {'token': token, 'expires_at': '2099-01-01T00:00:00Z'}
        if path == '/installation/repositories':
            ids = scopes[kwargs['token']]['repository_ids']
            return {'repositories': [repository_data(name) for name, rid in REPO_IDS.items() if rid in ids]}
        if method in {'POST', 'PATCH'} and path.startswith('/repositories/'):
            scope = scopes[kwargs['token']]
            assert scope['repository_ids'] == [int(path.split('/')[2])]
            assert scope['permissions']['contents'] == 'write'
        return await request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', service)
    monkeypatch.setattr(github, 'installation_token', GitHub.installation_token.__get__(github))
    return scopes, minted


@pytest.mark.parametrize('path', ['.github/workflows/ci.yml', '.github/actions/test/action.yml',
                                  '/'.join(['nested']*30)+'/test.py'])
def test_publish_and_update_use_only_required_repository_scope(workspace, monkeypatch, path):
    app, _ = workspace
    rid, _ = connected(app)
    github = app.state.connectors.github
    api = FollowupAPI(github, monkeypatch)
    scopes, minted = token_service(github, api, monkeypatch)
    files = [{'path': path, 'content': 'before'}]
    args = Publish.model_validate({**PAYLOAD, 'files': files})
    result = asyncio.run(github.publish(app.state.store.run(rid), args))
    assert result['number'] == 100
    update = Update.model_validate({**update_args().model_dump(), 'files': [{'path': path, 'content': 'after'}]})
    result = asyncio.run(github.update(app.state.store.run(rid), update))
    assert result['commit'] == api.next_commit
    for method, url, kwargs in api.calls:
        if method in {'POST', 'PATCH'}:
            assert scopes[kwargs['token']]['permissions'] == {
                'contents': 'write', 'pull_requests': 'write',
                **({'workflows': 'write'} if path.startswith('.github/workflows/') else {})}
    # A workflow token must never be reused for an ordinary write or checkout.
    ordinary = asyncio.run(github.installation_token(repository=101, write=True))
    readonly = asyncio.run(github.installation_token(repository=101))
    assert scopes[ordinary]['permissions'] == {'contents': 'write', 'pull_requests': 'write'}
    assert scopes[readonly]['permissions'] == {'contents': 'read', 'pull_requests': 'read'}
    assert not any('administration' in scope['permissions'] for scope in minted)


@pytest.mark.parametrize('operation', ['publish', 'update'])
def test_missing_permission_precedes_repository_mutations_and_can_retry(workspace, monkeypatch, operation):
    app, _ = workspace
    rid, _ = connected(app)
    github = app.state.connectors.github
    api = FollowupAPI(github, monkeypatch)
    if operation == 'update':
        asyncio.run(github.publish(app.state.store.run(rid), Publish.model_validate(PAYLOAD)))
    token_service(github, api, monkeypatch, allowed=False)
    api.calls.clear()
    files = [Change(path='.github/workflows/ci.yml', content='name: CI')]
    args = (Publish.model_validate(PAYLOAD) if operation == 'publish' else update_args()).model_copy(update={'files': files})
    with pytest.raises(ConnectorError, match='Workflows: read and write'):
        asyncio.run(getattr(github, operation)(app.state.store.run(rid), args))
    assert all(method == 'GET' for method, _, _ in api.calls)
    github.tokens.clear()
    token_service(github, api, monkeypatch, allowed=True)
    result = asyncio.run(getattr(github, operation)(app.state.store.run(rid), args))
    assert result['number'] == 100


@pytest.mark.parametrize('head', [101, 202])
def test_external_workflow_write_scopes_head_repository_after_human_grant(external, monkeypatch, head):
    app, _, _, _, github, api = external
    select(app, (101, 202))
    api.pr['head']['repo']['id'] = head
    approved(external)
    scopes, _ = token_service(github, api, monkeypatch)
    args = {**update_args().model_dump(), 'files': [{'path': '.github/workflows/ci.yml', 'content': 'name: CI'}]}
    response = call(external, 'github_update_pull_request', args)
    assert response.json()['commit'] == api.next_commit, response.text
    writes = [entry for entry in api.calls if entry[0] == 'PATCH']
    assert len(writes) == 1 and writes[0][1].startswith(f'/repositories/{head}/')
    assert scopes[writes[0][2]['token']] == {'repository_ids': [head], 'permissions': {
        'contents': 'write', 'pull_requests': 'write', 'workflows': 'write'}}


def test_workflow_deletion_also_requires_scope():
    args = Publish.model_validate({**PAYLOAD, 'files': [{'path': '.github/workflows/ci.yml', 'content': None}]})
    assert workflow_changes(args)
