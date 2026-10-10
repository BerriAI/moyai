"""Local GitHub HTTP fixture for the ID migration tests and browser demo."""
import json
import httpx
from app.github import MANIFEST_PERMISSIONS
OWNER_ID = 44

def repository_data(name):
    return {'id': {'BerriAI/litellm': 101, 'BerriAI/moyai': 202}[name], 'full_name': name,
            'owner': {'id': OWNER_ID}, 'default_branch': 'main', 'private': True,
            'html_url': 'https://github.com/' + name}


class IdentityProvider:
    def __init__(self):
        self.repos = {101: repository_data('BerriAI/litellm'), 202: repository_data('BerriAI/moyai')}
        self.installed = [101, 202]
        self.calls, self.tokens = [], {}
        self.on_request = None
        self.metadata_pages = None

    def handle(self, request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if self.on_request:
            self.on_request(method, path)
        if path == '/app':
            return httpx.Response(200, json={'id': 123, 'owner': {'id': OWNER_ID, 'login': 'BerriAI', 'type': 'Organization'}})
        if path == '/app/installations/10':
            return httpx.Response(200, json={'account': {'id': OWNER_ID, 'type': 'Organization'}, 'permissions': MANIFEST_PERMISSIONS})
        if path.endswith('/access_tokens'):
            ids = body.get('repository_ids', self.installed)
            if not set(ids) <= set(self.installed):
                return httpx.Response(422)
            token = 'fixture-' + str(len(self.tokens))
            self.tokens[token] = ids
            return httpx.Response(201, json={'token': token, 'expires_at': '2099-01-01T00:00:00Z'})
        ids = self.tokens[request.headers['authorization'].removeprefix('Bearer ')]
        if path == '/installation/repositories':
            if self.metadata_pages:
                repos = self.metadata_pages[int(request.url.params.get('page', '1')) - 1]
            else:
                repos = [self.repos[i] for i in ids if i in self.installed]
                size = int(request.url.params.get('per_page', '30'))
                offset = (int(request.url.params.get('page', '1')) - 1) * size
                repos = repos[offset:offset + size]
            return httpx.Response(200, json={'repositories': repos})
        if path.startswith('/repos/'):
            repo = next((r for r in self.repos.values() if r['full_name'].lower() == path[7:].lower()), None)
            if not repo and path == '/repos/BerriAI/moyai-devin':
                return httpx.Response(301, headers={'location': 'https://api.github.com/repositories/202'})
            return httpx.Response(200 if repo else 404, json=repo or {})
        if path.startswith('/repositories/'):
            identity = int(path.split('/')[2])
            if identity not in ids or identity not in self.installed:
                return httpx.Response(404)
            if path.endswith('/git/ref/heads/main'):
                return httpx.Response(200, json={'object': {'sha': 'a' * 40}})
            return httpx.Response(200, json=self.repos[identity])
        raise AssertionError(path)
