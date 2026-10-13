"""Fault injection: a provider can accept a write before its reply fails."""
import copy
import asyncio

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.github import GitHub, Publish, GitHubError
from test_github import BASE, CURRENT, COMMIT, GitHubAPI, PAYLOAD, connected
from test_workspace import workspace


@pytest.mark.parametrize('failure', ['502', 'timeout', 'invalid_json'])
def test_accepted_pr_with_failed_http_reply_is_read_back(workspace, monkeypatch, failure):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    real_client = httpx.AsyncClient
    transport_calls = []

    def transport(request):
        transport_calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('Lost response', request=request)
        return httpx.Response(502 if failure == '502' else 201, text='not JSON')

    monkeypatch.setattr('app.github.httpx.AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))

    async def request(method, path, **kwargs):
        result = await api.request(method, path, **kwargs)
        if method == 'POST' and path.endswith('/pulls'):
            # The simulated provider already saved the PR, then its HTTP reply fails.
            return await GitHub.request(github, method, path, **kwargs)
        return result

    monkeypatch.setattr(github, 'request', request)
    body = {'name': 'github_create_pull_request', 'arguments': PAYLOAD}
    url = f'/broker/{run_id}/tools/call'
    result = client.post(url, headers=headers, json=body).json()
    assert result['number'] == 100 and result['url'] == api.pr['html_url']
    assert result['draft'] is False and 'error' not in result
    assert len(transport_calls) == 1
    assert [c[0] for c in api.calls if c[1].endswith('/pulls')] == ['GET', 'POST', 'GET']
    assert client.post(url, headers=headers, json=body).json() == result
    assert len(transport_calls) == 1
    assert github.owned_publication(101, 100, github.connection_version())['result']


@pytest.mark.parametrize('outcome', ['empty', 'read_error', 'multiple', 'wrong_sha', 'wrong_repo', 'wrong_branch', 'wrong_base', 'revoked'])
def test_unconfirmed_readback_remains_uncertain_without_second_post(workspace, monkeypatch, outcome):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = 'pr'
    recovery_reads = []

    async def request(method, path, **kwargs):
        if method == 'GET' and path.endswith('/pulls') and api.pr:
            recovery_reads.append(path)
            if outcome == 'read_error':
                raise ConnectorError('Readback unavailable')
            if outcome == 'empty':
                return []
            if outcome == 'multiple':
                return [api.pr, api.pr]
            pr = copy.deepcopy(api.pr)
            if outcome == 'wrong_sha':
                pr['head']['sha'] = '0' * 40
            if outcome == 'wrong_repo':
                pr['head']['repo']['id'] = 999
            if outcome == 'wrong_branch':
                pr['head']['ref'] = 'other-branch'
            if outcome == 'wrong_base':
                pr['base']['ref'] = 'other-base'
            if outcome == 'revoked':
                app.state.store.update_run(run_id, status='cancelled')
            return [pr]
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
    assert result['outcome_uncertain'] is True
    assert 'No creation retry was sent' in result['error']
    assert 'Lost response' in result['error']
    assert 'branch moyai/' in result['error']
    assert len(recovery_reads) == 1
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    assert not app.state.store.rows('SELECT result FROM github_publications')[0]['result']
    if outcome != 'revoked':
        retry = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                            json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
        assert retry['outcome_uncertain'] is True
        assert not app.state.store.rows('SELECT result FROM github_publications')[0]['result']
        assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1


@pytest.mark.parametrize('phase', ['blobs', 'trees', 'commits', 'refs', 'pulls'])
def test_two_brokers_claim_each_publication_effect_once(workspace, monkeypatch, phase):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    second = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(second, 'installation_token', github.installation_token)
    run, args = app.state.store.run(run_id), Publish.model_validate(PAYLOAD)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, path, **kwargs):
            if method == 'POST' and path.endswith('/' + phase):
                entered.set()
                await release.wait()
            return await api.request(method, path, **kwargs)

        monkeypatch.setattr(github, 'request', delayed)
        monkeypatch.setattr(second, 'request', delayed)
        first = asyncio.create_task(github.publish(run, args))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(ConnectorError, match='claimed|attempted|unconfirmed'):
                await asyncio.wait_for(second.publish(run, args), 5)
        finally:
            release.set()
        result = await first
        assert await second.publish(run, args) == result

    asyncio.run(exercise())
    assert len([entry for entry in api.calls if entry[0] == 'POST' and entry[1].endswith('/' + phase)]) == 1
    assert len([entry for entry in api.calls if entry[0] == 'POST' and entry[1].endswith('/pulls')]) == 1


def test_recovery_keeps_frozen_base_after_default_changes(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = 'pr'
    hidden = True

    async def request(method, path, **kwargs):
        if method == 'GET' and path.endswith('/pulls') and hidden:
            return []
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    run, args = app.state.store.run(run_id), Publish.model_validate(PAYLOAD)
    with pytest.raises(ConnectorError, match='unconfirmed'):
        asyncio.run(github.publish(run, args))
    api.default_branch = 'new-default'
    api.named_branches['new-default'] = CURRENT
    hidden = False
    result = asyncio.run(github.publish(run, args))
    assert result['base_branch'] == 'main'
    assert len([entry for entry in api.calls if entry[0] == 'POST' and entry[1].endswith('/pulls')]) == 1


@pytest.mark.parametrize('phase', ['blobs', 'refs'])
def test_claimed_missing_write_never_repeats_after_restart(workspace, monkeypatch, phase):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    attempts = []

    async def request(method, path, **kwargs):
        if method == 'POST' and path.endswith('/' + phase):
            attempts.append(path)
            raise ConnectorError('Lost response without readback')
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    run, args = app.state.store.run(run_id), Publish.model_validate(PAYLOAD)
    with pytest.raises(ConnectorError):
        asyncio.run(github.publish(run, args))
    restored = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(restored, 'request', request)
    monkeypatch.setattr(restored, 'installation_token', github.installation_token)
    with pytest.raises(ConnectorError, match='unconfirmed'):
        asyncio.run(restored.publish(run, args))
    assert len(attempts) == 1
    assert not app.state.store.rows('SELECT 1 FROM github_owned_branches')


def test_ref_rejection_cannot_adopt_foreign_branch_matching_source(workspace, monkeypatch):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    attempts = []

    async def request(method, path, **kwargs):
        if method == 'POST' and path.endswith('/git/refs'):
            attempts.append(path)
            api.named_branches['human/race'] = BASE
            raise GitHubError('Reference already exists', 422)
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    args = CreateBranch(repository_id=101, branch='human/race', source_sha=BASE, request_key='foreign-race-123')
    for _ in range(2):
        with pytest.raises(ConnectorError):
            asyncio.run(github.controlled_publication(app.state.store.run(run_id), args, 'github_create_branch'))
    assert len(attempts) == 1
    assert not app.state.store.rows('SELECT 1 FROM github_owned_branches')


def test_lost_ref_creation_reply_never_mints_branch_authority_from_readback(workspace, monkeypatch):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = 'branch'
    run = app.state.store.run(run_id)
    args = CreateBranch(repository_id=101, branch='feature/uncertain-origin', source_sha=BASE,
                        request_key='uncertain-origin-123')
    with pytest.raises(ConnectorError, match='Lost'):
        asyncio.run(github.controlled_publication(run, args, 'github_create_branch'))
    result = asyncio.run(github.controlled_publication(run, args, 'github_create_branch'))
    assert result['kind'] == 'branch' and result['branch_ownership_confirmed'] is False
    assert not github.branch_owned(101, args.branch, github.connection_version())
    assert not app.state.store.rows('SELECT * FROM github_owned_branches')
    assert len([call for call in api.calls if call[0] == 'POST' and call[1].endswith('/git/refs')]) == 1


def test_saved_create_ack_recovers_provenance_without_another_ref_write(workspace, monkeypatch):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    args = CreateBranch(repository_id=101, branch='feature/confirmed-origin', source_sha=BASE,
                        request_key='confirmed-origin-123')
    run = app.state.store.run(run_id)
    original = github.confirm_branch
    def interrupted(row, intent):
        raise ConnectorError('Crash before provenance persistence')
    monkeypatch.setattr(github, 'confirm_branch', interrupted)
    with pytest.raises(ConnectorError, match='Crash'):
        asyncio.run(github.controlled_publication(run, args, 'github_create_branch'))
    assert not github.branch_owned(101, args.branch, github.connection_version())
    monkeypatch.setattr(github, 'confirm_branch', original)
    api.calls.clear()
    result = asyncio.run(github.controlled_publication(run, args, 'github_create_branch'))
    assert result['commit'] == BASE and github.branch_owned(101, args.branch, github.connection_version())
    assert not api.mutations
