"""Fault injection: a provider can accept a write before its reply fails."""
import copy

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.github import GitHub
from test_github import GitHubAPI, PAYLOAD, COMMIT, connected
from test_workspace import workspace


def fast_retries(monkeypatch):
    monkeypatch.setattr('app.github.RETRY_DELAYS', (0, 0, 0))


def real_github(github, monkeypatch, transport):
    real_client = httpx.AsyncClient
    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))


@pytest.mark.parametrize('failure', ['502', 'timeout', 'invalid_json'])
def test_accepted_pr_with_failed_http_reply_is_read_back(workspace, monkeypatch, failure):
    app, client = workspace
    fast_retries(monkeypatch)
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    transport_calls = []

    def transport(request):
        transport_calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('Lost response', request=request)
        return httpx.Response(502 if failure == '502' else 201, text='not JSON')

    real_github(github, monkeypatch, transport)

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


def test_definite_github_rejection_reports_message_and_never_retries(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    transport_calls = []

    def transport(request):
        transport_calls.append(request)
        return httpx.Response(422, json={'message': 'Validation Failed',
                                         'errors': [{'message': 'No commits between main and moyai/x'},
                                                    {'code': 'custom', 'field': 'head'}]})

    real_github(github, monkeypatch, transport)

    async def request(method, path, **kwargs):
        if method == 'POST' and path.endswith('/pulls'):
            # Definite rejection: nothing was created, so readback stays empty.
            return await GitHub.request(github, method, path, **kwargs)
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
    assert result['outcome_uncertain'] is False
    assert 'No commits between' in result['error']
    assert 'custom' in result['error']
    assert 'nothing was created' in result['instruction']
    assert len(transport_calls) == 1
    # A same-key retry is still allowed but hits the same definite rejection once.
    retry = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                        json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
    assert retry['outcome_uncertain'] is False and 'No commits between' in retry['error']
    assert len(transport_calls) == 2


def test_already_exists_422_recovers_the_pr_itself(workspace, monkeypatch):
    app, client = workspace
    fast_retries(monkeypatch)
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)

    def transport(request):
        return httpx.Response(422, json={'message': 'Validation Failed',
                                         'errors': [{'message': 'A pull request already exists for this branch.'}]})

    real_github(github, monkeypatch, transport)

    async def request(method, path, **kwargs):
        result = await api.request(method, path, **kwargs)
        if method == 'POST' and path.endswith('/pulls'):
            # The PR exists from an earlier lost attempt: POST is refused, the
            # next readback confirms it and no second POST is needed.
            return await GitHub.request(github, method, path, **kwargs)
        return result

    monkeypatch.setattr(github, 'request', request)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
    assert result['number'] == 100 and result['url'] == api.pr['html_url']
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1


def test_transient_creation_failures_retry_until_confirmed(workspace, monkeypatch):
    app, client = workspace
    fast_retries(monkeypatch)
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    transport_calls = []

    def transport(request):
        transport_calls.append(request)
        return httpx.Response(502, text='bad gateway')

    real_github(github, monkeypatch, transport)

    async def request(method, path, **kwargs):
        result = await api.request(method, path, **kwargs)
        if method == 'POST' and path.endswith('/pulls'):
            return await GitHub.request(github, method, path, **kwargs)
        return result

    monkeypatch.setattr(github, 'request', request)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': PAYLOAD}).json()
    # First POST's reply was lost; the retried readback confirmed the saved PR.
    assert result['number'] == 100
    assert len(transport_calls) == 1


def test_persistent_creation_failure_stays_uncertain_and_same_key_can_retry(workspace, monkeypatch):
    app, client = workspace
    fast_retries(monkeypatch)
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    failing = {'on': True}
    transport_calls = []

    def transport(request):
        transport_calls.append(request)
        return httpx.Response(502, text='bad gateway')

    real_github(github, monkeypatch, transport)

    async def request(method, path, **kwargs):
        if method == 'POST' and path.endswith('/pulls') and failing['on']:
            # The POST never reaches GitHub's state: transport fails first, so
            # api.pr stays unset and every readback keeps returning empty.
            return await GitHub.request(github, method, path, **kwargs)
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    body = {'name': 'github_create_pull_request', 'arguments': PAYLOAD}
    url = f'/broker/{run_id}/tools/call'
    result = client.post(url, headers=headers, json=body).json()
    assert result['outcome_uncertain'] is True
    assert 'same request_key is safe' in result['error']
    assert 'branch moyai/' in result['error']
    assert len(transport_calls) == 4
    assert not app.state.store.rows('SELECT result FROM github_publications')[0]['result']
    # Once the destination recovers, the same request_key completes normally.
    failing['on'] = False
    result = client.post(url, headers=headers, json=body).json()
    assert result['number'] == 100 and result['url'] == api.pr['html_url']
    assert app.state.store.rows('SELECT result FROM github_publications')[0]['result']


COMMIT_PAYLOAD = {**{k: v for k, v in PAYLOAD.items() if k != 'files'}, 'commit': COMMIT}


def test_missing_branch_returns_push_required(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': COMMIT_PAYLOAD}).json()
    assert result['push_required'] is True
    assert result['branch'].startswith('moyai/' + run_id[:12] + '/')
    assert not any(c[1].endswith('/pulls') for c in api.calls)
    # The row records the commit so the broker can authorize the push.
    row = app.state.store.rows('SELECT commit_sha FROM github_publications')[0]
    assert row['commit_sha'] == COMMIT


def test_pushed_commit_path_validates_ref_and_compare(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.branch = COMMIT  # The sandbox already pushed its commit to the branch.
    url = f'/broker/{run_id}/tools/call'
    result = client.post(url, headers=headers, json={'name': 'github_create_pull_request', 'arguments': COMMIT_PAYLOAD}).json()
    assert result['number'] == 100 and not api.deleted
    # A same-key retry returns the saved receipt without contacting GitHub.
    retry = client.post(url, headers=headers, json={'name': 'github_create_pull_request', 'arguments': COMMIT_PAYLOAD}).json()
    assert retry == result


@pytest.mark.parametrize('case', ['wrong_ref', 'protected', 'parent'])
def test_commit_path_rejections_are_definite(workspace, monkeypatch, case):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    if case == 'wrong_ref':
        api.branch = '0' * 40
    elif case == 'protected':
        api.branch = COMMIT
        api.compare_files = [{'filename': '.github/workflows/deploy.yml'}]
    else:
        api.parents = [{'sha': '9' * 40}]
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                         json={'name': 'github_create_pull_request', 'arguments': COMMIT_PAYLOAD}).json()
    assert result['outcome_uncertain'] is False and 'error' in result
    if case == 'wrong_ref':
        assert 'different commit' in result['error'] and 'new request_key' in result['error']
    elif case == 'protected':
        assert '.github/workflows/deploy.yml' in result['error'] and api.deleted
    else:
        assert 'one commit on top of the checkout base' in result['error']
    assert not any(c[1].endswith('/pulls') for c in api.calls)


@pytest.mark.parametrize('outcome', ['read_error', 'multiple', 'wrong_sha', 'wrong_repo', 'wrong_branch', 'wrong_base', 'revoked'])
def test_unconfirmed_readback_remains_uncertain(workspace, monkeypatch, outcome):
    app, client = workspace
    fast_retries(monkeypatch)
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = 'pr'
    recovery_reads = []

    async def request(method, path, **kwargs):
        if method == 'POST' and path.endswith('/pulls'):
            # GitHub saved the PR (readback sees it) but every ack is lost.
            await api.request(method, path, **kwargs)
            raise ConnectorError('Lost response')
        if method == 'GET' and path.endswith('/pulls') and api.pr:
            recovery_reads.append(path)
            if outcome == 'read_error':
                raise ConnectorError('Readback unavailable')
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
    assert 'same request_key is safe' in result['error']
    assert 'branch moyai/' in result['error']
    assert not app.state.store.rows('SELECT result FROM github_publications')[0]['result']
    assert recovery_reads
