"""Shared GitHub App. Write credentials and the finite write API stay on Render.

GitHub's pull_requests:write scope itself also permits reviews. Never expose an
installation token, generic REST proxy, receive-pack, review or merge operation.
"""
import asyncio
import hashlib
import json
import re
import time
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .connector_errors import ConnectorError
from .db import now

API = 'https://api.github.com'
PERMISSIONS = {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'}
REPOSITORY = r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*'
SHA = r'[0-9a-f]{40}'


class Args(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Repository(Args):
    repository: str = Field(default='', pattern='^(?:' + REPOSITORY + ')?$',
                            description='An allowed owner/repository. Defaults to the session repository, then the first connected repository.')


class PullRequest(Repository):
    number: int = Field(ge=1)


class Change(Args):
    path: str = Field(min_length=1, max_length=500)
    content: str | None = Field(default=None, max_length=1024 * 1024)
    executable: bool = False

    @field_validator('path')
    @classmethod
    def safe_path(cls, path):
        parts = path.split('/')
        lower = path.lower()
        if (any(p in {'', '.', '..'} or p.lower() == '.git' for p in parts)
                or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or lower.startswith(('.github/workflows/', '.github/actions/', '.ssh/'))
                or lower in {'.github', '.github/workflows', '.github/actions', '.ssh'}
                or parts[-1].lower() == 'codeowners' or parts[-1].lower().startswith('.env')
                or lower.endswith(('.pem', '.key'))):
            raise ValueError('Unsafe, credential, workflow or access-control path cannot be published.')
        return path

    @field_validator('content')
    @classmethod
    def text_only(cls, content):
        if content is not None and ('\x00' in content or len(content.encode('utf-8')) > 1024 * 1024):
            raise ValueError('Publish UTF-8 text files of at most 1 MiB each.')
        return content


class Publish(Args):
    repository: str = Field(pattern='^' + REPOSITORY + '$')
    base_sha: str = Field(pattern='^' + SHA + '$')
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')
    title: str = Field(min_length=3, max_length=250)
    body: str = Field(min_length=1, max_length=20000)
    files: list[Change] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def bounded_changes(self):
        paths = [f.path for f in self.files]
        if len(set(paths)) != len(paths) or len({p.lower() for p in paths}) != len(paths):
            raise ValueError('Duplicate file paths are not allowed.')
        lowered = {p.lower() for p in paths}
        if any('/'.join(p.split('/')[:i]).lower() in lowered for p in paths for i in range(1, len(p.split('/')))):
            raise ValueError('A file cannot also be the parent of another change.')
        if sum(len((f.content or '').encode()) for f in self.files) > 2 * 1024 * 1024:
            raise ValueError('Publish at most 2 MiB of changed text per PR.')
        return self


TOOLS = {
    'github_repositories': ('github', False, Args, 'List the repositories enabled for the shared organization GitHub connection. Use the exact owner/repository in checkout and PR tools.'),
    'github_repository': ('github', False, Repository, 'Read an allowed GitHub repository, its default branch and current commit. Shared organization access; no personal GitHub sign-in is needed.'),
    'github_checkout': ('github', False, Repository, 'Check out an allowed GitHub repository into the sandbox using read-only Git access. Choose repository explicitly when working on Moyai itself. Never grants a GitHub credential or push access.'),
    'github_pull_request': ('github', False, PullRequest, 'Read a pull request and its changed files in the connected repository. Cannot approve, review, merge or enable auto-merge.'),
    'github_create_pull_request': ('github', True, Publish, 'Publish reviewed local text changes to a new Moyai branch and open a normal, ready-for-review pull request. Requires exact-action administrator approval. Cannot update existing branches, change workflows/access controls, approve, merge or enable auto-merge. Reuse the same request_key and unchanged arguments only when explicitly recovering an uncertain publication.'),
}


class GitHub:
    def __init__(self, store, security, settings, connectors):
        self.store, self.security, self.settings, self.connectors = store, security, settings, connectors
        self.write_lock = asyncio.Lock()
        self.token_lock = asyncio.Lock()
        self.setup_lock = asyncio.Lock()
        self.tokens = {}
        store.execute('CREATE TABLE IF NOT EXISTS github_app (id INTEGER PRIMARY KEY CHECK(id=1), encrypted TEXT NOT NULL)')
        store.execute('''CREATE TABLE IF NOT EXISTS github_publications (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            arguments_hash TEXT NOT NULL, branch TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '', connection_version TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def app_config(self):
        rows = self.store.rows('SELECT encrypted FROM github_app WHERE id=1')
        return json.loads(self.security.decrypt(rows[0]['encrypted'])) if rows else {}

    def save_app(self, config):
        self.store.execute('INSERT INTO github_app VALUES(1,?) ON CONFLICT(id) DO UPDATE SET encrypted=excluded.encrypted',
                           (self.security.encrypt(json.dumps(config)),))
        self.tokens.clear()

    def targets(self):
        return self.settings.allowed_github_repositories()

    def target(self, repository=''):
        requested = repository or self.targets()[0]
        for target in self.targets():
            if target.lower() == requested.lower():
                return target
        raise ConnectorError('This repository is outside the organization GitHub allowlist.')

    def connected_targets(self, credentials):
        if credentials.get('kind') != 'github_app':
            raise ConnectorError('Connect the organization GitHub App first.')
        recorded = credentials.get('repositories', [credentials.get('repository', '')])
        if not isinstance(recorded, list) or not recorded or any(not isinstance(repo, str) for repo in recorded):
            raise ConnectorError('Reconnect the GitHub installation to confirm its repositories.')
        # Changing configuration alone never grants an existing connection more access.
        return [repo for repo in self.targets() if repo.lower() in {value.lower() for value in recorded}]

    async def selected_target(self, run, repository=''):
        connected = self.connected_targets(await self.connectors.credentials('github'))
        if not connected:
            raise ConnectorError('Reconnect GitHub to confirm the allowed repositories.')
        if not repository:
            requested = (run.get('repo_url') or '').removeprefix('https://github.com/').removesuffix('.git')
            repository = next((repo for repo in connected if repo.lower() == requested.lower()), connected[0])
        target = self.target(repository)
        if target not in connected:
            raise ConnectorError('Reconnect GitHub to grant this repository access.')
        return target

    def app_jwt(self, config=None):
        config = config or self.app_config()
        if not config.get('pem') or not config.get('id'):
            raise ConnectorError('Register the organization GitHub App in Connections first.')
        try:
            return jwt.encode({'iat': int(time.time()) - 60, 'exp': int(time.time()) + 540, 'iss': str(config['id'])},
                              config['pem'], algorithm='RS256')
        except Exception:
            raise ConnectorError('The GitHub App signing key is invalid. An administrator must reconnect it.') from None

    async def request(self, method, path, *, token='', missing=False, **kwargs):
        # Every caller below constructs a fixed GitHub API path; no user URL.
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                response = await client.request(method, API + path, headers=headers, **kwargs)
                if missing and response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    raise ConnectorError(f'GitHub did not confirm the operation ({response.status_code}). Check the installation, repository access and destination before retrying a write.')
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise ConnectorError('The GitHub response was not received. Check the destination before retrying a write.') from None

    async def verify(self, credentials):
        targets = self.connected_targets(credentials)
        if targets != self.targets():
            raise ConnectorError('Reconnect GitHub to confirm every configured repository.')
        installation = await self.request('GET', f"/app/installations/{int(credentials['installation_id'])}", token=self.app_jwt())
        if (installation.get('account', {}).get('login', '').lower() != self.target().split('/')[0].lower()
                or installation.get('account', {}).get('type') != 'Organization' or installation.get('suspended_at')
                or installation.get('permissions') != PERMISSIONS):
            raise ConnectorError('Use the organization installation with only Contents and Pull requests write access, plus Metadata read access.')
        for target in targets:
            token = await self.installation_token(credentials, repository=target, fresh=True)
            repositories = await self.request('GET', '/installation/repositories', token=token)
            if [r['full_name'].lower() for r in repositories.get('repositories', [])] != [target.lower()]:
                raise ConnectorError('A selected repository is not accessible to this installation.')
        return ', '.join(targets)

    async def installation_token(self, credentials=None, *, repository='', write=False, fresh=False):
        credentials = credentials or await self.connectors.credentials('github')
        target = self.target(repository)
        if target not in self.connected_targets(credentials):
            raise ConnectorError('The GitHub installation does not match the configured repository.')
        installation = int(credentials['installation_id'])
        config = self.app_config()
        key = (config.get('id'), installation, target.lower(), write)
        async with self.token_lock:
            cached = self.tokens.get(key)
            if not fresh and cached and cached[0] > time.time():
                return cached[1]
            permissions = {'contents': 'write' if write else 'read', 'pull_requests': 'write' if write else 'read'}
            result = await self.request('POST', f'/app/installations/{installation}/access_tokens', token=self.app_jwt(config),
                                        json={'repositories': [target.split('/')[1]], 'permissions': permissions})
            from datetime import datetime
            expiry = datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00')).timestamp() - 90
            self.tokens[key] = (expiry, result['token'])
            return result['token']

    async def repository(self, token, repository=''):
        target = self.target(repository)
        repo = await self.request('GET', '/repos/' + target, token=token)
        if repo.get('full_name', '').lower() != target.lower():
            raise ConnectorError('GitHub returned a different repository; ask an administrator to reconnect it.')
        branch = repo['default_branch']
        ref = await self.request('GET', f'/repos/{target}/git/ref/heads/{quote(branch, safe="")}', token=token)
        return {'repository': repo['full_name'], 'default_branch': branch, 'base_sha': ref['object']['sha'],
                'url': repo['html_url'], 'private': repo['private']}

    async def call(self, run, name, arguments):
        if name not in TOOLS:
            raise ConnectorError('This GitHub operation is not available.')
        if name == 'github_create_pull_request':
            async with self.write_lock:
                return await self.publish(run, Publish.model_validate(arguments))
        if name == 'github_repositories':
            return {'repositories': self.connected_targets(await self.connectors.credentials('github'))}
        target = await self.selected_target(run, arguments.get('repository', ''))
        token = await self.installation_token(repository=target)
        if name in {'github_repository', 'github_checkout'}:
            return {**await self.repository(token, target), 'git_path': '/github/' + target + '.git'}
        number = PullRequest.model_validate(arguments).number
        prefix = f'/repos/{target}/pulls/{number}'
        pr = await self.request('GET', prefix, token=token)
        files = await self.request('GET', prefix + '/files', token=token, params={'per_page': 100})
        remaining = 160000
        compact = []
        for f in files:
            patch = (f.get('patch') or '')[:min(remaining, 16000)]
            remaining -= len(patch)
            compact.append({**{k: f.get(k) for k in ('filename', 'status', 'additions', 'deletions')},
                            'patch': patch, 'patch_truncated': len(f.get('patch') or '') > len(patch)})
        return {**{k: pr.get(k) for k in ('number', 'title', 'state', 'draft', 'merged', 'html_url')},
                'body': (pr.get('body') or '')[:20000],
                'head': pr['head']['sha'], 'base': pr['base']['ref'],
                'files': compact,
                'files_truncated': pr['changed_files'] > len(files)}

    def connection_version(self):
        rows = self.store.rows("SELECT encrypted FROM connections WHERE provider='github'")
        app = self.store.rows('SELECT encrypted FROM github_app WHERE id=1')
        return hashlib.sha256(((rows[0]['encrypted'] if rows else '') + (app[0]['encrypted'] if app else '')
                              + json.dumps(self.targets())).encode()).hexdigest()

    def ensure_publish_allowed(self, run, connection_version):
        current = self.store.run(run['id'])
        if (not current or current['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or current['active_message_id'] != run['active_message_id'] or not current['token_hash']
                or current['token_hash'] != run['token_hash'] or 'github' not in current['plugins']
                or connection_version != self.connection_version()
                or not self.connectors.allowed('github_create_pull_request')):
            raise ConnectorError('The session or GitHub connection changed. Publication has stopped; inspect the destination before retrying.')

    async def validate_tree_paths(self, token, tree_sha, changes, repository=''):
        """Reject implicit directory deletion, symlinks and submodules in the base."""
        cache = {}
        async def entries(sha):
            if sha not in cache:
                tree = await self.request('GET', f'/repos/{self.target(repository)}/git/trees/{sha}', token=token)
                if tree.get('truncated'):
                    raise ConnectorError('A repository directory is too large to verify safely.')
                cache[sha] = {entry['path']: entry for entry in tree['tree']}
            return cache[sha]
        for change in changes:
            parts = change.path.split('/')
            if len(parts) > 25:
                raise ConnectorError('The file path is too deeply nested.')
            sha, found = tree_sha, None
            for i, part in enumerate(parts):
                found = (await entries(sha)).get(part) if sha else None
                if i < len(parts) - 1:
                    if found and found['type'] != 'tree':
                        raise ConnectorError('A parent path is not a regular directory.')
                    sha = found['sha'] if found else None
                elif found and (found['type'] != 'blob' or found['mode'] not in {'100644', '100755'}):
                    raise ConnectorError('Only regular text files may be changed; directories, symlinks and submodules are protected.')
            if change.content is None and not found:
                raise ConnectorError('Cannot delete a file that does not exist in the approved base.')

    async def publish(self, run, args):
        target = await self.selected_target(run, args.repository)
        version = run.get('github_connection_version') or self.connection_version()
        self.ensure_publish_allowed(run, version)
        # Stable across chat turns so a lost ACK can be resolved on a follow-up.
        identity = hashlib.sha256(f"{run['id']}:{args.request_key}".encode()).hexdigest()
        fingerprint = hashlib.sha256(args.model_dump_json().encode()).hexdigest()
        branch = f"moyai/{run['id'][:12]}/{identity[:16]}"
        self.store.execute('''INSERT OR IGNORE INTO github_publications
            (id,run_id,message_id,arguments_hash,branch,connection_version,created_at) VALUES(?,?,?,?,?,?,?)''',
            (identity, run['id'], run['active_message_id'] or 0, fingerprint, branch, version, now()))
        row = self.store.rows('SELECT * FROM github_publications WHERE id=?', (identity,))[0]
        if row['arguments_hash'] != fingerprint:
            raise ConnectorError('That request_key already describes different changes. Use a new key.')
        if row['connection_version'] != version:
            raise ConnectorError('The GitHub installation changed since this publication. Inspect the existing branch before continuing.')
        if row['result']:
            return json.loads(row['result'])
        token = await self.installation_token(repository=target, write=True)
        repo = await self.repository(token, target)
        prefix = '/repos/' + target
        commit = row['commit_sha']
        if not commit:
            if repo['base_sha'] != args.base_sha:
                comparison = await self.request('GET', prefix + '/compare/' + args.base_sha + '...' + repo['base_sha'], token=token, params={'per_page': 1})
                if comparison.get('status') not in {'ahead', 'identical'}:
                    raise ConnectorError('The approved base is not on the current default branch. Rebase and request fresh approval.')
            base = await self.request('GET', prefix + '/git/commits/' + args.base_sha, token=token)
            await self.validate_tree_paths(token, base['tree']['sha'], args.files, target)
            tree = [{'path': f.path, 'mode': '100755' if f.executable else '100644', 'type': 'blob',
                     **({'sha': None} if f.content is None else {'content': f.content})} for f in args.files]
            self.ensure_publish_allowed(run, version)
            created_tree = await self.request('POST', prefix + '/git/trees', token=token, json={'base_tree': base['tree']['sha'], 'tree': tree})
            self.ensure_publish_allowed(run, version)
            created_commit = await self.request('POST', prefix + '/git/commits', token=token,
                                                json={'message': args.title, 'tree': created_tree['sha'], 'parents': [args.base_sha]})
            commit = created_commit['sha']
            self.store.execute('UPDATE github_publications SET commit_sha=? WHERE id=?', (commit, identity))
        existing = await self.request('GET', prefix + '/git/ref/heads/' + branch, token=token, missing=True)
        if existing and existing['object']['sha'] != commit:
            raise ConnectorError('The Moyai branch changed externally. It will not be overwritten.')
        if not existing:
            self.ensure_publish_allowed(run, version)
            await self.request('POST', prefix + '/git/refs', token=token, json={'ref': 'refs/heads/' + branch, 'sha': commit})
        # Resolve a lost PR-creation ACK by reading the deterministic head branch.
        pulls = await self.request('GET', prefix + '/pulls', token=token,
                                   params={'state': 'all', 'head': target.split('/')[0] + ':' + branch, 'base': repo['default_branch']})
        if pulls:
            if len(pulls) != 1:
                raise ConnectorError('Several PRs use this branch. Inspect them before continuing.')
            pr = pulls[0]
        else:
            self.ensure_publish_allowed(run, version)
            pr = await self.request('POST', prefix + '/pulls', token=token,
                                    json={'title': args.title, 'body': args.body, 'head': branch, 'base': repo['default_branch'],
                                          'draft': False, 'maintainer_can_modify': True})
        result = {'number': pr['number'], 'url': pr['html_url'], 'branch': branch, 'commit': commit,
                  'draft': pr['draft'], 'state': pr['state'], 'repository': target}
        self.store.execute('UPDATE github_publications SET result=? WHERE id=?', (json.dumps(result), identity))
        return result
