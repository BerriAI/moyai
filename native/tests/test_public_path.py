"""Path routing exercises the real API; these are supplementary to local E2E."""
import re

import httpx
import pytest
from app.config import Settings as ProductSettings
from app.public_url import app_path, base_path
from app.security import Security as ProductSecurity
from starlette.requests import Request
from starlette.responses import Response

from moyai.api import create_app
from moyai.deployment_config import InstallationError, _public_origin


@pytest.mark.parametrize('prefix', ['', '/moyai', '/tools/moyai'])
async def test_assets_login_mutations_download_and_logout_under_prefix(runtime, prefix, tmp_path, monkeypatch):
    # Native-only CI does not build Vite. Exercise the HTML/static serving
    # contract here; the actual built frontend is covered by browser/local E2E.
    static = tmp_path / 'static'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<html><head><!--moyai-base-->'
                                     '<script src="./assets/proof.js"></script></head></html>')
    (static / 'assets/proof.js').write_text('/* static serving proof */')
    monkeypatch.setattr('moyai.api.STATIC', static)
    runtime.settings.public_url = 'http://testserver' + prefix
    app = create_app(runtime, run_dispatcher=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        if prefix:
            response = await client.get(prefix + '?tab=1')
            assert response.status_code == 307
            assert response.headers['location'] == prefix + '/?tab=1'
        page = await client.get(prefix + '/')
        assert f'<base href="{prefix}/">' in page.text
        assets = re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', page.text)
        assert assets
        for asset in assets:
            result = await client.get(prefix + '/' + asset)
            assert result.status_code == 200, asset
            assert 'text/html' not in result.headers['content-type']
        denied = await client.post(prefix + '/api/login', headers={'Origin': 'https://other.test'}, json={
            'username': 'admin', 'password': 'test-password-12345',
        })
        assert denied.status_code == 403
        if prefix:
            # A sibling/root Moyai deployment's cookie must not shadow this login.
            client.cookies.set('moyai_session', 'unrelated-root-cookie', path='/')
        client.headers['Origin'] = 'http://testserver'
        login = await client.post(prefix + '/api/login', json={'username': 'admin', 'password': 'test-password-12345'})
        assert login.status_code == 200
        assert f'Path={prefix or "/"};' in login.headers['set-cookie']
        client.headers['X-Moyai-CSRF'] = login.json()['csrf']
        assert (await client.get(prefix + '/api/me')).status_code == 200
        session = await client.post(prefix + '/api/sessions', json={'title': 'Path proof'})
        assert session.status_code < 300
        upload = await client.post(prefix + '/api/uploads?name=proof.txt', content=b'path proof',
                                   headers={'Content-Type': 'text/plain'})
        assert upload.status_code < 300, upload.text
        downloaded = await client.get(prefix + '/api/uploads/' + upload.json()['id'])
        assert downloaded.content == b'path proof'
        logout = await client.post(prefix + '/api/logout')
        assert logout.status_code == 200
        assert f'Path={prefix or "/"};' in logout.headers['set-cookie']
        assert (await client.get(prefix + '/api/me')).status_code == 401
        assert (await client.get('/health')).status_code == 200


@pytest.mark.parametrize('path', ['/moyai', '/tools/moyai', ''])
def test_origin_and_product_cookies_are_distinct_from_application_base(tmp_path, path):
    url = 'https://gateway.example.com' + path
    settings = ProductSettings(_env_file=None, data_dir=tmp_path, public_url=url,
                               password_login_enabled=True, workspace_password='test-password-12345')
    security = ProductSecurity(settings)
    assert security.origin == 'https://gateway.example.com'
    assert (security.session_cookie == 'workspace_session') is (not path)
    security.check_origin(Request({'type': 'http', 'headers': [(b'origin', b'https://gateway.example.com')]}))
    response = Response()
    security.new_session(response)
    assert f'Path={path or "/"};' in response.headers['set-cookie']
    assert app_path(url, '/auth/google/callback') == path + '/auth/google/callback'
    assert _public_origin(url) == url


@pytest.mark.parametrize('path', ['/../x', '/%2fadmin', '/moyai//x', '/moyai//', '/moyai\n', '/moyai;x', '/moyai\\x'])
def test_ambiguous_paths_fail_closed_without_echoing_configuration(path):
    with pytest.raises(ValueError):
        base_path('https://example.test' + path)
    with pytest.raises(InstallationError) as failure:
        _public_origin('https://example.test' + path)
    assert 'example.test' not in str(failure.value)


async def test_role_and_stream_paths_are_normalized_before_authentication():
    from moyai.public_path import PublicPathMiddleware
    seen = []

    async def downstream(scope, receive, send):
        seen.append((scope['type'], scope['path'], scope['raw_path'], scope['root_path']))
        await send({'type': 'websocket.close', 'code': 1008} if scope['type'] == 'websocket' else {
            'type': 'http.response.start', 'status': 401, 'headers': [],
        })

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        pass

    app = PublicPathMiddleware(downstream, 'https://gateway.test/moyai')
    for kind, path in [('http', '/broker/native/v1/responses'), ('http', '/hooks/slack'),
                       ('websocket', '/api/sessions/session/computer/stream'), ('http', '/health')]:
        external = '/moyai' + path if path != '/health' else path
        await app({'type': kind, 'path': external, 'raw_path': external.encode()}, receive, send)
        assert seen[-1] == (kind, path, path.encode(), '')


async def test_redirects_preserve_prefix_query_and_provider_endpoints():
    from moyai.public_path import PublicPathMiddleware
    for location, expected in [
        ('/api/sessions/', '/moyai/api/sessions/'),
        ('/?connection=success#connections', '/moyai/?connection=success#connections'),
        ('/moyai/#tasks', '/moyai/#tasks'),
        ('https://gateway.test/auth/callback', 'https://gateway.test/moyai/auth/callback'),
        ('https://accounts.google.com/o/oauth2/v2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'),
        ('/cdn-cgi/access/logout', '/cdn-cgi/access/logout'),
    ]:
        sent = []

        async def downstream(scope, receive, send, target=location):
            await Response(status_code=303, headers={'Location': target})(scope, receive, send)

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message, messages=sent):
            messages.append(message)

        app = PublicPathMiddleware(downstream, 'https://gateway.test/moyai')
        await app({'type': 'http', 'path': '/moyai/', 'raw_path': b'/moyai/'}, receive, send)
        assert dict(sent[0]['headers'])[b'location'].decode() == expected
