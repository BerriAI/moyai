"""Attach edge credentials only to the configured broker; never follow redirects."""
import os
import urllib.request
from urllib.parse import urlsplit


def broker_headers(url: str, token: str) -> dict[str, str]:
    headers = {'Authorization': 'Bearer ' + token}
    origin, client_id, secret = (os.environ.get(name, '') for name in (
        'WORKSPACE_ACCESS_ORIGIN', 'WORKSPACE_ACCESS_CLIENT_ID', 'WORKSPACE_ACCESS_CLIENT_SECRET'))
    if not any((origin, client_id, secret)):
        return headers
    target, expected = urlsplit(url), urlsplit(origin)
    if (not all((origin, client_id, secret)) or target.scheme != 'https'
            or (target.scheme, target.netloc) != (expected.scheme, expected.netloc)
            or expected.path not in {'', '/'} or expected.query or expected.fragment
            or target.username or target.password or target.fragment
            or not target.path.startswith('/broker/')
            or any('\r' in value or '\n' in value for value in (client_id, secret))):
        raise ValueError('Cloudflare credentials require the configured HTTPS broker destination.')
    # Cloudflare's browser integrity checks reject Python's default user agent
    # before evaluating the service-token policy. Identify our machine client.
    return {**headers, 'User-Agent': 'Moyai/1.0',
            'CF-Access-Client-Id': client_id, 'CF-Access-Client-Secret': secret}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_broker(request, **kwargs):
    return urllib.request.build_opener(NoRedirect()).open(request, **kwargs)


def git_environment(remote: str, token: str) -> dict[str, str]:
    headers = broker_headers(remote, token)
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': str(len(headers) + 1)}
    for index, (name, value) in enumerate(headers.items()):
        env[f'GIT_CONFIG_KEY_{index}'] = 'http.' + remote.rstrip('/') + '/.extraHeader'
        env[f'GIT_CONFIG_VALUE_{index}'] = name + ': ' + value
    env[f'GIT_CONFIG_KEY_{len(headers)}'] = 'http.followRedirects'
    env[f'GIT_CONFIG_VALUE_{len(headers)}'] = 'false'
    return env
