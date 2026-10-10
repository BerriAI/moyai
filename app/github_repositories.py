"""GitHub repository identity and migration. Names are cached display metadata only."""
import asyncio
import hashlib
import json
import re

from .connector_errors import ConnectorError
from .db import now


def positive_id(value):
    return type(value) is int and value > 0


class GitHubRepositories:
    def init_repositories(self):
        self.identity_lock = asyncio.Lock()
        self.references_dirty = True
        self.store.execute('''CREATE TABLE IF NOT EXISTS github_repositories (
            installation_id INTEGER NOT NULL, repository_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL, full_name TEXT NOT NULL, aliases TEXT NOT NULL,
            PRIMARY KEY(installation_id,repository_id))''')

    def saved_credentials(self):
        rows = self.store.rows("SELECT encrypted FROM connections WHERE provider='github'")
        return json.loads(self.security.decrypt(rows[0]['encrypted'])) if rows else {}

    def connected_ids(self, credentials):
        ids = credentials.get('repository_ids')
        if credentials.get('kind') != 'github_app' or not isinstance(ids, list) or not ids or not all(map(positive_id, ids)):
            raise ConnectorError('Refresh the GitHub connection to confirm repository IDs.')
        return list(dict.fromkeys(ids))

    def repository_options(self, credentials=None):
        credentials = credentials if credentials is not None else self.saved_credentials()
        ids = credentials.get('repository_ids', [])
        rows = self.store.rows('SELECT * FROM github_repositories WHERE installation_id=?',
                               (credentials.get('installation_id', 0),))
        by_id = {r['repository_id']: r for r in rows}
        return [{'id': i, 'full_name': by_id[i]['full_name']} for i in ids if i in by_id]

    def targets(self):
        """Labels for the UI; never an authorization allowlist."""
        return [r['full_name'] for r in self.repository_options()]

    def connected_targets(self, credentials):
        return self.connected_ids(credentials)

    def target(self, repository='', credentials=None):
        credentials = credentials if credentials is not None else self.saved_credentials()
        ids = self.connected_ids(credentials)
        if repository in ('', None):
            return ids[0]
        if positive_id(repository):
            if repository in ids:
                return repository
        elif isinstance(repository, str):
            rows = self.store.rows('SELECT * FROM github_repositories WHERE installation_id=?',
                                   (credentials['installation_id'],))
            matches = [row['repository_id'] for row in rows if row['repository_id'] in ids
                       and repository.lower() in json.loads(row['aliases'])]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ConnectorError('This name has belonged to multiple repositories. Choose a permanent repository ID.')
        raise ConnectorError('This repository is not selected in the organization GitHub connection.')

    def repository_name(self, repository_id, credentials=None):
        credentials = credentials if credentials is not None else self.saved_credentials()
        rows = self.store.rows('SELECT full_name FROM github_repositories WHERE installation_id=? AND repository_id=?',
                               (credentials.get('installation_id', 0), repository_id))
        if not rows:
            raise ConnectorError('Refresh GitHub to load repository details.')
        return rows[0]['full_name']

    def remember_repository(self, data, credentials, alias=''):
        identity, name = data.get('id'), data.get('full_name', '')
        owner_id = (data.get('owner') or {}).get('id')
        if (not positive_id(identity) or owner_id != credentials.get('account_id')
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', name)):
            raise ConnectorError('GitHub returned a different repository owner or invalid repository identity.')
        rows = self.store.rows('SELECT aliases,full_name FROM github_repositories WHERE installation_id=? AND repository_id=?',
                               (credentials['installation_id'], identity))
        if not rows or rows[0]['full_name'] != name:
            self.references_dirty = True
        aliases = set(json.loads(rows[0]['aliases'])) if rows else set()
        aliases.update(x.lower() for x in (name, alias) if x)
        self.store.execute('''INSERT INTO github_repositories VALUES(?,?,?,?,?)
            ON CONFLICT(installation_id,repository_id) DO UPDATE SET
            owner_id=excluded.owner_id,full_name=excluded.full_name,aliases=excluded.aliases''',
            (credentials['installation_id'], identity, owner_id, name, json.dumps(sorted(aliases))))
        return identity

    async def installation(self, credentials):
        from .github import supports_permissions
        config = self.app_config()
        if not positive_id(config.get('owner_id')):
            app = await self.request('GET', '/app', token=self.app_jwt())
            owner = app.get('owner') or {}
            if app.get('id') != config.get('id') or owner.get('type') != 'Organization' or not positive_id(owner.get('id')):
                raise ConnectorError('Connect an organization-owned GitHub App.')
            # Owner metadata is not a credential rotation. Concurrent key changes win.
            if self.app_config() != config:
                raise ConnectorError('The GitHub App changed during verification. Retry the connection check.')
            config = {**config, 'owner_id': owner['id'], 'owner_login': owner['login']}
            self.save_app(config)
        identity = credentials.get('installation_id')
        if not positive_id(identity):
            raise ConnectorError('GitHub did not provide a valid installation ID.')
        data = await self.request('GET', f'/app/installations/{identity}', token=self.app_jwt())
        owner = data.get('account') or {}
        if (owner.get('type') != 'Organization' or owner.get('id') != config['owner_id']
                or data.get('suspended_at') or not supports_permissions(data.get('permissions'))):
            raise ConnectorError('Use the active installation belonging to this GitHub App’s organization with code and PR write permissions.')
        if credentials.get('account_id', owner['id']) != owner['id']:
            raise ConnectorError('The GitHub installation belongs to a different organization.')
        return data

    async def metadata_token(self, installation_id):
        # Broad discovery is metadata-only. Code/PR tokens always select exact IDs.
        data = await self.request('POST', f'/app/installations/{installation_id}/access_tokens',
                                  token=self.app_jwt(), json={'permissions': {'metadata': 'read'}})
        return data['token']

    async def discover_repositories(self, installation_id):
        credentials = {'kind': 'github_app', 'installation_id': installation_id}
        installed = await self.installation(credentials)
        credentials['account_id'] = installed['account']['id']
        token = await self.metadata_token(installation_id)
        ids = []
        for page in range(1, 101):
            data = await self.request('GET', '/installation/repositories', token=token,
                                      params={'per_page': 100, 'page': page})
            repositories = data.get('repositories', [])
            for repo in repositories:
                ids.append(self.remember_repository(repo, credentials))
            if len(repositories) < 100:
                break
        else:
            raise ConnectorError('Too many repositories to list completely. Narrow the GitHub App installation.')
        credentials['repository_ids'] = list(dict.fromkeys(ids))
        return credentials

    async def ensure_connection(self):
        """Convert legacy saved names once, without consulting deployment variables."""
        async with self.identity_lock:
            old = await self.connectors.credentials('github')
            if 'repository_ids' in old:
                self.connected_ids(old)
                if self.references_dirty:
                    self.migrate_references(old)
                return old
            names = old.get('repositories', [old.get('repository', '')])
            if not names or any(not isinstance(n, str) or not n for n in names):
                raise ConnectorError('Reconnect GitHub to select repositories.')
            app_row = self.store.rows('SELECT encrypted FROM github_app WHERE id=1')
            connection_row = self.store.rows("SELECT encrypted FROM connections WHERE provider='github'")
            before = hashlib.sha256((connection_row[0]['encrypted'] + (app_row[0]['encrypted'] if app_row else '') + json.dumps(names)).encode()).hexdigest()
            original = self.store.rows("SELECT encrypted FROM connections WHERE provider='github'")[0]['encrypted']
            installed = await self.installation(old)
            updated = {'kind': 'github_app', 'installation_id': old['installation_id'],
                       'account_id': installed['account']['id'], 'repository_ids': []}
            expected = self.connection_version()
            token = await self.metadata_token(old['installation_id'])
            for name in names:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                    raise ConnectorError('A saved GitHub repository name is invalid.')
                repo = await self.request('GET', '/repos/' + name, token=token, repository_redirect=True)
                updated['repository_ids'].append(self.remember_repository(repo, updated, alias=name))
            updated['repository_ids'] = list(dict.fromkeys(updated['repository_ids']))
            await self.verify(updated)
            if self.connection_version() != expected:
                raise ConnectorError('The GitHub connection changed during migration. Retry the connection check.')
            label = ', '.join(self.repository_name(i, updated) for i in updated['repository_ids'])
            with self.store.connect() as conn:
                if not conn.execute("UPDATE connections SET encrypted=?,label=?,updated_at=? WHERE provider='github' AND encrypted=?",
                                    (self.security.encrypt(json.dumps(updated)), label, now(), original)).rowcount:
                    raise ConnectorError('The GitHub connection changed during migration. Retry the connection check.')
            self.migrate_references(updated, before, self.connection_version())
            self.connectors.audit('github', 'Migrated the existing repository selection to permanent GitHub IDs')
            return updated

    async def refresh_repository(self, repository_id, credentials=None):
        credentials = credentials if credentials is not None else await self.ensure_connection()
        token = await self.installation_token(credentials, repository=repository_id, rules=True)
        repo = await self.request('GET', f'/repositories/{repository_id}', token=token)
        if repo.get('id') != repository_id:
            raise ConnectorError('GitHub returned a different repository ID.')
        self.remember_repository(repo, credentials)
        return repo

    async def selected_target(self, run, repository='', repository_id=None):
        credentials = await self.ensure_connection()
        expected = self.connection_version()
        requested = repository_id if repository_id is not None else repository
        if not requested:
            requested = run.get('github_repository_id') or (run.get('repo_url') or '').removeprefix('https://github.com/').removesuffix('.git')
        try:
            target = self.target(requested, credentials)
        except ConnectorError:
            if repository_id is not None or positive_id(requested):
                raise
            # A new spelling may be a rename of one of the already selected IDs.
            for identity in self.connected_ids(credentials):
                await self.refresh_repository(identity, credentials)
            target = self.target(requested, credentials)
        await self.refresh_repository(target, credentials)
        if self.connection_version() != expected:
            raise ConnectorError('The GitHub connection changed during repository lookup. Retry the operation.')
        if repository_id is not None and repository and self.target(repository, credentials) != target:
            raise ConnectorError('The repository name and ID identify different repositories.')
        if run.get('id') and not repository and repository_id is None:
            self.store.execute('UPDATE runs SET github_repository_id=?,repo_url=? WHERE id=?',
                               (target, 'https://github.com/' + self.repository_name(target), run['id']))
        if self.references_dirty:
            self.migrate_references(credentials)
        return target

    def migrate_references(self, credentials, old_version=None, new_version=None):
        """Backfill identity on saved work; labels may follow a verified rename."""
        rows = self.store.rows('SELECT * FROM github_repositories WHERE installation_id=?', (credentials['installation_id'],))
        selected = set(self.connected_ids(credentials))
        names = {}
        for row in rows:
            if row['repository_id'] in selected:
                for alias in json.loads(row['aliases']):
                    names[alias] = row['repository_id'] if alias not in names or names[alias] == row['repository_id'] else None
        labels = {r['repository_id']: r['full_name'] for r in rows if r['repository_id'] in selected}

        def identify(value):
            return names.get((value or '').removeprefix('https://github.com/').removesuffix('.git').lower())

        def recipe(value, rename=True):
            identity = value.get('repository_id') or identify(value.get('repository'))
            if identity in labels:
                value['repository_id'] = identity
                if rename:
                    value['repository'] = labels[identity]
            return value

        with self.store.connect() as conn:
            for run in conn.execute("SELECT id,repo_url,github_repository_id FROM runs WHERE repo_url!='' OR github_repository_id IS NOT NULL").fetchall():
                identity = run['github_repository_id'] or identify(run['repo_url'])
                if identity in labels:
                    conn.execute('UPDATE runs SET github_repository_id=?,repo_url=? WHERE id=?',
                                 (identity, 'https://github.com/' + labels[identity], run['id']))
            tables = conn.table_names()
            for table in ('environments', 'environment_builds'):
                if table in tables:
                    for row in conn.execute(f'SELECT id,recipe FROM {table}').fetchall():
                        value = json.loads(row['recipe'])
                        updated = json.dumps(recipe(value, rename=table == 'environments'))
                        if updated != row['recipe']:
                            conn.execute(f'UPDATE {table} SET recipe=? WHERE id=?', (updated, row['id']))
            if 'automations' in tables:
                for row in conn.execute('SELECT id,definition FROM automations').fetchall():
                    value = json.loads(row['definition'])
                    identity = value.get('github_repository_id') or identify(value.get('repo_url'))
                    if identity in labels:
                        value.update(github_repository_id=identity, repo_url='https://github.com/' + labels[identity])
                    if value.get('event', {}).get('provider') == 'github':
                        recipe(value['event'])
                    for trigger in value.get('triggers', []):
                        event = trigger.get('event')
                        if event and event.get('provider') == 'github':
                            recipe(event)
                    updated = json.dumps(value)
                    if updated != row['definition']:
                        conn.execute('UPDATE automations SET definition=? WHERE id=?', (updated, row['id']))
            for table in ('github_publications', 'github_followups'):
                for row in conn.execute(f"SELECT id,result,connection_version FROM {table} WHERE result!=''").fetchall():
                    value = json.loads(row['result'])
                    identity = value.get('repository_id') or identify(value.get('repository'))
                    if identity not in labels:
                        continue
                    value['repository_id'] = identity
                    version = new_version if old_version and row['connection_version'] == old_version else row['connection_version']
                    conn.execute(f'UPDATE {table} SET result=?,connection_version=? WHERE id=?', (json.dumps(value), version, row['id']))

        if self.saved_credentials() == credentials:
            self.store.execute("UPDATE connections SET label=? WHERE provider='github'", (', '.join(labels[i] for i in self.connected_ids(credentials) if i in labels),))
        self.references_dirty = False

    async def public_repository(self, repository='', repository_id=None):
        # Public clones also pin identity, without granting any installation access.
        if repository_id is not None and not positive_id(repository_id):
            raise ConnectorError('Choose a valid GitHub repository ID.')
        if repository_id is None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
            raise ConnectorError('Choose a GitHub repository.')
        path = f'/repositories/{repository_id}' if repository_id else '/repos/' + repository
        repo = await self.request('GET', path, repository_redirect=repository_id is None)
        if (not positive_id(repo.get('id')) or repo.get('private') is not False
                or (repository_id and repo['id'] != repository_id)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repo.get('full_name', ''))):
            raise ConnectorError('GitHub did not confirm this public repository.')
        return repo

    async def refresh_connection(self):
        credentials = await self.ensure_connection()
        expected = self.connection_version()
        label = await self.verify(credentials)
        if self.connection_version() != expected:
            raise ConnectorError('The GitHub connection changed during verification. Retry the connection check.')
        self.migrate_references(credentials)
        # Metadata refresh does not revoke in-flight writes or workspace-published PRs.
        self.store.execute("UPDATE connections SET label=? WHERE provider='github'", (label,))
        self.connectors.record_check('github', 'healthy')
        return credentials
