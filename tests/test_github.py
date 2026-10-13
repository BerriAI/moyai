import asyncio
import copy
import json
import time
from urllib.parse import parse_qs, urlparse
from urllib.parse import unquote

import httpx
import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import Change, Publish, PERMISSIONS
from app.github_publications import CLASSIC_PROTECTION_QUERY, classic_pattern_matches, classic_creation_restricted
from app.security import digest
from test_workspace import workspace, cloud_capability
from test_spend import sign_in

REPO_IDS = {'BerriAI/litellm': 101, 'BerriAI/moyai': 202}
OWNER_ID = 44


def credentials(ids=(101,)):
    return {'kind': 'github_app', 'installation_id': 10, 'account_id': OWNER_ID, 'repository_ids': list(ids)}


def repository_data(name):
    return {'id': REPO_IDS[name], 'node_id': f'R_{REPO_IDS[name]}', 'full_name': name, 'owner': {'id': OWNER_ID},
            'default_branch': 'main', 'html_url': 'https://github.com/' + name, 'private': True}


def select(app, ids=(101,)):
    github = app.state.connectors.github
    for name, identity in REPO_IDS.items():
        github.remember_repository(repository_data(name), credentials(ids))
    app.state.connectors.save('github', credentials(ids), 'Selected repositories')


BASE = 'a' * 40
CURRENT = 'b' * 40
TREE = 'c' * 40
COMMIT = 'd' * 40
PAYLOAD = {'repository': 'BerriAI/litellm', 'base_sha': BASE, 'request_key': 'fix-test-123',
           'title': 'Fix the tested behavior', 'body': 'A reviewed change. Validation: tests passed.',
           'files': [{'path': 'fix.py', 'content': 'answer = 42\n', 'executable': False}]}


def connected(app):
    run_id, headers = cloud_capability(app, ['github'])
    app.state.connectors.github.save_app({'id': 123, 'slug': 'moyai-test', 'pem': 'private-signing-key', 'owner_id': OWNER_ID})
    select(app)
    return run_id, headers


class GitHubAPI:
    def __init__(self, github, monkeypatch, repository="BerriAI/litellm"):
        self.github, self.calls, self.repository = github, [], repository
        self.repository_id = REPO_IDS[repository]
        self.token_repository = self.repository_id
        monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
        self.branch = None
        self.pr = None
        self.tree = []
        self.lose = ''
        self.status = 'ahead'
        self.on_call = None
        self.default_branch = 'main'
        self.named_branches = {'main': CURRENT}
        self.protected = set()
        self.rules = {}
        self.classic_rules = []
        monkeypatch.setattr(github, 'request', self.request)
        async def token(*args, **kwargs):
            self.token_repository = github.target(kwargs.get('repository', ''))
            return 'installation-secret-never-in-sandbox'
        monkeypatch.setattr(github, 'installation_token', token)

    @property
    def mutations(self):
        return [call for call in self.calls if call[0] != 'GET' and not (
            call[0] == 'POST' and call[1] == '/graphql' and call[2].get('json', {}).get('query') == CLASSIC_PROTECTION_QUERY)]

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.on_call:
            self.on_call(method, path)
        prefix = '/repositories/' + str(self.repository_id)
        if method == 'POST' and path == '/graphql':
            assert kwargs['json']['query'] == CLASSIC_PROTECTION_QUERY
            owner, name = self.repository.split('/')
            assert kwargs['json']['variables'] == {'owner': owner, 'name': name, 'cursor': None}
            return {'data': {'repository': {'databaseId': self.repository_id, 'branchProtectionRules': {
                'nodes': copy.deepcopy(self.classic_rules), 'totalCount': len(self.classic_rules),
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
        if path == '/app/installations/10':
            return {'account': {'id': OWNER_ID, 'type': 'Organization'}, 'permissions': PERMISSIONS}
        if path == '/app/installations/10/access_tokens':
            assert kwargs['json']['permissions'] == {'metadata': 'read'}
            self.metadata_ids = kwargs['json']['repository_ids']
            return {'token': 'metadata-fixture'}
        if path == '/installation/repositories':
            if kwargs['token'] == 'metadata-fixture':
                return {'repositories': [repository_data(n) for n, i in REPO_IDS.items() if i in self.metadata_ids]}
            name = next(n for n, i in REPO_IDS.items() if i == self.token_repository)
            return {'repositories': [repository_data(name)]}
        if path == prefix:
            return {**repository_data(self.repository), 'id': self.repository_id, 'default_branch': self.default_branch}
        if path in ['/repositories/' + str(i) for i in REPO_IDS.values()]:
            return repository_data(next(n for n, i in REPO_IDS.items() if path == '/repositories/' + str(i)))
        if path.startswith(prefix + '/branches/'):
            name = unquote(path.split('/branches/', 1)[1])
            sha = self.branch if name.startswith('moyai/') else self.named_branches.get(name)
            return {'name': name, 'protected': name in self.protected, 'commit': {'sha': sha}} if sha else None
        if path.startswith(prefix + '/rules/branches/'):
            return self.rules.get(unquote(path.split('/rules/branches/', 1)[1]), [])
        if path.startswith(prefix + '/git/ref/heads/'):
            name = unquote(path.split('/git/ref/heads/', 1)[1])
            sha = self.branch if name.startswith('moyai/') else self.named_branches.get(name)
            return {'ref': 'refs/heads/' + name, 'object': {'sha': sha, 'type': 'commit'}} if sha else None
        if '/compare/' in path:
            return {'status': self.status}
        if path == prefix + '/git/commits/' + BASE:
            return {'sha': BASE, 'tree': {'sha': TREE}}
        if method == 'GET' and '/git/trees/' in path:
            return {'tree': self.tree}
        if method == 'POST' and path.endswith('/git/blobs'):
            return {'sha': 'f' * 40}
        if method == 'POST' and path.endswith('/git/trees'):
            return {'sha': 'e' * 40}
        if method == 'POST' and path.endswith('/git/commits'):
            return {'sha': COMMIT}
        if method == 'PATCH' and '/git/refs/heads/' in path:
            assert kwargs['json']['force'] is False
            name = unquote(path.split('/git/refs/heads/', 1)[1])
            self.branch = kwargs['json']['sha']
            self.named_branches[name] = self.branch
            return {}
        if method == 'GET' and '/git/ref/heads/moyai/' in path:
            return {'object': {'sha': self.branch}} if self.branch else None
        if method == 'POST' and path.endswith('/git/refs'):
            self.branch = kwargs['json']['sha']
            self.named_branches[kwargs['json']['ref'].removeprefix('refs/heads/')] = self.branch
            if self.lose == 'branch':
                self.lose = ''
                raise ConnectorError('Lost response')
            return {'ref': kwargs['json']['ref'], 'object': {'type': 'commit', 'sha': self.branch}}
        if path.endswith('/pulls'):
            if method == 'GET':
                return [self.pr] if self.pr else []
            assert kwargs['json']['draft'] is False
            self.pr = {'number': 100, 'html_url': 'https://github.com/' + self.repository + '/pull/100', 'draft': False, 'state': 'open'}
            self.pr.update(head={'ref': kwargs['json']['head'], 'sha': self.branch, 'repo': {'id': self.repository_id, 'full_name': self.repository}},
                           base={'ref': kwargs['json']['base'], 'repo': {'id': self.repository_id, 'full_name': self.repository}})
            if self.lose == 'pr':
                self.lose = ''
                raise ConnectorError('Lost response')
            return self.pr
        raise AssertionError((method, path))


@pytest.mark.parametrize('path', ['../a', '/a', 'a//b', 'a/./b', 'a\\b', '.git/config', '.GIT/config',
    '.github', '.github/workflows', '.github/actions', '.ssh', '.ssh/config',
    'CODEOWNERS', 'nested/codeowners', '.env.local', 'a/private.pem', 'a/key.key', 'a\nb'])
def test_protected_paths(path):
    with pytest.raises(ValidationError):
        Change(path=path, content='unsafe')


def test_bounds_and_ambiguous_changes():
    for files in [[{'path': 'a', 'content': 'x'}, {'path': 'A', 'content': 'x'}],
                  [{'path': 'a', 'content': 'x'}, {'path': 'a/b', 'content': 'x'}],
                  [{'path': 'a', 'content': 'invalid binary', 'encoding': 'base64'}]]:
        with pytest.raises(ValidationError):
            Publish.model_validate({**PAYLOAD, 'files': files})


@pytest.mark.parametrize('status,payload', [(200, {'remaining': 1}), (204, None)])
def test_versioned_provider_requests_preserve_http_status(workspace, monkeypatch, status, payload):
    from app.github import GitHub
    app, _ = workspace
    github = app.state.connectors.github
    real_client = httpx.AsyncClient
    observed = []

    def transport(request):
        observed.append(request.headers['X-GitHub-Api-Version'])
        return httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)

    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    default = asyncio.run(GitHub.request(github, 'GET', '/repositories/101'))
    assert default == (payload or {}) and observed[-1] == '2022-11-28'
    versioned = asyncio.run(GitHub.request(github, 'GET', '/repositories/101',
        api_version='2026-03-10', include_status=True))
    assert versioned == {'status_code': status, 'data': payload or {}} and observed[-1] == '2026-03-10'
    with pytest.raises(ConnectorError, match='Unsupported'):
        asyncio.run(GitHub.request(github, 'GET', '/repositories/101', api_version='unreviewed'))
    assert len(observed) == 2


@pytest.mark.parametrize('lost', ['', 'branch', 'pr'])
def test_publish_normal_pr_once_across_lost_ack_and_new_turn(workspace, monkeypatch, lost):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = lost
    args = Publish.model_validate(PAYLOAD)
    if lost == 'branch':
        with pytest.raises(ConnectorError, match='Lost'):
            asyncio.run(github.publish(app.state.store.run(run_id), args))
    app.state.store.execute('UPDATE runs SET active_message_id=2 WHERE id=?', (run_id,))
    run = app.state.store.run(run_id)
    result = asyncio.run(github.publish(run, args))
    assert result['draft'] is False and result['number'] == 100
    assert result['title'] == args.title
    if lost == 'branch':
        assert result['branch_ownership_confirmed'] is False
        assert not github.branch_owned(101, result['branch'], github.connection_version())
    else:
        assert github.branch_owned(101, result['branch'], github.connection_version())
    assert asyncio.run(github.publish(run, args)) == result
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/refs')]) == 1
    writes = api.mutations
    assert all(c[0] == 'POST' and c[1].rsplit('/', 1)[-1] in {'blobs', 'trees', 'commits', 'refs', 'pulls'} for c in writes)
    ref = next(c[2]['json']['ref'] for c in writes if c[1].endswith('/refs'))
    assert ref.startswith('refs/heads/moyai/' + run_id[:12] + '/')
    with pytest.raises(ConnectorError, match='different changes'):
        asyncio.run(github.publish(run, Publish.model_validate({**PAYLOAD, 'title': 'Different payload'})))


@pytest.mark.parametrize('entry', [
    {'path': 'fix.py', 'type': 'tree', 'mode': '040000', 'sha': TREE},
    {'path': 'fix.py', 'type': 'blob', 'mode': '120000', 'sha': TREE},
    {'path': 'fix.py', 'type': 'commit', 'mode': '160000', 'sha': TREE},
])
def test_no_implicit_directory_deletion_or_special_file_changes(workspace, monkeypatch, entry):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.tree = [entry]
    with pytest.raises(ConnectorError, match='regular files'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    assert not api.mutations


def test_non_default_base_and_missing_deletion_are_rejected(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.status = 'diverged'
    with pytest.raises(ConnectorError, match='not on the selected base'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    api.status = 'ahead'
    with pytest.raises(ConnectorError, match='does not exist'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate({**PAYLOAD, 'request_key': 'delete-test-123', 'files': [{'path': 'absent', 'content': None}]})))
    assert not api.mutations


@pytest.mark.parametrize('name', ['-option', 'HEAD', 'bad..ref', 'bad@{ref', 'bad.lock', 'bad//ref', 'bad\nref', 'x?y', '@'])
def test_shared_branch_validation_rejects_ref_expressions(name):
    from agent.github_refs import validate_branch_name, validate_checkout_ref
    for validate in (validate_branch_name, validate_checkout_ref):
        with pytest.raises(ValueError):
            validate(name)


def test_optional_publication_fields_preserve_legacy_hash_payload():
    original = Publish.model_validate(PAYLOAD).model_dump()
    explicit = Publish.model_validate({**PAYLOAD, 'base_branch': '', 'head_branch': ''}).model_dump()
    assert original == explicit
    assert not {'base_branch', 'head_branch'} & original.keys()


def test_checkout_explicit_branch_and_exact_commit(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.named_branches['archive/base'] = BASE
    run = app.state.store.run(run_id)
    branch = asyncio.run(github.call(run, 'github_checkout', {'repository_id': 101, 'ref': 'archive/base'}))
    assert branch['base_sha'] == BASE and branch['base_branch'] == 'archive/base'
    assert branch['checkout_ref'] == 'refs/heads/archive/base'
    commit = asyncio.run(github.call(run, 'github_checkout', {'repository_id': 101, 'ref': BASE}))
    assert commit['base_sha'] == BASE and commit['checkout_ref'] == BASE and commit['base_branch'] == ''
    with pytest.raises(ValidationError):
        asyncio.run(github.call(run, 'github_checkout', {'repository_id': 101, 'ref': BASE, 'number': 100}))


def test_selected_base_and_head_are_used_without_default_only_limit(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.named_branches['archive/base'] = BASE
    api.status = 'diverged'
    args = Publish.model_validate({**PAYLOAD, 'base_branch': 'archive/base', 'head_branch': 'archive/feature'})
    result = asyncio.run(github.publish(app.state.store.run(run_id), args))
    assert result['base_branch'] == 'archive/base' and result['branch'] == 'archive/feature'
    assert api.pr['base']['ref'] == 'archive/base' and api.pr['head']['ref'] == 'archive/feature'
    assert result['draft'] is False


def test_branch_publication_and_existing_head_pr_are_separate(workspace, monkeypatch):
    from app.github import CreateBranch, OpenPullRequest, PublishBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    first = asyncio.run(github.controlled_publication(run, CreateBranch(repository_id=101, branch='archive/base',
        source_sha=BASE, request_key='create-base-123'), 'github_create_branch'))
    assert first == {'kind': 'branch', 'repository_id': 101, 'repository': 'BerriAI/litellm',
                     'branch': 'archive/base', 'commit': BASE, 'base_branch': ''}
    published = asyncio.run(github.controlled_publication(run, PublishBranch(repository_id=101,
        branch='archive/feature', base_sha=BASE, title='Branch change', files=PAYLOAD['files'],
        request_key='branch-files-123'), 'github_publish_branch'))
    assert published['kind'] == 'branch' and published['commit'] == COMMIT
    assert api.pr is None
    assert not app.state.store.rows("SELECT 1 FROM events WHERE json_text(data,'phase')='pr_created'")
    opened = asyncio.run(github.controlled_publication(run, OpenPullRequest(repository_id=101,
        base_branch='archive/base', head_branch='archive/feature', expected_head_sha=COMMIT,
        title='Open the branch', body='Verified separate branch.', request_key='open-branch-123'), 'github_open_pull_request'))
    assert opened['number'] == 100 and opened['draft'] is False and opened['base_branch'] == 'archive/base'
    assert len(app.state.store.rows("SELECT 1 FROM events WHERE json_text(data,'phase')='pr_created'")) == 1
    assert len([entry for entry in api.calls if entry[0] == 'POST' and entry[1].endswith('/pulls')]) == 1


@pytest.mark.parametrize('boundary', ['default', 'protected', 'rules', 'foreign', 'stale'])
def test_branch_publication_denies_unsafe_heads(workspace, monkeypatch, boundary):
    from app.github import PublishBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    branch = 'main' if boundary == 'default' else 'human/feature'
    api.named_branches[branch] = BASE
    expected = BASE
    if boundary == 'protected':
        api.protected.add(branch)
    if boundary == 'rules':
        api.rules[branch] = [{'type': 'pull_request'}]
    if boundary == 'stale':
        app.state.store.execute('INSERT INTO github_owned_branches VALUES(?,?,?,?,?,?)',
            (101, branch, github.connection_version(), 'trusted-test', run_id, BASE))
        api.named_branches[branch] = CURRENT
    args = PublishBranch(repository_id=101, branch=branch, base_sha=BASE, expected_head_sha=expected,
                         title='Not authorized', files=PAYLOAD['files'], request_key='unsafe-head-123')
    with pytest.raises(ConnectorError):
        asyncio.run(github.controlled_publication(app.state.store.run(run_id), args, 'github_publish_branch'))
    assert all(method == 'GET' for method, _, _ in api.calls)


def test_confirmed_branch_update_is_expected_head_fenced(workspace, monkeypatch):
    from app.github import CreateBranch, PublishBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    asyncio.run(github.controlled_publication(run, CreateBranch(repository_id=101, branch='feature/test',
        source_sha=BASE, request_key='create-base-123'), 'github_create_branch'))
    args = PublishBranch(repository_id=101, branch='feature/test', base_sha=BASE, expected_head_sha=BASE,
                         title='Owned branch update', files=PAYLOAD['files'], request_key='owned-update-123')
    result = asyncio.run(github.controlled_publication(run, args, 'github_publish_branch'))
    assert result['kind'] == 'branch' and result['commit'] == COMMIT and api.pr is None
    assert asyncio.run(github.controlled_publication(run, args, 'github_publish_branch')) == result
    patches = [entry for entry in api.calls if entry[0] == 'PATCH']
    assert len(patches) == 1 and patches[0][2]['json'] == {'sha': COMMIT, 'force': False}


@pytest.mark.parametrize('pattern,branch,expected', [
    ('release/*', 'release/next', True), ('release/*', 'release/team/next', False),
    ('qa/**/*', 'qa/fix', True), ('qa/**/*', 'qa/team/fix', True),
    ('qa**/**/*', 'qa-team/one/two', True), ('**/fix', 'fix', True),
    ('**/fix', 'team/other/fix', True), ('qa/***/fix', 'qa/team/fix', True),
    ('qa/***/fix', 'qa/team/other/fix', False), ('**', 'team/fix', False),
    ('release/?', 'release/v', True), ('release/?', 'release/v1', False),
    ('release/[0-9]*', 'release/2.0', True), ('release/[a-c]', 'release/b', True),
    ('release/[!a-c]', 'release/d', True), ('release/[^a-c]', 'release/b', False),
    ('feature/*', 'Feature/fix', False), ('{a,b}', 'a', False),
    ('{a,b}', '{a,b}', True), ('rélease/?', 'rélease/é', True),
])
def test_classic_patterns_follow_ruby_pathname_semantics(pattern, branch, expected):
    assert classic_pattern_matches(pattern, branch) is expected


@pytest.mark.parametrize('pattern', ['', 'bad\\*', '[a', '[[:digit:]]', '[z-a]', '[A-z]', 'bad//name', 'bad\nname'])
def test_unknown_classic_patterns_are_not_treated_as_nonmatches(pattern):
    with pytest.raises(ValueError):
        classic_pattern_matches(pattern, 'unrelated/feature')


def test_supported_classic_patterns_agree_with_ruby_reference():
    import shutil
    import subprocess
    ruby = shutil.which('ruby')
    if not ruby:
        pytest.skip('The optional Ruby reference interpreter is unavailable.')
    patterns = ['*', '**', '***', 'a*', 'a?', '[ab]', '[a-c]', '[!a-c]', '[^0-9]', '[-ab]', '[ab-]',
                '[A-C0-2]', 'a{b,c}', 'release/*', 'release/**', 'release/**/x', 'release/***/x',
                'release/**/*', 'release/**/**/x', 'a*/**/?', 'qa**/**/*', 'rélease/?']
    branches = ['a', 'ab', 'b', 'c', 'd', 'x', '1', 'A', 'C', 'a.b', 'a{b,c}', 'a/b', 'a/b/c',
                'release/x', 'release/a/x', 'release/a/b/x', 'release/a', 'release/a/b',
                'qa/fix', 'qa/team/deep/fix', 'qafoo/other/fix', 'rélease/é']
    cases = [(pattern, branch) for pattern in patterns for branch in branches]
    script = 'puts JSON.generate(JSON.parse(STDIN.read).map { |pattern,name| File.fnmatch(pattern,name,File::FNM_PATHNAME) })'
    reference = subprocess.run([ruby, '-rjson', '-e', script], input=json.dumps(cases), text=True,
                               capture_output=True, timeout=10, check=True)
    expected = json.loads(reference.stdout)
    assert len(expected) == len(cases)
    for (pattern, branch), matches in zip(cases, expected, strict=True):
        assert classic_pattern_matches(pattern, branch) is matches, (pattern, branch)


def test_classic_query_reaches_only_the_exact_read_policy(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    real_client = httpx.AsyncClient
    observed = []

    def transport(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={'data': {'repository': {'databaseId': 101, 'branchProtectionRules': {
            'nodes': [], 'totalCount': 0, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}})

    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    result = asyncio.run(github.classic_branch_rules(app.state.store.run(run_id), 'fixture', 101,
        github.connection_version(), 'github_create_branch'))
    assert result == []
    assert observed == [{'query': CLASSIC_PROTECTION_QUERY,
                         'variables': {'owner': 'BerriAI', 'name': 'litellm', 'cursor': None}}]


@pytest.mark.parametrize('pattern,branch,restricted', [
    ('main', 'feature/new', False),
    ('release/*', 'feature/new', False),
    ('release/*', 'release/new', True),
    ('qa/*', 'qa/team/new', False),
    ('qa/**/*', 'qa/team/new', True),
])
def test_absent_ref_checks_targeted_classic_creation_policy(workspace, monkeypatch, pattern, branch, restricted):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.classic_rules = [{'id': 'BPR_1', 'pattern': pattern, 'blocksCreations': True}]
    args = CreateBranch(repository_id=101, branch=branch, source_sha=BASE, request_key='classic-create-123')
    operation = github.controlled_publication(app.state.store.run(run_id), args, 'github_create_branch')
    if restricted:
        with pytest.raises(ConnectorError, match='restricts creation'):
            asyncio.run(operation)
        assert not api.mutations
    else:
        assert asyncio.run(operation)['branch'] == branch
        assert len(api.mutations) == 1 and api.mutations[0][1].endswith('/git/refs')


def test_creation_allowance_never_authorizes_a_later_protected_update(workspace, monkeypatch):
    from app.github import CreateBranch, PublishBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.classic_rules = [{'id': 'BPR_1', 'pattern': 'release/*', 'blocksCreations': False}]
    api.protected.add('release/new')
    run = app.state.store.run(run_id)
    args = CreateBranch(repository_id=101, branch='release/new', source_sha=BASE, request_key='classic-allow-123')
    assert asyncio.run(github.controlled_publication(run, args, 'github_create_branch'))['commit'] == BASE
    assert github.branch_owned(101, 'release/new', github.connection_version())
    api.calls.clear()
    update = PublishBranch(repository_id=101, branch='release/new', base_sha=BASE, expected_head_sha=BASE,
        files=PAYLOAD['files'], title='Must not bypass protection', request_key='classic-update-123')
    with pytest.raises(ConnectorError, match='Protected branch'):
        asyncio.run(github.controlled_publication(run, update, 'github_publish_branch'))
    assert not api.mutations


@pytest.mark.parametrize('metadata_missing', [False, True])
def test_branch_metadata_404_never_authorizes_overwriting_a_ref(workspace, monkeypatch, metadata_missing):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.named_branches['feature/existing'] = BASE

    async def request(method, path, **kwargs):
        if metadata_missing and path == '/repositories/101/branches/feature%2Fexisting':
            api.calls.append((method, path, kwargs))
            return None
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    args = CreateBranch(repository_id=101, branch='feature/existing', source_sha=BASE, request_key='classic-existing-123')
    with pytest.raises(ConnectorError, match='already exists'):
        asyncio.run(github.controlled_publication(app.state.store.run(run_id), args, 'github_create_branch'))
    assert not api.mutations


@pytest.mark.parametrize('policy', ['default', 'protected', 'classic', 'ruleset'])
def test_initial_branch_policy_denial_is_definitely_rejected(workspace, monkeypatch, policy):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    branch = 'main' if policy == 'default' else 'release/new'
    if policy == 'protected':
        api.named_branches[branch] = BASE
        api.protected.add(branch)
    elif policy == 'classic':
        api.classic_rules = [{'id': 'BPR_1', 'pattern': 'release/*', 'blocksCreations': True}]
    elif policy == 'ruleset':
        api.rules[branch] = [{'type': 'pull_request'}]
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers, json={
        'name': 'github_create_branch', 'arguments': {'repository_id': 101, 'branch': branch,
        'source_sha': BASE, 'request_key': 'known-policy-denial'}}).json()
    assert result['outcome'] == 'rejected' and result['outcome_uncertain'] is False
    assert result['dispatched'] is False
    assert not api.mutations


def test_classic_priority_uses_exact_winner_or_fails_closed():
    wildcard = {'id': 'BPR_1', 'pattern': 'release/*', 'blocksCreations': True}
    exact = {'id': 'BPR_2', 'pattern': 'release/feature', 'blocksCreations': False}
    assert classic_creation_restricted([wildcard, exact], 'release/feature') is False
    assert classic_creation_restricted([wildcard, exact], 'release/production') is True
    ambiguous = {'id': 'BPR_3', 'pattern': 'release/fea*', 'blocksCreations': False}
    with pytest.raises(ValueError, match='ambiguous'):
        classic_creation_restricted([wildcard, ambiguous], 'release/feature')
    with pytest.raises(ValueError, match='ambiguous'):
        classic_creation_restricted([exact, {**exact, 'id': 'BPR_4', 'blocksCreations': True}], 'release/feature')


@pytest.mark.parametrize('failure', ['permission', 'not_found', 'partial_errors', 'missing_repository',
    'wrong_repository', 'missing_rules', 'missing_total', 'bad_flag', 'incomplete', 'cursor_loop', 'unsupported',
    'changed_total', 'too_many_rules', 'revoked'])
def test_absent_ref_policy_uncertainty_prevents_every_mutation(workspace, monkeypatch, failure):
    from app.github import CreateBranch, GitHubError
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    reads = []

    async def request(method, path, **kwargs):
        if path != '/graphql':
            return await api.request(method, path, **kwargs)
        reads.append(kwargs['json']['variables']['cursor'])
        api.calls.append((method, path, kwargs))
        if failure in {'permission', 'not_found'}:
            raise GitHubError('Classic policy unavailable', 403 if failure == 'permission' else 404)
        rules = {'nodes': [], 'totalCount': 0, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        repository = {'databaseId': 101, 'branchProtectionRules': rules}
        response = {'data': {'repository': repository}}
        if failure == 'partial_errors':
            response['errors'] = [{'message': 'Resource not accessible by integration'}]
        elif failure == 'missing_repository':
            response['data']['repository'] = None
        elif failure == 'wrong_repository':
            repository['databaseId'] = 202
        elif failure == 'missing_rules':
            repository.pop('branchProtectionRules')
        elif failure == 'missing_total':
            rules.pop('totalCount')
        elif failure in {'bad_flag', 'unsupported'}:
            rules.update(totalCount=1, nodes=[{'id': 'BPR_1', 'pattern': '[[:alpha:]]' if failure == 'unsupported' else 'feature/*',
                                             'blocksCreations': None if failure == 'bad_flag' else True}])
        elif failure == 'incomplete':
            rules['totalCount'] = 1
        elif failure in {'cursor_loop', 'changed_total'}:
            rules.update(totalCount=3 if failure == 'cursor_loop' else 2 + len(reads),
                         nodes=[{'id': f'BPR_{len(reads)}', 'pattern': 'main', 'blocksCreations': True}],
                         pageInfo={'hasNextPage': True, 'endCursor': 'same-cursor'})
        elif failure == 'too_many_rules':
            rules['totalCount'] = 1001
        elif failure == 'revoked':
            app.state.store.update_run(run_id, status='cancelled')
        return response

    monkeypatch.setattr(github, 'request', request)
    args = CreateBranch(repository_id=101, branch='feature/new', source_sha=BASE, request_key='classic-unknown-123')
    with pytest.raises(ConnectorError) as rejected:
        asyncio.run(github.controlled_publication(app.state.store.run(run_id), args, 'github_create_branch'))
    assert not api.mutations and len(reads) <= 2
    if failure not in {'revoked', 'permission', 'not_found'}:
        assert rejected.value.receipt(write=True)['outcome_uncertain'] is False
    assert not app.state.store.rows('SELECT * FROM github_publications')


def test_classic_policy_reads_later_pages_before_creating(workspace, monkeypatch):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    reads = []

    async def request(method, path, **kwargs):
        if path != '/graphql':
            return await api.request(method, path, **kwargs)
        cursor = kwargs['json']['variables']['cursor']
        reads.append(cursor)
        api.calls.append((method, path, kwargs))
        nodes = [{'id': 'BPR_1', 'pattern': 'main', 'blocksCreations': True}] if cursor is None else [
            {'id': 'BPR_2', 'pattern': 'release/*', 'blocksCreations': True}]
        return {'data': {'repository': {'databaseId': 101, 'branchProtectionRules': {
            'nodes': nodes, 'totalCount': 2, 'pageInfo': {'hasNextPage': cursor is None, 'endCursor': 'next-page'}}}}}

    monkeypatch.setattr(github, 'request', request)
    args = CreateBranch(repository_id=101, branch='release/new', source_sha=BASE, request_key='classic-paging-123')
    with pytest.raises(ConnectorError, match='restricts creation'):
        asyncio.run(github.controlled_publication(app.state.store.run(run_id), args, 'github_create_branch'))
    assert reads == [None, 'next-page'] and not api.mutations


@pytest.mark.parametrize('name', ['github_stacks', 'github_stack', 'github_create_stack',
                                 'github_add_to_stack', 'github_unstack'])
def test_native_stack_tools_use_the_broker_dispatch_owner(workspace, monkeypatch, name):
    from app.github import TOOLS
    from app.github_diagnostics import required_permissions
    from app.github_stacks import TOOLS as STACK_TOOLS
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github

    async def stack_call(run, tool, arguments):
        return {'run_id': run['id'], 'tool': tool, 'arguments': arguments}

    monkeypatch.setattr(github, 'stack_call', stack_call)
    assert TOOLS[name] == STACK_TOOLS[name]
    assert asyncio.run(github.call(app.state.store.run(run_id), name, {'repository_id': 101})) == {
        'run_id': run_id, 'tool': name, 'arguments': {'repository_id': 101}}
    assert required_permissions(name) == ({'contents': 'read', 'pull_requests': 'write'}
                                         if STACK_TOOLS[name][1] else {'pull_requests': 'read'})


@pytest.mark.parametrize('name,permissions', [
    ('github_retarget_pull_request', {'contents': 'read', 'pull_requests': 'write'}),
    ('github_create_branch', {'contents': 'write', 'pull_requests': 'write'}),
    ('github_publish_branch', {'contents': 'write', 'pull_requests': 'write'}),
    ('github_open_pull_request', {'contents': 'write', 'pull_requests': 'write'}),
    ('github_rebase_pull_request', {'contents': 'write', 'pull_requests': 'write'}),
])
def test_new_write_capabilities_report_actual_token_scopes(name, permissions):
    from app.github_diagnostics import required_permissions
    assert required_permissions(name) == permissions


@pytest.mark.parametrize('change', ['disconnect', 'replace', 'stop', 'rotate', 'read_only', 'allowlist'])
def test_revocation_during_publication_stops_further_mutations(workspace, monkeypatch, change):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    def mutate(method, path):
        if method != 'POST' or not path.endswith('/git/trees'):
            return
        if change == 'disconnect':
            app.state.store.execute("DELETE FROM connections WHERE provider='github'")
        elif change == 'replace':
            app.state.connectors.save('github', {**credentials(), 'installation_id': 20}, 'Changed')
        elif change == 'stop':
            app.state.store.update_run(run_id, status='cancelled')
        elif change == 'rotate':
            app.state.store.update_run(run_id, token_hash=digest('new-token'))
        elif change == 'allowlist':
            select(app, (202,))
        else:
            app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    api.on_call = mutate
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    assert [c[1].rsplit('/', 1)[-1] for c in api.mutations] == ['blobs', 'trees']


@pytest.mark.parametrize('user,role', [('alice', 'admin'), ('bob', 'member')])
def test_direct_pr_creation_without_admin_approval_and_no_merge_review_tools(workspace, monkeypatch, user, role):
    app, client = workspace
    run_id, headers = connected(app)
    owner = sign_in(app, client, user, user + '@berri.ai')
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?', (owner, owner, run_id))
    assert client.get('/api/session').json()['role'] == role
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    def still_running(method, path):
        assert app.state.store.run(run_id)['status'] == 'running'
        assert not app.state.store.approvals(run_id)
    api.on_call = still_running
    url = f'/broker/{run_id}/tools/call'
    for name in ['github_merge', 'github_approve', 'github_review', 'github_request', 'github_update_branch']:
        assert client.post(url, headers=headers, json={'name': name, 'arguments': {}}).status_code == 403
    tool = next(t for t in client.get(f'/broker/{run_id}/tools', headers=headers).json() if t['name'] == 'github_create_pull_request')
    assert tool['annotations']['readOnlyHint'] is False
    connection = next(c for c in client.get('/api/connections').json() if c['id'] == 'github')
    policy = next(t for t in connection['tools'] if t['name'] == tool['name'])
    assert policy['write'] is True and policy['requires_approval'] is False
    response = client.post(url, headers=headers, json={'name': tool['name'], 'arguments': PAYLOAD})
    assert response.status_code == 200
    assert response.json()['url'] == 'https://github.com/BerriAI/litellm/pull/100'
    assert response.json()['draft'] is False
    assert not app.state.store.approvals(run_id)
    assert not any(e['kind'] == 'approval' for e in app.state.store.events(run_id))
    assert app.state.store.run(run_id)['status'] == 'running'
    if role == 'member':
        assert client.patch('/api/connections/github/policy', json={'enabled': True, 'read_only': False}).status_code == 403


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_direct_pr_still_requires_connection_policy_and_live_capability(workspace, monkeypatch, restriction):
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    if restriction in {'read_only', 'disabled'}:
        response = client.patch('/api/connections/github/policy', json={'enabled': restriction != 'disabled', 'read_only': restriction == 'read_only'})
        assert response.status_code == 200
    elif restriction == 'disconnect':
        client.delete('/api/connections/github')
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    else:
        app.state.store.update_run(run_id, token_hash=digest('rotated'))
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'github_create_pull_request', 'arguments': PAYLOAD})
    if restriction in {'read_only', 'disabled', 'disconnect'}:
        assert response.status_code == 200
        assert response.json()['outcome'] == 'rejected' and response.json()['dispatched'] is False
    else:
        assert response.status_code == (401 if restriction in {'cancelled', 'revoked'} else 403)
    assert not api.calls and not app.state.store.approvals(run_id)
    if restriction == 'read_only':
        names = [t['name'] for t in client.get(f'/broker/{run_id}/tools', headers=headers).json()]
        assert 'github_checkout' in names and 'github_create_pull_request' not in names


def test_direct_pr_lost_response_is_reconciled_without_replaying_write(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    api.lose = 'pr'
    url = f'/broker/{run_id}/tools/call'
    body = {'name': 'github_create_pull_request', 'arguments': PAYLOAD}
    result = client.post(url, headers=headers, json=body).json()
    assert result['url'] == api.pr['html_url'] and 'error' not in result
    assert app.state.store.rows('SELECT result FROM github_publications')[0]['result']
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    # A follow-up reads the saved receipt instead of opening another PR.
    app.state.store.execute('UPDATE runs SET active_message_id=2 WHERE id=?', (run_id,))
    retry = client.post(url, headers=headers, json=body).json()
    assert retry['url'] == api.pr['html_url'] and retry['draft'] is False
    assert not app.state.store.approvals(run_id)
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/refs')]) == 1


def test_connection_secrets_stay_on_server_and_raw_tokens_refused(workspace):
    app, client = workspace
    run_id, _ = connected(app)
    encrypted = app.state.store.rows('SELECT encrypted FROM github_app')[0]['encrypted']
    assert 'private-signing-key' not in encrypted
    for value in [client.get('/api/connections').text, client.get('/api/config').text,
                  json.dumps(app.state.manager.spec(app.state.store.run(run_id)))]:
        assert 'private-signing-key' not in value and 'installation_id' not in value
    assert client.post('/api/connections/github', json={'token': 'personal-secret'}).status_code == 400


def test_reconnect_after_admission_before_write_lock_cannot_substitute_installation(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    admitted_run = {**app.state.store.run(run_id), 'github_connection_version': github.connection_version()}
    app.state.connectors.save('github', {**credentials(), 'installation_id': 20}, 'Reconnected')
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.publish(admitted_run, Publish.model_validate(PAYLOAD)))
    assert not api.calls


def test_manifest_state_is_bound_expiring_single_use_and_admin_only(workspace, monkeypatch):
    app, client = workspace
    async def organization(*args, **kwargs):
        return {'id': OWNER_ID, 'login': 'BerriAI', 'type': 'Organization'}
    monkeypatch.setattr(app.state.connectors.github, 'request', organization)
    start = client.post('/api/connections/github/oauth', json={'organization': 'BerriAI'}).json()['url']
    state = parse_qs(urlparse(start).query)['state'][0]
    page = client.get(start)
    assert page.status_code == 200 and 'BerriAI' in page.text
    assert 'https://github.com' in page.headers['content-security-policy']
    assert client.get('/auth/github/register?state=unknown').status_code == 400
    async def convert(*args, **kwargs):
        return {'owner': {'id': OWNER_ID, 'login': 'BerriAI', 'type': 'Organization'}, 'permissions': PERMISSIONS, 'id': 123, 'slug': 'moyai-test', 'pem': 'secret-key'}
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'request', convert)
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'app-jwt')
    callback = f'/oauth/github/app-callback?state={state}&code=temporary-registration-code'
    redirected = client.get(callback, follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers['location'].startswith('https://github.com/apps/moyai-test/installations/new?state=')
    assert client.get(callback).status_code == 400
    next_url = client.post('/api/connections/github/oauth').json()['url']
    next_state = parse_qs(urlparse(next_url).query)['state'][0]
    app.state.store.execute('UPDATE oauth_states SET expires=?', (time.time() - 1,))
    assert client.get(f'/oauth/github/callback?state={next_state}&installation_id=10&setup_action=install').status_code == 400
    sid = app.state.security.signer.dumps({'sid': 'member-session', 'role': 'member', 'method': 'local'})
    client.cookies.clear(); client.cookies.set('workspace_session', sid)
    assert client.get(start).status_code == 403
    assert client.post('/api/connections/github/oauth').status_code == 403


@pytest.mark.parametrize('bad', ['owner', 'permissions', 'repository', 'suspended'])
def test_installation_scope_validation(workspace, monkeypatch, bad):
    app, _ = workspace
    connected(app)
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
    async def response(method, path, **kwargs):
        if path.startswith('/app/installations/') and not path.endswith('/access_tokens'):
            return {'account': {'id': 999 if bad == 'owner' else OWNER_ID, 'login': 'BerriAI', 'type': 'Organization'},
                    'permissions': {**PERMISSIONS, **({'contents': 'read'} if bad == 'permissions' else {})},
                    'suspended_at': '2026-01-01' if bad == 'suspended' else None}
        if path.endswith('/access_tokens'):
            assert kwargs['json'] == {'repository_ids': [101], 'permissions': {'contents': 'read', 'pull_requests': 'read'}}
            return {'token': 'token', 'expires_at': '2099-01-01T00:00:00Z'}
        return {'repositories': [{**repository_data('BerriAI/litellm'), 'id': 999 if bad == 'repository' else 101}]}
    monkeypatch.setattr(github, 'request', response)
    with pytest.raises(ConnectorError):
        asyncio.run(github.verify(credentials()))


def test_git_read_only_stream_and_no_header_or_redirect_leak(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    async def token(*args, **kwargs):
        return 'installation-secret'
    monkeypatch.setattr(github, 'installation_token', token)
    async def metadata(*args, **kwargs):
        return repository_data('BerriAI/litellm')
    monkeypatch.setattr(github, 'request', metadata)
    captured, redirect = [], [False]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            yield b'second'
    def upstream(request):
        captured.append(request)
        assert request.url.host == 'github.com'
        assert request.url.path.startswith('/BerriAI/litellm.git/')
        assert request.headers['authorization'].startswith('Basic ')
        assert 'run-capability-only' not in str(request.headers)
        assert 'x-arbitrary' not in request.headers
        if redirect[0]:
            return httpx.Response(302, headers={'Location': 'https://evil.example'})
        kind = 'advertisement' if request.method == 'GET' else 'result'
        return httpx.Response(200, headers={'Content-Type': 'application/x-git-upload-pack-' + kind,
                                           'X-Upstream-Secret': 'do-not-forward'}, stream=Stream())
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github_git.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    base = f'/broker/{run_id}/github.git/'
    for method, route in [('GET', 'info/refs?service=git-receive-pack'), ('POST', 'git-receive-pack'),
                          ('POST', 'git-upload-pack?url=evil'), ('GET', 'config'), ('GET', 'info/refs?service=git-upload-pack&x=1')]:
        assert client.request(method, base + route, headers=headers).status_code == 403
    assert not captured
    read = client.get(base + 'info/refs?service=git-upload-pack', headers={**headers, 'X-Arbitrary': 'secret'})
    assert read.content == b'firstsecond'
    assert 'x-upstream-secret' not in read.headers
    assert client.post(base + 'git-upload-pack', headers={**headers, 'Content-Type': 'application/x-git-upload-pack-request'}, content=b'0000').content == b'firstsecond'
    redirect[0] = True
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 502
    assert len(captured) == 3  # No request to the redirect target.
    client.delete('/api/connections/github')
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 403
    app.state.store.update_run(run_id, token_hash=digest('rotated'))
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 401


def connect_multiple(app):
    select(app, (101, 202))


def test_self_repository_pr_is_created_directly_without_approval(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    connect_multiple(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch, 'BerriAI/moyai')
    payload = {**PAYLOAD, 'repository': 'BerriAI/moyai'}
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'github_create_pull_request', 'arguments': payload})
    assert response.status_code == 200
    result = response.json()
    assert result['repository'] == 'BerriAI/moyai' and not result['draft']
    assert not app.state.store.approvals(run_id)
    assert all(path.startswith('/repositories/202') or path == '/graphql' for _, path, _ in api.calls)
    assert all(method == 'POST' and path.rsplit('/', 1)[-1] in {'blobs', 'trees', 'commits', 'refs', 'pulls'}
               for method, path, _ in api.mutations)


def test_repository_routing_and_no_implicit_access_expansion(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    GitHubAPI(github, monkeypatch)
    with pytest.raises(ConnectorError, match='not selected'):
        asyncio.run(github.selected_target({}, 'BerriAI/moyai'))
    assert asyncio.run(github.selected_target({})) == 101
    connect_multiple(app)
    api = GitHubAPI(github, monkeypatch, 'BerriAI/moyai')
    run = {**app.state.store.run(run_id), 'repo_url': 'https://github.com/BerriAI/moyai.git'}
    result = asyncio.run(github.call(run, 'github_checkout', {}))
    assert result['repository'] == 'BerriAI/moyai'
    assert result['git_path'] == '/github/repositories/202.git'
    assert asyncio.run(github.call(run, 'github_repositories', {}))['repositories'] == github.repository_options()
    api.calls.clear()
    for target in ['BerriAI/other', 'OtherOrg/moyai', '../moyai']:
        with pytest.raises(ConnectorError):
            asyncio.run(github.call(run, 'github_checkout', {'repository': target}))
        response = client.get(f'/broker/{run_id}/github/{target}.git/info/refs?service=git-upload-pack', headers=headers)
        assert response.status_code in {403, 404}
    assert all(method == 'GET' for method, _, _ in api.calls)
    approved = {**run, 'github_connection_version': github.connection_version()}
    select(app, (101,))
    with pytest.raises(ConnectorError):
        asyncio.run(github.publish(approved, Publish.model_validate({**PAYLOAD, 'repository': 'BerriAI/moyai'})))
    assert all(method == 'GET' for method, _, _ in api.calls)


def test_installation_tokens_are_scoped_cached_and_verified_per_repository(workspace, monkeypatch):
    app, _ = workspace
    connected(app); connect_multiple(app)
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
    requests = []
    async def request(method, path, **kwargs):
        if path.endswith('/access_tokens'):
            requests.append(kwargs['json'])
            repo = kwargs['json']['repository_ids'][0]
            return {'token': str(repo) + '-' + kwargs['json']['permissions']['contents'], 'expires_at': '2099-01-01T00:00:00Z'}
        if path == '/installation/repositories':
            repo = kwargs['token'].removesuffix('-read')
            return {'repositories': [repository_data(next(n for n, i in REPO_IDS.items() if i == int(repo)))]}
        return {'account': {'id': OWNER_ID, 'login': 'BerriAI', 'type': 'Organization'}, 'permissions': PERMISSIONS, 'suspended_at': None}
    monkeypatch.setattr(github, 'request', request)
    saved = github.saved_credentials()
    assert asyncio.run(github.verify(saved)) == 'BerriAI/litellm, BerriAI/moyai'
    assert [r['repository_ids'] for r in requests] == [[101], [202]]
    assert asyncio.run(github.installation_token(repository='berriai/MOYAI')) == '202-read'
    assert len(requests) == 2
    assert asyncio.run(github.installation_token(repository='BerriAI/moyai', write=True)) == '202-write'
    assert requests[-1] == {'repository_ids': [202], 'permissions': {'contents': 'write', 'pull_requests': 'write'}}
    assert asyncio.run(github.installation_token(repository='BerriAI/litellm')) == '101-read'
    with pytest.raises(ConnectorError):
        asyncio.run(github.installation_token(repository='BerriAI/unlisted', write=True))
    assert len(requests) == 3


def test_specific_git_route_cannot_change_repo_or_allow_push(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app); connect_multiple(app)
    github = app.state.connectors.github
    tokens, paths = [], []
    async def metadata(method, path, **kwargs):
        return repository_data(next(n for n,i in REPO_IDS.items() if path == '/repositories/' + str(i)))
    monkeypatch.setattr(github, 'request', metadata)
    async def token(*args, **kwargs):
        assert not kwargs.get('write')
        tokens.append(kwargs['repository'])
        return 'server-only'
    monkeypatch.setattr(github, 'installation_token', token)
    def upstream(request):
        paths.append(request.url.path)
        kind = 'advertisement' if request.method == 'GET' else 'result'
        return httpx.Response(200, content=b'0000', headers={'Content-Type': 'application/x-git-upload-pack-' + kind})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github_git.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    for repo in ['BerriAI/litellm', 'BerriAI/moyai']:
        base = f'/broker/{run_id}/github/{repo}.git/'
        assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).content == b'0000'
        assert paths[-1] == '/' + repo + '.git/info/refs' and tokens[-1] == REPO_IDS[repo]
        for suffix in ['info/refs?service=git-receive-pack', 'info/refs?service=git-upload-pack&repository=other', 'config']:
            assert client.get(base + suffix, headers=headers).status_code == 403
        assert client.post(base + 'git-receive-pack', headers=headers).status_code == 403
    assert len(paths) == 2
    select(app, (101,))
    assert client.get(f'/broker/{run_id}/github/BerriAI/moyai.git/info/refs?service=git-upload-pack', headers=headers).status_code == 403
    assert len(paths) == 2


@pytest.mark.parametrize('invalid', ['', 'owner', 'type', 'permission', 'id', 'signature'])
def test_existing_app_recovery_verifies_before_saving_and_keeps_key_private(workspace, monkeypatch, invalid):
    app, client = workspace
    github = app.state.connectors.github
    secret = 'private-key-material-' * 12
    def sign(config=None):
        if invalid == 'signature':
            raise ConnectorError('The signing key is invalid.')
        return 'signed-jwt'
    monkeypatch.setattr(github, 'app_jwt', sign)
    async def request(method, path, **kwargs):
        assert method == 'GET' and path == '/app' and kwargs['token'] == 'signed-jwt'
        return {'id': 1 if invalid == 'id' else 123, 'slug': 'moyai-test',
                'owner': {'id': None if invalid == 'owner' else OWNER_ID, 'login': 'BerriAI', 'type': 'User' if invalid == 'type' else 'Organization'},
                'permissions': {**PERMISSIONS, **({'pull_requests': 'read'} if invalid == 'permission' else {})}}
    monkeypatch.setattr(github, 'request', request)
    result = client.post('/api/connections/github/app', json={'app_id': 123, 'private_key': secret})
    assert secret not in result.text
    if invalid:
        assert result.status_code >= 400 and not github.app_config()
    else:
        assert result.status_code == 200 and result.json()['url'].startswith('https://github.com/apps/moyai-test/installations/new?')
        assert github.app_config()['pem'] == secret
        assert secret not in app.state.store.rows('SELECT encrypted FROM github_app')[0]['encrypted']
        assert secret not in client.get('/api/connections').text
    sid = app.state.security.signer.dumps({'sid': 'member-session', 'role': 'member', 'method': 'local'})
    client.cookies.clear(); client.cookies.set('workspace_session', sid)
    assert client.post('/api/connections/github/app', json={'app_id': 123, 'private_key': secret}).status_code == 403
