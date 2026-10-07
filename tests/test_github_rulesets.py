"""Ruleset tools through the real broker and GitHub HTTP client; no live writes."""
import asyncio
import copy
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.github import PERMISSIONS, MANIFEST_PERMISSIONS, supports_permissions
from app.security import digest
from test_github import connected, repository_data
from test_workspace import workspace

REPO = 'BerriAI/litellm'
REVIEWERS = [
    {'reviewer': {'id': 7, 'type': 'Team'}, 'file_patterns': ['*'], 'minimum_approvals': 1},
    {'reviewer': {'id': 7, 'type': 'Team'}, 'file_patterns': ['.github/**'], 'minimum_approvals': 1},
]
RULESET = {
    'id': 42, 'name': 'guard-main', 'source_type': 'Repository', 'source': REPO,
    'target': 'branch', 'enforcement': 'active', 'updated_at': '2026-10-05T20:00:00Z',
    'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
    'bypass_actors': [{'actor_type': 'Team', 'actor_id': 99, 'bypass_mode': 'pull_request'}],
    'rules': [
        {'type': 'deletion'}, {'type': 'non_fast_forward'},
        {'type': 'pull_request', 'parameters': {
            'required_approving_review_count': 1, 'require_code_owner_review': True,
            'dismiss_stale_reviews_on_push': False, 'require_last_push_approval': False,
            'required_review_thread_resolution': True, 'allowed_merge_methods': ['squash'],
            'required_reviewers': REVIEWERS,
        }},
        {'type': 'required_status_checks', 'parameters': {
            'strict_required_status_checks_policy': True,
            'required_status_checks': [{'context': 'lint', 'integration_id': 15368}],
        }},
    ],
}


class Provider:
    def __init__(self):
        self.permissions = {**MANIFEST_PERMISSIONS, 'issues': 'write', 'workflows': 'write'}
        self.ruleset = copy.deepcopy(RULESET)
        self.calls, self.tokens, self.minted = [], {}, []
        self.lose_response = self.corrupt_readback = self.many = False
        self.on_read = None

    def handle(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if path == '/orgs/BerriAI':
            return httpx.Response(200, json={'id': 44, 'login': 'BerriAI', 'type': 'Organization'})
        if path == '/app':
            return httpx.Response(200, json={'id': 123, 'slug': 'moyai-test',
                'owner': {'id': 44, 'login': 'BerriAI', 'type': 'Organization'}, 'permissions': self.permissions})
        if path == '/app/installations/10':
            return httpx.Response(200, json={'account': {'id': 44, 'login': 'BerriAI', 'type': 'Organization'},
                                           'permissions': self.permissions, 'suspended_at': None})
        if path.endswith('/access_tokens'):
            assert body['repository_ids'] == [101]
            assert supports_permissions(self.permissions, body['permissions'])
            token = f'server-token-{len(self.tokens)}'
            self.tokens[token] = body['permissions']
            self.minted.append(body['permissions'])
            return httpx.Response(201, json={'token': token, 'expires_at': '2099-01-01T00:00:00Z'})
        permissions = self.tokens[request.headers['authorization'].removeprefix('Bearer ')]
        if path == '/installation/repositories':
            return httpx.Response(200, json={'repositories': [repository_data(REPO)]})
        if path == '/repositories/101':
            return httpx.Response(200, json=repository_data(REPO))
        if path == '/repositories/101/rulesets':
            assert request.url.params['includes_parents'] == 'true'
            assert request.url.params['per_page'] == '30'
            org_rule = {**self.ruleset, 'id': 43, 'source': 'BerriAI', 'source_type': 'Organization'}
            return httpx.Response(200, json=[self.ruleset] * 30 if self.many else [self.ruleset, org_rule])
        assert path == '/repositories/101/rulesets/42'
        if method == 'PUT':
            assert permissions == {'administration': 'write'}
            assert list(body) == ['rules']
            self.ruleset['rules'] = body['rules']
            self.ruleset['updated_at'] = '2026-10-05T20:01:00Z'
            if self.corrupt_readback:
                self.ruleset['rules'][2]['parameters']['required_approving_review_count'] = 0
            if self.lose_response:
                return httpx.Response(500, json={'message': 'Uncertain provider result'})
        else:
            assert method == 'GET' and request.url.params['includes_parents'] == 'true'
            if self.on_read:
                self.on_read()
        result = copy.deepcopy(self.ruleset)
        if 'administration' not in permissions:
            result.pop('bypass_actors')
        return httpx.Response(200, json=result)

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == 'PUT']


@pytest.fixture
def rules(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    provider = Provider()
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'server-jwt')
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(provider.handle), **kwargs))
    def call(name, args=None):
        return client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': name, 'arguments': args or {}})
    return app, client, run_id, headers, github, provider, call


def edit_args(call):
    detail = call('github_ruleset', {'ruleset_id': 42}).json()
    assert 'error' not in detail
    return {'repository': REPO, 'ruleset_id': 42, 'revision': detail['revision'],
            'required_reviewers': REVIEWERS[1:]}


def test_extra_app_permissions_accepted_without_changing_pr_tokens(rules):
    app, client, _, _, github, provider, _ = rules
    assert asyncio.run(github.verify(github.saved_credentials())) == REPO
    result = client.post('/api/connections/github/app', json={'app_id': 123, 'private_key': 'local-fixture-key' * 20})
    assert result.status_code == 200
    asyncio.run(github.installation_token(repository=REPO, write=True))
    assert provider.minted == [{'contents': 'read', 'pull_requests': 'read'},
                               {'contents': 'write', 'pull_requests': 'write'}]
    assert 'server-token' not in client.get('/api/connections').text


def test_new_manifest_requests_administration_and_accepts_broader_conversion(workspace, monkeypatch):
    app, client = workspace
    async def organization(*args, **kwargs):
        return {'id': 44, 'login': 'BerriAI', 'type': 'Organization'}
    monkeypatch.setattr(app.state.connectors.github, 'request', organization)
    start = client.post('/api/connections/github/oauth', json={'organization': 'BerriAI'}).json()['url']
    page = client.get(start)
    assert 'administration' in page.text and 'Administration write access' in page.text
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'fixture-jwt')
    async def convert(*args, **kwargs):
        return {'owner': {'id': 44, 'login': 'BerriAI'}, 'permissions': {**MANIFEST_PERMISSIONS, 'issues': 'write'},
                'id': 123, 'slug': 'moyai-test', 'pem': 'fixture-key'}
    monkeypatch.setattr(github, 'request', convert)
    state = parse_qs(urlparse(start).query)['state'][0]
    assert client.get(f'/oauth/github/app-callback?state={state}&code=temporary-registration-code',
                      follow_redirects=False).status_code == 303


def test_metadata_only_inspection_lists_inherited_rules_and_paginates(rules):
    _, _, _, _, _, provider, call = rules
    provider.permissions = PERMISSIONS
    listing = call('github_rulesets').json()
    assert listing['rulesets'][1]['source_type'] == 'Organization'
    assert listing['next_page'] is None
    provider.many = True
    assert call('github_rulesets', {'page': 2}).json()['next_page'] == 3
    detail = call('github_ruleset', {'ruleset_id': 42}).json()
    assert detail['ruleset']['rules'][2]['parameters']['required_reviewers'] == REVIEWERS
    assert len(detail['revision']) == 64
    assert provider.minted == [{'metadata': 'read'}]
    assert not provider.writes


def test_remove_wildcard_preserves_narrower_reviewers_and_all_other_settings(rules):
    _, _, _, _, github, provider, call = rules
    args = edit_args(call)
    result = call('github_update_ruleset_reviewers', args).json()
    assert result['changed'] is True
    expected = copy.deepcopy(RULESET)
    expected['rules'][2]['parameters']['required_reviewers'] = REVIEWERS[1:]
    expected['updated_at'] = provider.ruleset['updated_at']
    assert provider.ruleset == expected
    assert len(provider.writes) == 1
    assert provider.minted == [{'metadata': 'read'}, {'administration': 'write'}]
    # Administration tokens are never reused for checkout or PR operations.
    asyncio.run(github.installation_token(repository=REPO, write=True))
    assert provider.minted[-1] == {'contents': 'write', 'pull_requests': 'write'}
    args['revision'] = result['revision']
    assert call('github_update_ruleset_reviewers', args).json()['changed'] is False
    assert len(provider.writes) == 1
    assert 'server-token' not in json.dumps(result)


def test_explicit_empty_list_removes_all_team_requirements_only(rules):
    _, _, _, _, _, provider, call = rules
    args = {**edit_args(call), 'required_reviewers': []}
    result = call('github_update_ruleset_reviewers', args).json()
    assert result['changed'] is True
    expected = copy.deepcopy(RULESET)
    expected['rules'][2]['parameters']['required_reviewers'] = []
    expected['updated_at'] = provider.ruleset['updated_at']
    assert provider.ruleset == expected
    assert len(provider.writes) == 1


@pytest.mark.parametrize('change', ['revision', 'organization', 'tag', 'missing_pr', 'id', 'other_repo'])
def test_conflicts_and_unsupported_targets_send_no_update(rules, change):
    _, _, _, _, _, provider, call = rules
    args = edit_args(call)
    if change == 'revision':
        provider.ruleset['rules'][2]['parameters']['required_approving_review_count'] = 2
    elif change == 'organization':
        provider.ruleset.update(source_type='Organization', source='BerriAI')
    elif change == 'tag':
        provider.ruleset['target'] = 'tag'
    elif change == 'missing_pr':
        provider.ruleset['rules'] = []
        args['revision'] = call('github_ruleset', {'ruleset_id': 42}).json()['revision']
    elif change == 'id':
        provider.ruleset['id'] = 43
    else:
        args['repository'] = 'BerriAI/other'
    result = call('github_update_ruleset_reviewers', args).json()
    assert 'error' in result and not provider.writes


def test_missing_admin_permission_is_actionable_and_reads_still_work(rules):
    _, _, _, _, _, provider, call = rules
    provider.permissions = PERMISSIONS
    args = edit_args(call)
    result = call('github_update_ruleset_reviewers', args).json()
    assert 'Administration: read and write' in result['error']
    assert 'approve' in result['error']
    assert not provider.writes
    assert 'error' not in call('github_ruleset', {'ruleset_id': 42}).json()
    assert provider.minted == [{'metadata': 'read'}]


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_connection_and_run_restrictions_block_edits(rules, restriction):
    app, client, run_id, headers, _, provider, call = rules
    args = edit_args(call)
    if restriction in {'read_only', 'disabled'}:
        app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                ('github', restriction != 'disabled', restriction == 'read_only'))
    elif restriction == 'disconnect':
        app.state.store.execute("DELETE FROM connections WHERE provider='github'")
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    else:
        app.state.store.update_run(run_id, token_hash=digest('changed'))
    response = call('github_update_ruleset_reviewers', args)
    assert response.status_code in {401, 403} and not provider.writes
    if restriction == 'read_only':
        names = [tool['name'] for tool in client.get(f'/broker/{run_id}/tools', headers=headers).json()]
        assert 'github_ruleset' in names and 'github_update_ruleset_reviewers' not in names


@pytest.mark.parametrize('restriction', ['read_only', 'cancelled', 'reconnected'])
def test_revocation_during_read_stops_write(rules, restriction):
    app, _, run_id, _, _, provider, call = rules
    args = edit_args(call)
    def revoke():
        if restriction == 'read_only':
            app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
        elif restriction == 'cancelled':
            app.state.store.update_run(run_id, status='cancelled')
        else:
            app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 20, 'repository': REPO}, 'Changed')
    provider.on_read = revoke
    assert 'changed' in call('github_update_ruleset_reviewers', args).json()['error']
    assert not provider.writes


@pytest.mark.parametrize('failure', ['lost_response', 'bad_readback'])
def test_uncertain_update_never_retries_or_claims_success(rules, failure):
    _, _, _, _, _, provider, call = rules
    args = edit_args(call)
    provider.lose_response = failure == 'lost_response'
    provider.corrupt_readback = failure == 'bad_readback'
    result = call('github_update_ruleset_reviewers', args).json()
    assert 'error' in result and result['outcome_uncertain'] is True
    assert len(provider.writes) == 1
    provider.lose_response = provider.corrupt_readback = False
    assert 'error' in call('github_update_ruleset_reviewers', args).json()
    assert len(provider.writes) == 1
    if failure == 'lost_response':
        args['revision'] = call('github_ruleset', {'ruleset_id': 42}).json()['revision']
        assert call('github_update_ruleset_reviewers', args).json()['changed'] is False
        assert len(provider.writes) == 1


@pytest.mark.parametrize('change', ['omitted_reviewers', 'extra_field', 'reviewer_type', 'empty_pattern'])
def test_invalid_edits_are_rejected_at_broker_boundary(rules, change):
    _, _, _, _, _, provider, call = rules
    args = edit_args(call)
    if change == 'omitted_reviewers':
        args.pop('required_reviewers')
    elif change == 'extra_field':
        args['required_approving_review_count'] = 0
    else:
        args['required_reviewers'] = copy.deepcopy(REVIEWERS)
        if change == 'reviewer_type':
            args['required_reviewers'][0]['reviewer']['type'] = 'User'
        else:
            args['required_reviewers'][0]['file_patterns'] = ['']
    assert call('github_update_ruleset_reviewers', args).status_code == 422
    assert not provider.writes
