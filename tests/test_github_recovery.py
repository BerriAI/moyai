"""Fault injection: a provider can accept a write before its reply fails."""
import copy

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.github import GitHub
from test_github import GitHubAPI, PAYLOAD, connected
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
    assert github.owned_publication(app.state.store.run(run_id), PAYLOAD['repository'], 100, github.connection_version())['result']


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
                pr['head']['repo']['full_name'] = 'other/repository'
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
