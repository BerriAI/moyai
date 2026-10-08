"""An optional outer identity check. Never substitutes for Moyai authorization."""
import asyncio
import time

import httpx
import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .config import Settings


class AccessUnavailable(Exception):
    pass


class CloudflareAccess:
    def __init__(self, settings: Settings):
        self.enabled = bool(settings.cloudflare_access_team_domain)
        self.issuer = 'https://' + settings.cloudflare_access_team_domain
        self.audience = settings.cloudflare_access_audience
        self.broker_audience = settings.cloudflare_access_broker_audience
        self.webhooks = {'/hooks/slack/events', '/hooks/slack/interactions',
                         *settings.cloudflare_access_webhook_paths}
        self.keys = {}
        self.expires = 0
        self.last_fetch = float('-inf')
        self.lock = asyncio.Lock()

    async def signing_key(self, kid: str):
        now = time.monotonic()
        if kid in self.keys and now < self.expires:
            return self.keys[kid]
        async with self.lock:
            now = time.monotonic()
            if kid in self.keys and now < self.expires:
                return self.keys[kid]
            # Unknown key IDs must not turn anonymous requests into an unbounded
            # stream of requests to the identity provider. Rotation retries later.
            if now - self.last_fetch < 10:
                if now >= self.expires:
                    raise AccessUnavailable()
                raise jwt.InvalidTokenError()
            self.last_fetch = now
            try:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                    response = await client.get(self.issuer + '/cdn-cgi/access/certs')
                    response.raise_for_status()
                if len(response.content) > 65536:
                    raise ValueError()
                keys = response.json()['keys']
                self.keys = {key['kid']: jwt.PyJWK.from_dict(key).key for key in keys
                             if key.get('kty') == 'RSA' and key.get('alg') == 'RS256'
                             and key.get('use') == 'sig' and isinstance(key.get('kid'), str)}
                if not self.keys:
                    raise ValueError()
                self.expires = now + 3600
            except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
                raise AccessUnavailable() from None
            if kid not in self.keys:
                raise jwt.InvalidTokenError()
            return self.keys[kid]

    async def check(self, path: str, method: str, token: str):
        if not self.enabled:
            return
        # Keep the health response minimal. Webhook handlers still authenticate
        # their provider before accepting work; no general /hooks/* exemption.
        if (path == '/health' and method in {'GET', 'HEAD'}) or (path in self.webhooks and method == 'POST'):
            return
        if not token or len(token) > 16384:
            raise jwt.InvalidTokenError()
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
            raise jwt.InvalidTokenError()
        audience = self.broker_audience if path.startswith('/broker/') else self.audience
        claims = jwt.decode(token, await self.signing_key(header['kid']), algorithms=['RS256'],
                            audience=audience, issuer=self.issuer,
                            options={'require': ['iss', 'aud', 'exp', 'iat']})
        # An assertion covering both applications would defeat their separation.
        audiences = claims['aud'] if isinstance(claims['aud'], list) else [claims['aud']]
        if set(audiences) != {audience} or claims.get('type') != 'app':
            raise jwt.InvalidTokenError()


class CloudflareAccessMiddleware:
    def __init__(self, app, access: CloudflareAccess):
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        try:
            await self.access.check(scope['path'], scope.get('method', 'GET'),
                                    Headers(scope=scope).get('cf-access-jwt-assertion', ''))
        except (jwt.PyJWTError, ValueError, TypeError):
            status, detail = 401, 'Cloudflare Access authentication required.'
        except AccessUnavailable:
            status, detail = 503, 'Cloudflare Access verification is temporarily unavailable.'
        else:
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 4401 if status == 401 else 1013})
            return
        await JSONResponse({'detail': detail}, status_code=status,
                           headers={'Cache-Control': 'no-store'})(scope, receive, send)
