"""In-memory GitHub HTTP fixture for coding-tool tests and the local demonstration."""
import base64
import copy
import json
import httpx
from app.github import MANIFEST_PERMISSIONS, supports_permissions

COMMIT = 'd' * 40
PREFIX = '/repositories/101'
REPOSITORY = {'id': 101, 'full_name': 'BerriAI/litellm', 'owner': {'id': 44},
              'default_branch': 'main', 'html_url': 'https://github.com/BerriAI/litellm', 'private': True}
PR = {'number': 100, 'node_id': 'PR_100', 'title': 'Fix', 'body': 'Before', 'state': 'open', 'draft': True,
      'head': {'ref': 'moyai/test', 'sha': COMMIT, 'repo': {'id': 101}},
      'base': {'ref': 'main', 'repo': {'id': 101}}}

class CodingProvider:
    def __init__(self):
        self.permissions = dict(MANIFEST_PERMISSIONS)
        self.calls, self.tokens, self.minted = [], {}, []
        self.lose_response = False
        self.pr = copy.deepcopy(PR)
        self.issue = {'number': 10, 'title': 'Bug', 'body': 'full body' * 5000}
        self.comments = []
        self.searches = []
        self.dispatches = []
        self.wrong_comment = self.wrong_run = self.wrong_thread = False
        self.archive = bytes(range(256)) * 600
        self.after_read = None
        self.push_run = False

    def handle(self, request):
        path, method = request.url.path, request.method
        if path.startswith('/app') or path in {'/installation/repositories', PREFIX}:
            body = json.loads(request.content) if request.content else {}
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
            if path == '/installation/repositories':
                return httpx.Response(200, json={'repositories': [REPOSITORY]})
            if path == PREFIX:
                return httpx.Response(200, json=REPOSITORY)
            raise AssertionError(path)
        if request.url.host == 'test.blob.core.windows.net':
            assert 'authorization' not in request.headers and 'cookie' not in request.headers
            return httpx.Response(200, content=self.archive)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))
        token = request.headers['authorization'].removeprefix('Bearer ')
        permissions = self.tokens[token]
        if path == PREFIX + '/pulls/100' and method == 'GET':
            assert permissions.get('pull_requests') in {'read', 'write'}
            return httpx.Response(200, json=self.pr)
        if path == '/graphql':
            assert permissions.get('pull_requests') in {'read', 'write'}
            if body['query'].startswith('query'):
                assert body['variables']['number'] == 100
                nodes = [] if self.wrong_thread else [{'id': 'THREAD_1', 'isResolved': False, 'path': 'fix.py', 'line': 1,
                    'comments': {'nodes': [{'databaseId': 25, 'body': 'Please fix', 'url': 'https://github.com/comment'}],
                                 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}]
                return httpx.Response(200, json={'data': {'repository': {'databaseId': 101, 'pullRequest': {'reviewThreads': {
                    'nodes': nodes, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}})
        if method in {'POST', 'PATCH', 'DELETE'}:
            required = 'actions' if '/actions/' in path else 'issues' if '/issues' in path else 'pull_requests'
            # PR triage is also permitted with pull_requests write by GitHub.
            assert permissions.get(required) == 'write' or ('/issues/100' in path and permissions.get('pull_requests') == 'write')
            assert '/merge' not in path and body.get('event') not in {'APPROVE', 'REQUEST_CHANGES'}
            self.dispatches.append((method, path, body))
            if self.lose_response:
                return httpx.Response(502, json={'message': 'Response lost'})
            if path == '/graphql':
                return httpx.Response(200, json={'data': {'result': {'confirmed': True}}})
            if '/actions/' in path:
                return httpx.Response(204)
            return httpx.Response(200, json={'number': 10 if '/issues' in path else 100, 'id': 25, **body})
        assert method == 'GET'
        if self.after_read:
            self.after_read(path)
        if path == PREFIX + '/issues/10':
            assert permissions.get('issues') in {'read', 'write'}
            return httpx.Response(200, json=self.issue)
        if '/comments/26' in path:
            return httpx.Response(200, json={'issue_url': 'https://api.github.com/repos/BerriAI/litellm/issues/10'})
        if '/comments/25' in path:
            number = 999 if self.wrong_comment else 100
            family = 'pulls' if '/pulls/' in path else 'issues'
            return httpx.Response(200, json={('pull_request_url' if family == 'pulls' else 'issue_url'):
                f'https://api.github.com/repos/BerriAI/litellm/{family}/{number}'})
        if path.endswith('/actions/runs/7'):
            assert permissions == {'actions': 'write'}
            return httpx.Response(200, json={'head_sha': COMMIT, 'repository': {'id': 101}, 'run_attempt': 2,
                'event': 'push' if self.push_run else 'pull_request',
                'head_branch': 'other' if self.wrong_run else self.pr['head']['ref'], 'head_repository': {'id': 101},
                'pull_requests': [] if self.push_run else [{'number': 999 if self.wrong_run else 100, 'base': {'repo': {'id': 101}}}]})
        if path.endswith('/actions/jobs/8'):
            return httpx.Response(200, json={'run_id': 7, 'head_sha': COMMIT, 'run_attempt': 2})
        if path.endswith('/actions/artifacts/9/zip'):
            return httpx.Response(302, headers={'location': 'https://test.blob.core.windows.net/artifact'})
        if path.endswith('/actions/artifacts/9'):
            return httpx.Response(200, json={'id': 9, 'expired': False})
        if path.startswith('/search/'):
            self.searches.append((path, dict(request.url.params)))
            assert 'repo:BerriAI/litellm' in request.url.params['q']
            return httpx.Response(200, json={'items': [{'body': self.issue['body']}], 'total_count': 1001, 'incomplete_results': False})
        if '/contents/' in path:
            return httpx.Response(200, json={'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(b'answer = 42').decode()})
        if '/compare/' in path or path.endswith('/commits/' + COMMIT):
            return httpx.Response(200, json={'commits': [{'sha': COMMIT}], 'files': [{'filename': 'binary.png'}]})
        items = [{'number': 10, 'body': self.issue['body']}] * 30
        if path.endswith('/artifacts'):
            return httpx.Response(200, json={'artifacts': items})
        return httpx.Response(200, json=items)

