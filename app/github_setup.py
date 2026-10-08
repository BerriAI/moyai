"""Admin-only GitHub App manifest/installation flow; no per-user GitHub OAuth."""
import html
import json
import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .connector_errors import ConnectorError
from .github import MANIFEST_PERMISSIONS, supports_permissions
from .github_repositories import positive_id
from .security import digest


class ExistingApp(BaseModel):
    model_config = ConfigDict(extra='forbid')
    app_id: int = Field(gt=0)
    private_key: SecretStr = Field(min_length=100, max_length=20000)


def routes(connectors, security, store, settings):
    router = APIRouter()
    github = connectors.github
    store.execute('CREATE TABLE IF NOT EXISTS github_registration (state_hash TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, owner_login TEXT NOT NULL)')

    def state_for(sid, provider):
        state = secrets.token_urlsafe(32)
        store.execute('DELETE FROM oauth_states WHERE expires<?', (time.time(),))
        store.execute('DELETE FROM github_registration WHERE state_hash NOT IN (SELECT state_hash FROM oauth_states)')
        store.execute('INSERT INTO oauth_states VALUES(?,?,?,?)', (digest(state), provider, digest(sid), time.time() + 900))
        return state

    def check_state(sid, provider, state, *, consume=True):
        query = ('DELETE FROM oauth_states' if consume else 'SELECT state_hash FROM oauth_states')
        query += ' WHERE state_hash=? AND provider=? AND session_id=? AND expires>?'
        if consume:
            query += ' RETURNING state_hash'
        with store.connect() as conn:
            rows = conn.execute(query, (digest(state), provider, digest(sid), time.time())).fetchall()
        if not rows:
            raise HTTPException(400, 'Expired or invalid GitHub setup. Start again from Connections.')

    def install_url(sid):
        slug = github.app_config()['slug']
        return 'https://github.com/apps/' + slug + '/installations/new?' + urlencode({'state': state_for(sid, 'github')})

    @router.post('/api/connections/github/app')
    async def existing_app(request: Request):
        sid = security.require(request, mutation=True, admin=True)
        raw = await request.body()
        if len(raw) > 24000:
            raise HTTPException(413, 'The GitHub App key file is too large.')
        try:
            args = ExistingApp.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, 'Provide a GitHub App ID and its PEM private key.') from None
        async with github.setup_lock:
            previous = github.app_config()
            if previous and previous.get('id') != args.app_id:
                raise HTTPException(409, 'A different GitHub App is already registered. Keep the existing organization app.')
            config = {'id': args.app_id, 'pem': args.private_key.get_secret_value()}
            data = await github.request('GET', '/app', token=github.app_jwt(config))
            if (data.get('id') != args.app_id or data.get('owner', {}).get('type') != 'Organization'
                    or not positive_id(data.get('owner', {}).get('id'))
                    or not supports_permissions(data.get('permissions'))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', data.get('slug', ''))):
                raise ConnectorError('Use the organization-owned App with Contents and Pull requests write access, plus Metadata read access. Additional permissions are supported.')
            config.update(slug=data['slug'], owner_id=data['owner']['id'], owner_login=data['owner']['login'])
            github.save_app(config)
            connectors.expire_approvals('github')
            connectors.audit('github', 'Verified existing organization GitHub App; repository access awaits installation')
            return {'url': install_url(sid)}

    @router.post('/api/connections/github/oauth')
    async def start(request: Request):
        sid = security.require(request, mutation=True, admin=True)
        if github.app_config():
            if github.saved_credentials():
                await github.refresh_connection()
                return {'connected': True}
            return {'url': install_url(sid)}
        try:
            args = json.loads(await request.body() or b'{}')
            owner = args.get('organization', '')
        except (ValueError, AttributeError):
            raise HTTPException(422, 'Enter your GitHub organization.') from None
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', owner):
            raise HTTPException(422, 'Enter your GitHub organization.')
        organization = await github.request('GET', '/orgs/' + owner)
        if not positive_id(organization.get('id')) or organization.get('type') != 'Organization' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', organization.get('login', '')):
            raise ConnectorError('GitHub did not confirm the organization.')
        state = state_for(sid, 'github_app')
        store.execute('INSERT INTO github_registration VALUES(?,?,?)',
                      (digest(state), organization['id'], organization['login']))
        return {'url': '/auth/github/register?' + urlencode({'state': state})}

    def registration_owner(state):
        rows = store.rows('SELECT * FROM github_registration WHERE state_hash=?', (digest(state),))
        if not rows:
            raise HTTPException(400, 'Start GitHub registration again from Connections.')
        return rows[0]

    @router.post('/api/connections/github/refresh')
    async def refresh(request: Request):
        security.require(request, mutation=True, admin=True)
        await github.refresh_connection()
        return {'connected': True, 'repositories': github.repository_options()}

    @router.get('/api/connections/github/repositories')
    async def repositories(request: Request):
        security.require(request, admin=True)
        current = await github.ensure_connection()
        discovered = await github.discover_repositories(current['installation_id'])
        options = {r['id']: r for r in github.repository_options(discovered)}
        options.update({r['id']: r for r in github.repository_options(current)})
        return {'repositories': sorted(options.values(), key=lambda r: r['full_name'].lower()),
                'selected_ids': github.connected_ids(current),
                'installation_url': f"https://github.com/apps/{github.app_config()['slug']}/installations/{current['installation_id']}"}

    @router.post('/api/connections/github/repositories')
    async def select_repositories(request: Request):
        security.require(request, mutation=True, admin=True)
        try:
            raw = json.loads(await request.body())
            ids = raw['repository_ids']
            if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or not all(map(positive_id, ids)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, 'Select at least one repository using its GitHub ID.') from None
        async with github.setup_lock:
            current = await github.ensure_connection()
            expected = github.connection_version()
            discovered = await github.discover_repositories(current['installation_id'])
            allowed = set(discovered['repository_ids']) | set(current['repository_ids'])
            if not set(ids) <= allowed:
                raise HTTPException(403, 'A selected repository is unavailable to this installation.')
            updated = {**current, 'repository_ids': list(dict.fromkeys(ids))}
            label = await github.verify(updated)
            if github.connection_version() != expected:
                raise HTTPException(409, 'The GitHub connection changed. Reload before saving repositories.')
            if set(updated['repository_ids']) != set(current['repository_ids']):
                connectors.expire_approvals('github')
                connectors.save('github', updated, label)
            github.migrate_references(updated)
            store.execute("UPDATE connections SET label=? WHERE provider='github'", (label,))
            connectors.record_check('github', 'healthy')
            connectors.audit('github', 'Updated selected permanent repository IDs: ' + ', '.join(map(str, ids)))
            return {'repositories': github.repository_options(updated)}

    @router.get('/auth/github/register')
    async def registration(request: Request, state: str = ''):
        sid = security.require(request, admin=True)
        check_state(sid, 'github_app', state, consume=False)
        owner = registration_owner(state)['owner_login']
        origin = settings.public_url.rstrip('/')
        manifest = {'name': 'Moyai ' + owner, 'url': origin,
                    'description': 'Shared Moyai integration for code, PRs and ruleset required reviewers; agents cannot approve or merge PRs.',
                    'redirect_url': origin + '/oauth/github/app-callback',
                    'setup_url': origin + '/oauth/github/callback', 'setup_on_update': True,
                    'hook_attributes': {'url': origin + '/hooks/github', 'active': False},
                    'public': False, 'request_oauth_on_install': False, 'default_events': [],
                    'default_permissions': MANIFEST_PERMISSIONS}
        action = 'https://github.com/organizations/' + owner + '/settings/apps/new?' + urlencode({'state': state})
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><title>Connect GitHub · Moyai</title>'
            '<link rel="stylesheet" href="/static/style.css"></head><body><main style="max-width:720px;margin:60px auto;padding:24px">'
            '<h1>Connect GitHub for your organization</h1><p>Organization: <strong>' + html.escape(owner) + '</strong>. Select repositories on GitHub; manage the selection in Moyai afterward.</p>'
            '<p>Moyai can read code, create normal pull requests, and update or comment on Moyai PRs created by any chat in this workspace under the current GitHub connection without an administrator approval step. It cannot approve or merge pull requests, '
            'enable auto-merge, or change workflow and access-control files.</p>'
            '<p>Moyai can inspect repository rulesets and change their required reviewing teams and file patterns when requested. '
            'Ruleset edits preserve approval counts, code owner review, status checks and other settings. '
            'This requires Administration write access in addition to Contents and Pull requests write permissions. The credential stays on the server; '
            'Moyai agents receive only the specific operations listed above. No personal GitHub sign-in is needed for teammates.</p>'
            '<form method="post" action="' + html.escape(action, quote=True) + '"><input type="hidden" name="manifest" value="'
            + html.escape(json.dumps(manifest), quote=True) + '"><button type="submit">Continue to GitHub</button></form></main></body></html>')

    @router.get('/oauth/github/app-callback')
    async def app_callback(request: Request, state: str = '', code: str = ''):
        sid = security.require(request, admin=True)
        check_state(sid, 'github_app', state)
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', code):
            raise HTTPException(400, 'GitHub did not provide a valid registration code.')
        async with github.setup_lock:
            if github.app_config():
                raise HTTPException(409, 'An organization GitHub App is already registered. Connect that app instead.')
            data = await github.request('POST', '/app-manifests/' + code + '/conversions')
            owner = registration_owner(state)
            if (data.get('owner', {}).get('id') != owner['owner_id'] or not supports_permissions(data.get('permissions'))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', data.get('slug', ''))):
                raise ConnectorError('The registered GitHub App has a different owner or permissions. An administrator must inspect it.')
            config = {k: data[k] for k in ('id', 'slug', 'pem')}
            config.update(owner_id=owner['owner_id'], owner_login=owner['owner_login'])
            store.execute('DELETE FROM github_registration WHERE state_hash=?', (digest(state),))
            github.app_jwt(config)
            github.save_app(config)
            connectors.audit('github', 'Registered organization GitHub App; repository access awaits installation')
            return RedirectResponse(install_url(sid), status_code=303)

    @router.get('/oauth/github/callback')
    async def install_callback(request: Request, state: str = '', installation_id: str = '', setup_action: str = ''):
        sid = security.require(request, admin=True)
        check_state(sid, 'github', state)
        if not installation_id.isdecimal() or len(installation_id) > 20 or setup_action not in {'install', 'update'}:
            raise HTTPException(400, 'GitHub did not confirm an installation. Start again from Connections.')
        async with github.setup_lock:
            current = await github.ensure_connection() if github.saved_credentials() else None
            expected = github.connection_version()
            credentials = await github.discover_repositories(int(installation_id))
            if current:
                if current['installation_id'] != credentials['installation_id']:
                    raise HTTPException(409, 'Disconnect the old installation before connecting a different one.')
                credentials = current  # Updating GitHub access never silently widens Moyai's selection.
            label = await github.verify(credentials)
            if github.connection_version() != expected:
                raise HTTPException(409, 'The GitHub connection changed. Start setup again.')
            if not current:
                connectors.expire_approvals('github')
                connectors.save('github', credentials, label)
            else:
                github.migrate_references(credentials)
                store.execute("UPDATE connections SET label=? WHERE provider='github'", (label,))
        connectors.record_check('github', 'healthy')
        connectors.audit('github', 'Connected ' + label + ': PR publishing and ruleset inspection; reviewer edits require Administration write; PR reviews and merges blocked')
        return RedirectResponse('/?connection=success#connections', status_code=303)

    return router
