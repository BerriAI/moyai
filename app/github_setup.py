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
from .github import PERMISSIONS
from .security import digest


class ExistingApp(BaseModel):
    model_config = ConfigDict(extra='forbid')
    app_id: int = Field(gt=0)
    private_key: SecretStr = Field(min_length=100, max_length=20000)


def routes(connectors, security, store, settings):
    router = APIRouter()
    github = connectors.github

    def state_for(sid, provider):
        state = secrets.token_urlsafe(32)
        store.execute('DELETE FROM oauth_states WHERE expires<?', (time.time(),))
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
                    or data.get('owner', {}).get('login', '').lower() != github.target().split('/')[0].lower()
                    or data.get('permissions') != PERMISSIONS
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', data.get('slug', ''))):
                raise ConnectorError('Use the organization-owned App with only Contents and Pull requests write access, plus Metadata read access.')
            config['slug'] = data['slug']
            github.save_app(config)
            connectors.expire_approvals('github')
            connectors.audit('github', 'Verified existing organization GitHub App; repository access awaits installation')
            return {'url': install_url(sid)}

    @router.post('/api/connections/github/oauth')
    async def start(request: Request):
        sid = security.require(request, mutation=True, admin=True)
        github.target()
        if github.app_config():
            return {'url': install_url(sid)}
        return {'url': '/auth/github/register?' + urlencode({'state': state_for(sid, 'github_app')})}

    @router.get('/auth/github/register')
    async def registration(request: Request, state: str = ''):
        sid = security.require(request, admin=True)
        check_state(sid, 'github_app', state, consume=False)
        owner = github.target().split('/')[0]
        origin = settings.public_url.rstrip('/')
        manifest = {'name': 'Moyai Devin ' + owner, 'url': origin,
                    'description': 'Shared Moyai coding integration. Server-enforced branch and PR publishing; agents cannot approve or merge.',
                    'redirect_url': origin + '/oauth/github/app-callback',
                    'setup_url': origin + '/oauth/github/callback', 'setup_on_update': True,
                    'hook_attributes': {'url': origin + '/hooks/github', 'active': False},
                    'public': False, 'request_oauth_on_install': False, 'default_events': [],
                    'default_permissions': PERMISSIONS}
        action = 'https://github.com/organizations/' + owner + '/settings/apps/new?' + urlencode({'state': state})
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><title>Connect GitHub · Moyai Devin</title>'
            '<link rel="stylesheet" href="/static/style.css"></head><body><main style="max-width:720px;margin:60px auto;padding:24px">'
            '<h1>Connect GitHub for your organization</h1><p>Repositories: <strong>' + html.escape(', '.join(github.targets())) + '</strong></p>'
            '<p>Moyai can read code, create normal pull requests, and update or comment on PRs created by the current session without an administrator approval step. It cannot approve or merge pull requests, '
            'enable auto-merge, or change workflow and access-control files.</p>'
            '<p>GitHub combines these operations under Contents and Pull requests write permissions. The credential stays on the server; '
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
            owner = github.target().split('/')[0]
            if (data.get('owner', {}).get('login', '').lower() != owner.lower() or data.get('permissions') != PERMISSIONS
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', data.get('slug', ''))):
                raise ConnectorError('The registered GitHub App has a different owner or permissions. An administrator must inspect it.')
            config = {k: data[k] for k in ('id', 'slug', 'pem')}
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
        credentials = {'kind': 'github_app', 'installation_id': int(installation_id), 'repositories': github.targets()}
        label = await github.verify(credentials)
        connectors.expire_approvals('github')
        connectors.save('github', credentials, label)
        connectors.record_check('github', 'healthy')
        connectors.audit('github', 'Connected ' + label + ': normal PR publishing; PR reviews and merges blocked')
        return RedirectResponse('/?connection=success#connections', status_code=303)

    return router
