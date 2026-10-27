"""Normalize the configured external prefix before authentication and role routing."""

from urllib.parse import SplitResult, urlsplit

from app.public_url import base_path, origin
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class PublicPathMiddleware:
    def __init__(self, app: ASGIApp, public_url: str):
        self.app, self.prefix = app, base_path(public_url)
        self.origin = origin(public_url)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.prefix and scope['type'] in {'http', 'websocket'}:
            path = scope['path']
            if path == self.prefix and scope['type'] == 'http':
                query = scope.get('query_string', b'').decode('latin-1')
                target = self.prefix + '/' + ('?' + query if query else '')
                await RedirectResponse(target, status_code=307)(scope, receive, send)
                return
            scope = dict(scope)
            if path.startswith(self.prefix + '/'):
                scope['path'] = path[len(self.prefix):]
                if raw := scope.get('raw_path'):
                    scope['raw_path'] = raw[len(self.prefix.encode()):]
            # Private health and inter-role calls retain their unprefixed paths.
            # Ingress must forward only this deployment's configured path.
            # Keep the internal path and mount root in the same coordinate system.
            # StaticFiles appends /assets to root_path when matching its mount.
            scope['root_path'] = ''
        async def external_redirect(message: Message) -> None:
            if self.prefix and message['type'] == 'http.response.start':
                headers = []
                for name, value in message.get('headers', []):
                    if name.lower() == b'location':
                        location = value.decode('latin-1')
                        parsed: SplitResult = urlsplit(location)
                        local = not parsed.netloc and location.startswith('/') and not location.startswith('//')
                        if (local or origin(location) == self.origin) and not parsed.path.startswith('/cdn-cgi/'):
                            path = parsed.path
                            if path != self.prefix and not path.startswith(self.prefix + '/'):
                                value = parsed._replace(path=self.prefix + path).geturl().encode('latin-1')
                    headers.append((name, value))
                message = {**message, 'headers': headers}
            await send(message)
        await self.app(scope, receive, external_redirect)
