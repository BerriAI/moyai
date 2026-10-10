"""CI reads through the real broker, permission scopes and HTTP adapter."""
import json

import httpx
import pytest

from app.github import PERMISSIONS
from app.github_ci import MAX_LOG_BYTES
from test_github import connected
from test_github_rulesets import Provider
from test_workspace import workspace

SHA = 'a' * 40
CI_TOOLS = {'github_ci_checks', 'github_workflow_runs', 'github_workflow_jobs', 'github_job_logs'}


class CIProvider(Provider):
    def __init__(self):
        super().__init__()
        self.log = b'first\npassword=hidden-password\nAuthorization: Bearer hidden-bearer\nlast\n'
        self.destination = 'https://test.blob.core.windows.net/logs/job?sig=private-signed-url'
        self.many = False
        self.log_status = 302
        self.on_result = None

    def handle(self, request):
        path = request.url.path
        if request.url.host != 'api.github.com':
            assert str(request.url) == self.destination
            assert 'authorization' not in request.headers and 'cookie' not in request.headers
            return httpx.Response(200, content=self.log)
        if '/actions/' not in path and '/commits/' not in path:
            return super().handle(request)
        self.calls.append((request.method, path, dict(request.url.params)))
        assert request.method == 'GET' and path.startswith('/repositories/101/')
        permissions = self.tokens[request.headers['authorization'].removeprefix('Bearer ')]
        # Enforce GitHub's endpoint permission contract independently of the
        # requested token scopes. Contents read does not grant status access.
        required = 'checks' if path.endswith('/check-runs') else 'statuses' if path.endswith('/statuses') else 'actions'
        if permissions.get(required) not in ('read', 'write'):
            return httpx.Response(403, json={'message': 'Resource not accessible by integration'},
                                  headers={'X-Accepted-GitHub-Permissions': required + '=read'})
        if self.on_result:
            self.on_result()
        count = 30 if self.many else 1
        if path.endswith('/check-runs'):
            assert SHA in path and request.url.params['filter'] == 'latest'
            return httpx.Response(200, json={'check_runs': [{'id': 5, 'name': 'test', 'status': 'completed',
                'conclusion': 'failure', 'head_sha': SHA}] * count})
        if path.endswith('/statuses'):
            return httpx.Response(200, json=[{'id': 6, 'context': 'external-ci', 'state': 'pending'}] * count)
        if path.endswith('/actions/runs'):
            return httpx.Response(200, json={'workflow_runs': [{'id': 7, 'head_sha': SHA,
                'run_attempt': 2, 'status': 'completed', 'conclusion': 'failure'}] * count})
        if path.endswith('/actions/runs/7/jobs'):
            return httpx.Response(200, json={'jobs': [{'id': 8, 'run_id': 7, 'run_attempt': 2,
                'status': 'completed', 'conclusion': 'failure', 'steps': [
                    {'number': 1, 'name': 'test', 'conclusion': 'failure'}]}] * count})
        assert path.endswith('/actions/jobs/8/logs')
        return httpx.Response(self.log_status, headers={'location': self.destination,
            'set-cookie': 'private=api-cookie; Domain=.blob.core.windows.net; Path=/'})


@pytest.fixture
def ci(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    provider = CIProvider()
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'server-jwt')
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(provider.handle), **kwargs))
    def call(name, **args):
        return client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': name, 'arguments': args})
    return app, client, run_id, headers, github, provider, call


def test_ci_catalog_and_reads_work_with_read_only_connection(ci):
    app, client, run_id, headers, _, provider, call = ci
    app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    catalog = client.get(f'/broker/{run_id}/tools', headers=headers).json()
    assert CI_TOOLS <= {tool['name'] for tool in catalog}
    for tool in catalog:
        if tool['name'] in CI_TOOLS:
            assert tool['annotations']['readOnlyHint'] and tool['annotations']['idempotentHint']
    checks = call('github_ci_checks', head_sha=SHA).json()
    assert checks['check_runs'][0]['conclusion'] == 'failure'
    assert checks['statuses'][0]['state'] == 'pending'
    assert checks['head_sha'] == SHA and checks['next_page'] is None
    runs = call('github_workflow_runs', head_sha=SHA, branch='feature/ci').json()
    assert runs['workflow_runs'][0]['run_attempt'] == 2
    assert provider.calls[-1][2]['head_sha'] == SHA
    jobs = call('github_workflow_jobs', run_id=7).json()
    assert jobs['jobs'][0]['steps'][0]['conclusion'] == 'failure'
    assert {'checks': 'read', 'statuses': 'read'} in provider.minted
    assert {'actions': 'read'} in provider.minted
    assert not any(method not in ('GET', 'POST') for method, _, _ in provider.calls)
    assert all(path.endswith('/access_tokens') for method, path, _ in provider.calls if method == 'POST')


def test_ci_pagination(ci):
    _, _, _, _, _, provider, call = ci
    provider.many = True
    for name, args in [('github_ci_checks', {'head_sha': SHA}), ('github_workflow_runs', {}),
                       ('github_workflow_jobs', {'run_id': 7})]:
        assert call(name, page=2, **args).json()['next_page'] == 3


def test_log_excerpt_redacts_before_slicing_and_drops_download_credentials(ci):
    _, _, _, _, _, provider, call = ci
    result = call('github_job_logs', job_id=8, start_line=2, max_lines=2).json()
    assert result['next_line'] == 4 and result['truncated']
    assert 'password=[redacted]' in result['text']
    assert 'hidden-' not in json.dumps(result) and 'server-token' not in json.dumps(result)
    assert 'private-signed-url' not in json.dumps(result) and result['untrusted_content']
    assert call('github_job_logs', job_id=8, start_line=4).json()['text'] == 'last'
    provider.log = b'good\n' + b'x' * MAX_LOG_BYTES + b'secret-at-cut'
    bounded = call('github_job_logs', job_id=8).json()
    assert bounded['download_truncated'] and bounded['text'] == 'good'


@pytest.mark.parametrize('destination', ['http://test.blob.core.windows.net/a', 'https://api.github.com/a',
    'https://test.blob.core.windows.net.attacker.test/a', 'https://127.0.0.1/private',
    'https://user:password@test.blob.core.windows.net/a', 'https://test.blob.core.windows.net:8443/a'])
def test_job_logs_reject_unexpected_redirects(ci, destination):
    *_, provider, call = ci
    provider.destination = destination
    assert 'unexpected job-log destination' in call('github_job_logs', job_id=8).json()['error']


@pytest.mark.parametrize('status', [403, 404, 410])
def test_unavailable_logs_are_a_tool_error(ci, status):
    *_, provider, call = ci
    provider.log_status = status
    result = call('github_job_logs', job_id=8).json()
    assert f'({status})' in result['error'] and 'text' not in result


def test_missing_permissions_do_not_disable_existing_pr_access(ci):
    *_, provider, call = ci
    provider.permissions = PERMISSIONS
    assert 'Checks: read' in call('github_ci_checks', head_sha=SHA).json()['error']
    assert 'Actions: read' in call('github_workflow_runs').json()['error']
    assert all(scope == {'metadata': 'read'} for scope in provider.minted)


def test_missing_statuses_permission_is_actionable_and_preserves_actions(ci):
    *_, provider, call = ci
    provider.permissions.pop('statuses', None)
    result = call('github_ci_checks', head_sha=SHA).json()
    assert 'Commit statuses: read' in result['error']
    assert 'organization owner' in result['error']
    assert not any('/commits/' in path for _, path, _ in provider.calls)
    assert call('github_workflow_runs', head_sha=SHA).json()['workflow_runs'][0]['id'] == 7


def test_ci_repository_scope_and_revocation_are_rechecked(ci):
    app, _, _, _, _, provider, call = ci
    assert 'error' in call('github_workflow_runs', repository_id=202).json()
    provider.on_result = lambda: app.state.store.execute("DELETE FROM connections WHERE provider='github'")
    result = call('github_workflow_runs').json()
    assert 'error' in result and 'workflow_runs' not in result


@pytest.mark.parametrize('tool,args', [('github_ci_checks', {'head_sha': '../main'}),
    ('github_workflow_jobs', {'run_id': -1}), ('github_job_logs', {'job_id': True}),
    ('github_job_logs', {'job_id': 8, 'start_line': 0})])
def test_ci_arguments_cannot_construct_arbitrary_paths(ci, tool, args):
    *_, provider, call = ci
    response = call(tool, **args)
    assert response.status_code == 422 or 'error' in response.json()
    assert not provider.calls
