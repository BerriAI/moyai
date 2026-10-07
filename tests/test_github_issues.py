import asyncio
import re

import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import (CommentIssue, CreateIssue, Issue, ListIssues, MANIFEST_PERMISSIONS, PERMISSIONS, ReadIssue,
                        supports_permissions)
from app.security import digest
from test_workspace import workspace, cloud_capability
from test_github import GitHubAPI, OWNER_ID, connected

PREFIX = '/repositories/101'
ISSUE_URL = 'https://github.com/BerriAI/litellm/issues/'
KEY = 'issue-key-123'
CREATE = {'repository': 'BerriAI/litellm', 'title': 'Add Gemini 2.5 support', 'body': 'Feature request description.', 'request_key': KEY}
COMMENT = {'repository': 'BerriAI/litellm', 'number': 7, 'body': 'Working on this now.', 'request_key': KEY}


class IssueAPI(GitHubAPI):
    """In-memory issues for BerriAI/litellm layered on the shared GitHub fixture."""

    def __init__(self, github, monkeypatch):
        super().__init__(github, monkeypatch)
        self.items, self.comments, self.tokens, self.token_hook = {}, {}, [], None
        self.lose = ''  # 'applied': write lands, response lost; 'dropped': nothing lands

        async def token(*args, **kwargs):
            self.tokens.append(kwargs)
            self.token_repository = github.target(kwargs.get('repository', ''))
            if self.token_hook:
                self.token_hook(kwargs)
            return 'installation-secret-never-in-sandbox'
        monkeypatch.setattr(github, 'installation_token', token)

    def add(self, number, pr=False, comments=0, **fields):
        item = {'number': number, 'title': f'Item {number}', 'state': 'open', 'body': '', 'user': {'login': 'user'},
                'labels': [], 'created_at': f'2026-10-{number % 28 + 1:02d}T00:00:00Z', 'comments': comments,
                'html_url': ISSUE_URL.replace('issues', 'pull' if pr else 'issues') + str(number), **fields}
        if pr:
            item['pull_request'] = {'url': f'https://api.github.com/repos/BerriAI/litellm/pulls/{number}'}
        self.items[number] = item
        self.comments[number] = [{'id': number * 1000 + i, 'body': f'comment {i}', 'user': {'login': 'user'},
                                  'html_url': f'{ISSUE_URL}{number}#issuecomment-{number * 1000 + i}'} for i in range(comments)]
        return item

    def posts(self):
        return [c for c in self.calls if c[0] == 'POST']

    def lost(self, apply):
        mode, self.lose = self.lose, ''
        if mode == 'dropped':
            raise ConnectorError('The GitHub response was not received. Check the destination before retrying a write.')
        apply()
        if mode == 'applied':
            raise ConnectorError('The GitHub response was not received. Check the destination before retrying a write.')

    @staticmethod
    def page(items, params):
        size, page = params['per_page'], params['page']
        return items[(page - 1) * size:page * size]

    async def request(self, method, path, **kwargs):
        match = re.fullmatch(PREFIX + r'/issues(?:/(\d+))?(/comments)?', path)
        if not match:
            return await super().request(method, path, **kwargs)
        self.calls.append((method, path, kwargs))
        number = int(match[1]) if match[1] else None
        if number is None and method == 'GET':
            params = kwargs['params']
            items = sorted(self.items.values(), key=lambda i: i['number'], reverse=params.get('direction') == 'desc')
            return self.page([i for i in items if params['state'] in ('all', i['state'])], params)
        if number is None and method == 'POST':
            created = {}
            def apply():
                created.update(self.add(max(self.items, default=0) + 1, title=kwargs['json']['title'], body=kwargs['json']['body']))
            self.lost(apply)
            return created
        if number not in self.items:
            raise ConnectorError('GitHub did not confirm the operation (404).')
        if not match[2]:
            return self.items[number]
        if method == 'GET':
            return self.page(self.comments[number], kwargs['params'])
        created = {}
        def apply():
            identity = 90000 + len(self.comments[number])
            created.update(id=identity, body=kwargs['json']['body'], html_url=f'{ISSUE_URL}{number}#issuecomment-{identity}')
            self.comments[number].append(created)
        self.lost(apply)
        return created


def setup(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    api = IssueAPI(app.state.connectors.github, monkeypatch)
    api.add(7)
    return app, client, run_id, headers, api


def call(app, run_id, name, arguments):
    return asyncio.run(app.state.connectors.github.call(app.state.store.run(run_id), name, arguments))


def test_issue_schema_validation():
    for model, value in [(Issue, {'number': 0}), (ReadIssue, {'number': 1, 'comments_page': 0}),
                         (ListIssues, {'state': 'merged'}), (ListIssues, {'page': 0}), (ListIssues, {'labels': 'bug'}),
                         (CreateIssue, {**CREATE, 'title': ''}), (CreateIssue, {**CREATE, 'title': ' \n\t'}),
                         (CreateIssue, {**CREATE, 'body': 'x' * 20001}), (CreateIssue, {**CREATE, 'request_key': 'short'}),
                         (CreateIssue, {k: v for k, v in CREATE.items() if k != 'request_key'}),
                         (CommentIssue, {**COMMENT, 'body': ''}), (CommentIssue, {**COMMENT, 'body': '  \n '}),
                         (CommentIssue, {k: v for k, v in COMMENT.items() if k != 'request_key'})]:
        with pytest.raises(ValidationError):
            model.model_validate(value)
    assert ListIssues().state == 'open'
    # GitHub accepts issues without a description.
    assert CreateIssue.model_validate({k: v for k, v in CREATE.items() if k != 'body'}).body == ''


def test_read_issue_pages_comments_and_marks_truncation(workspace, monkeypatch):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    api.add(42, comments=31, body='b' * 20001, labels=[{'name': 'bug'}, 'ignored'], user=None)
    api.comments[42][0]['body'] = 'c' * 20001
    first = call(app, run_id, 'github_issue', {'repository': 'BerriAI/litellm', 'number': 42})
    assert first['url'] == ISSUE_URL + '42' and 'html_url' not in first
    assert len(first['body']) == 20000 and first['body_truncated'] is True
    assert first['author'] is None and first['labels'] == ['bug']
    assert first['comments_count'] == 31 and len(first['comments']) == 30
    assert first['comments'][0]['body_truncated'] is True and len(first['comments'][0]['body']) == 20000
    assert first['comments'][1]['body_truncated'] is False and first['comments'][1]['url'].endswith('#issuecomment-42001')
    assert first['comments_page'] == 1 and first['comments_next_page'] == 2
    last = call(app, run_id, 'github_issue', {'repository': 'BerriAI/litellm', 'number': 42, 'comments_page': 2})
    assert [c['body'] for c in last['comments']] == ['comment 30'] and last['comments_next_page'] is None
    assert [c[2]['params'] for c in api.calls if c[1].endswith('/comments')] == [{'per_page': 30, 'page': 1}, {'per_page': 30, 'page': 2}]
    assert [t for t in api.tokens if t.get('issues')] == [{'repository': 101, 'issues': True}, {'repository': 101, 'issues': True}]


def test_read_issue_rejects_pull_request(workspace, monkeypatch):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    api.add(11, pr=True)
    with pytest.raises(ConnectorError, match='pull request'):
        call(app, run_id, 'github_issue', {'repository': 'BerriAI/litellm', 'number': 11})
    assert not any(c[1].endswith('/comments') for c in api.calls)


def test_list_issues_full_page_with_pr_keeps_next_page(workspace, monkeypatch):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    api.items.clear()
    for number in range(1, 46):
        api.add(number, pr=number % 5 == 0, labels=[{'name': 'good first issue'}] if number == 1 else [])
    first = call(app, run_id, 'github_issues', {'repository': 'BerriAI/litellm'})
    assert len(first['issues']) == 24 and first['next_page'] == 2
    assert first['issues'][0] == {'number': 1, 'title': 'Item 1', 'state': 'open', 'url': ISSUE_URL + '1', 'author': 'user',
                                  'labels': ['good first issue'], 'created_at': '2026-10-02T00:00:00Z', 'comments_count': 0}
    assert not any(i['number'] % 5 == 0 for i in first['issues'])
    second = call(app, run_id, 'github_issues', {'repository': 'BerriAI/litellm', 'page': 2})
    assert len(second['issues']) == 12 and second['next_page'] is None
    # A page made only of PRs still continues.
    api.items.clear()
    for number in range(1, 32):
        api.add(number, pr=number <= 30)
    only_prs = call(app, run_id, 'github_issues', {'repository': 'BerriAI/litellm', 'state': 'all'})
    assert only_prs['issues'] == [] and only_prs['next_page'] == 2
    assert api.calls[-1][2]['params'] == {'state': 'all', 'per_page': 30, 'page': 1}


def test_create_and_comment_issue_with_receipts(workspace, monkeypatch):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    created = call(app, run_id, 'github_create_issue', CREATE)
    sent = api.posts()[0][2]['json']
    assert sent['title'] == CREATE['title']
    assert re.fullmatch(r'Feature request description\.\n\n<!-- moyai-issue:[0-9a-f]{64} -->', sent['body'])
    assert created == {'number': 8, 'url': ISSUE_URL + '8', 'title': CREATE['title'], 'state': 'open',
                       'repository_id': 101, 'repository': 'BerriAI/litellm'}
    commented = call(app, run_id, 'github_comment_issue', COMMENT)
    assert re.fullmatch(r'Working on this now\.\n\n<!-- moyai-comment:[0-9a-f]{64} -->', api.posts()[1][2]['json']['body'])
    assert commented == {'id': 90000, 'url': ISSUE_URL + '7#issuecomment-90000', 'number': 7,
                         'repository_id': 101, 'repository': 'BerriAI/litellm'}
    assert [t.get('write') for t in api.tokens if t.get('issues')] == [True, True]
    # Same key and arguments replay the receipt; a different payload is refused.
    assert call(app, run_id, 'github_create_issue', CREATE) == created
    assert call(app, run_id, 'github_comment_issue', COMMENT) == commented
    with pytest.raises(ConnectorError, match='different changes'):
        call(app, run_id, 'github_create_issue', {**CREATE, 'body': 'Another description.'})
    with pytest.raises(ConnectorError, match='different changes'):
        call(app, run_id, 'github_comment_issue', {**COMMENT, 'body': 'Another comment.'})
    assert len(api.posts()) == 2
    # An empty description is allowed and carries only the hidden marker.
    call(app, run_id, 'github_create_issue', {'repository': 'BerriAI/litellm', 'title': 'No body', 'request_key': 'empty-body-1'})
    assert re.fullmatch(r'<!-- moyai-issue:[0-9a-f]{64} -->', api.posts()[2][2]['json']['body'])


def test_comment_issue_rejects_pull_requests(workspace, monkeypatch):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    api.add(11, pr=True)
    with pytest.raises(ConnectorError, match='pull request.*No comment was sent.*github_comment_pull_request'):
        call(app, run_id, 'github_comment_issue', {**COMMENT, 'number': 11})
    assert not api.posts()


@pytest.mark.parametrize('tool,arguments', [('github_create_issue', CREATE), ('github_comment_issue', COMMENT)])
@pytest.mark.parametrize('change', ['read_only', 'disabled', 'revoked', 'cancelled', 'disconnect'])
def test_issue_write_rechecks_policy_after_token_acquisition(workspace, monkeypatch, tool, arguments, change):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    run = app.state.store.run(run_id)
    def mutate(kwargs):
        if not kwargs.get('issues'):
            return
        if change in {'read_only', 'disabled'}:
            app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                    ('github', change != 'disabled', change == 'read_only'))
        elif change == 'revoked':
            app.state.store.update_run(run_id, token_hash=digest('rotated'))
        elif change == 'cancelled':
            app.state.store.update_run(run_id, status='cancelled')
        else:
            app.state.store.execute("DELETE FROM connections WHERE provider='github'")
    api.token_hook = mutate
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(app.state.connectors.github.call(run, tool, arguments))
    assert len([t for t in api.tokens if t.get('issues')]) == 1 and not api.posts()
    assert all(row['sent'] == 0 for row in app.state.store.rows('SELECT sent FROM github_followups'))


@pytest.mark.parametrize('tool,arguments', [('github_create_issue', CREATE), ('github_comment_issue', COMMENT)])
def test_issue_write_lost_response_recovers_without_reposting(workspace, monkeypatch, tool, arguments):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    # Look-alikes without this journal's marker must not be mistaken for the write.
    api.add(5, title=CREATE['title'], body=CREATE['body'])
    api.comments[7].append({'id': 1, 'body': COMMENT['body'], 'html_url': ISSUE_URL + '7#issuecomment-1'})
    api.lose = 'applied'
    with pytest.raises(ConnectorError, match='Do not retry with a new request_key.*same request_key'):
        call(app, run_id, tool, arguments)
    recovered = call(app, run_id, tool, arguments)
    assert len(api.posts()) == 1
    if tool == 'github_create_issue':
        assert recovered['number'] == 8 and recovered['url'] == ISSUE_URL + '8'
        assert [c[2]['params'] for c in api.calls if c[0] == 'GET' and c[1] == PREFIX + '/issues'] == [
            {'state': 'all', 'sort': 'created', 'direction': 'desc', 'per_page': 100, 'page': 1}]
    else:
        assert recovered['id'] == 90001 and recovered['url'] == ISSUE_URL + '7#issuecomment-90001'
    # Later calls replay the saved receipt without reading or writing again.
    count = len([c for c in api.calls if '/issues' in c[1]])
    assert call(app, run_id, tool, arguments) == recovered and len([c for c in api.calls if '/issues' in c[1]]) == count


@pytest.mark.parametrize('tool,arguments', [('github_create_issue', CREATE), ('github_comment_issue', COMMENT)])
def test_unconfirmed_issue_write_is_not_reposted(workspace, monkeypatch, tool, arguments):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    api.lose = 'dropped'
    with pytest.raises(ConnectorError, match='may have been applied'):
        call(app, run_id, tool, arguments)
    for _ in range(2):
        with pytest.raises(ConnectorError, match='not confirmed.*will not be (created|posted) again'):
            call(app, run_id, tool, arguments)
    assert len(api.posts()) == 1


@pytest.mark.parametrize('tool,arguments', [('github_issues', {}), ('github_issue', {'number': 7}),
                                            ('github_create_issue', CREATE), ('github_comment_issue', COMMENT)])
@pytest.mark.parametrize('target', [{'repository': 'BerriAI/moyai'}, {'repository_id': 202}])
def test_issue_tools_reject_unselected_repository(workspace, monkeypatch, tool, arguments, target):
    app, _, run_id, _, api = setup(workspace, monkeypatch)
    with pytest.raises(ConnectorError):
        call(app, run_id, tool, {**arguments, 'repository': '', **target})
    assert not api.posts()
    assert not any(t.get('issues') or t.get('write') for t in api.tokens)
    assert not any('/issues' in c[1] for c in api.calls)


def token_provider(app, monkeypatch, permissions, **installation):
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
    requests = []
    async def request(method, path, **kwargs):
        requests.append((method, path, kwargs.get('json')))
        if path.endswith('/access_tokens'):
            return {'token': 'token-%d' % len(requests), 'expires_at': '2099-01-01T00:00:00Z'}
        assert path == '/app/installations/10'
        return {'account': {'id': OWNER_ID, 'type': 'Organization'}, 'permissions': permissions, 'suspended_at': None, **installation}
    monkeypatch.setattr(github, 'request', request)
    return github, requests


def test_issue_tokens_request_issue_permissions_and_cache_separately(workspace, monkeypatch):
    app, _ = workspace
    connected(app)
    github, requests = token_provider(app, monkeypatch, {**PERMISSIONS, 'issues': 'write'})
    read = asyncio.run(github.installation_token(repository='BerriAI/litellm', issues=True))
    write = asyncio.run(github.installation_token(repository='BerriAI/litellm', write=True, issues=True))
    code = asyncio.run(github.installation_token(repository='BerriAI/litellm'))
    assert len({read, write, code}) == 3
    grants = [body for method, path, body in requests if path.endswith('/access_tokens')]
    assert grants == [{'repository_ids': [101], 'permissions': {'issues': 'read'}},
                      {'repository_ids': [101], 'permissions': {'issues': 'write'}},
                      {'repository_ids': [101], 'permissions': {'contents': 'read', 'pull_requests': 'read'}}]
    # Only issue tokens check the installation's approved permissions.
    assert [path for _, path, _ in requests if not path.endswith('/access_tokens')] == ['/app/installations/10'] * 2
    count = len(requests)
    assert asyncio.run(github.installation_token(repository='BerriAI/litellm', issues=True)) == read
    assert asyncio.run(github.installation_token(repository='BerriAI/litellm', write=True, issues=True)) == write
    assert len(requests) == count and len(github.tokens) == 3


@pytest.mark.parametrize('permissions,write', [(PERMISSIONS, False), (PERMISSIONS, True), ({**PERMISSIONS, 'issues': 'read'}, True)])
def test_missing_issues_permission_is_actionable(workspace, monkeypatch, permissions, write):
    app, _ = workspace
    connected(app)
    github, requests = token_provider(app, monkeypatch, permissions)
    with pytest.raises(ConnectorError) as error:
        asyncio.run(github.installation_token(repository='BerriAI/litellm', write=write, issues=True))
    message = str(error.value)
    assert 'Issues' in message and ('Read and write' if write else 'Read') in message
    assert 'organization owner approves' in message and 'Code, pull request and ruleset access still work' in message
    assert not any(path.endswith('/access_tokens') for _, path, _ in requests)
    # Code and PR tokens are unaffected by the missing grant.
    assert asyncio.run(github.installation_token(repository='BerriAI/litellm'))


def test_issue_token_refuses_suspended_installation(workspace, monkeypatch):
    app, _ = workspace
    connected(app)
    github, requests = token_provider(app, monkeypatch, {**PERMISSIONS, 'issues': 'write'}, suspended_at='2026-10-01T00:00:00Z')
    with pytest.raises(ConnectorError, match='suspended'):
        asyncio.run(github.installation_token(repository='BerriAI/litellm', issues=True))
    assert not any(path.endswith('/access_tokens') for _, path, _ in requests)


def test_issues_permission_is_requested_for_new_apps_but_not_required():
    assert 'issues' not in PERMISSIONS and MANIFEST_PERMISSIONS['issues'] == 'write'
    assert supports_permissions(dict(PERMISSIONS))


def test_broker_advertises_and_gates_issue_tools(workspace, monkeypatch):
    app, client, run_id, headers, api = setup(workspace, monkeypatch)
    names = {'github_issues': True, 'github_issue': True, 'github_create_issue': False, 'github_comment_issue': False}
    tools = {t['name']: t for t in client.get(f'/broker/{run_id}/tools', headers=headers).json()}
    assert {name: tools[name]['annotations']['readOnlyHint'] for name in names} == names
    assert 'request_key' in tools['github_create_issue']['inputSchema']['required']
    assert 'same request_key' in tools['github_comment_issue']['description']
    connection = next(c for c in client.get('/api/connections').json() if c['id'] == 'github')
    policy = {t['name']: t for t in connection['tools']}
    assert all(policy[name]['write'] is not read and policy[name]['requires_approval'] is False for name, read in names.items())
    url = f'/broker/{run_id}/tools/call'
    # A PR-shaped call reaches no write, and an uncertain write is flagged.
    api.lose = 'dropped'
    result = client.post(url, headers=headers, json={'name': 'github_create_issue', 'arguments': CREATE}).json()
    assert result['outcome_uncertain'] is True and 'same request_key' in result['error']
    assert client.post(url, headers=headers, json={'name': 'github_create_issue',
                                                   'arguments': {**CREATE, 'title': '  '}}).status_code == 422
    assert client.patch('/api/connections/github/policy', json={'enabled': True, 'read_only': True}).status_code == 200
    names_read_only = {t['name'] for t in client.get(f'/broker/{run_id}/tools', headers=headers).json()}
    assert {'github_issues', 'github_issue'} <= names_read_only
    assert not {'github_create_issue', 'github_comment_issue'} & names_read_only
    posts = len(api.posts())
    for name, arguments in [('github_create_issue', {**CREATE, 'request_key': 'read-only-1'}), ('github_comment_issue', COMMENT)]:
        assert client.post(url, headers=headers, json={'name': name, 'arguments': arguments}).status_code == 403
    assert len(api.posts()) == posts
    issue = client.post(url, headers=headers, json={'name': 'github_issue', 'arguments': {'repository': 'BerriAI/litellm', 'number': 7}})
    assert issue.status_code == 200 and issue.json()['url'] == ISSUE_URL + '7'
    listed = client.post(url, headers=headers, json={'name': 'github_issues', 'arguments': {'repository': 'BerriAI/litellm'}})
    assert listed.status_code == 200 and [i['number'] for i in listed.json()['issues']] == [7]
