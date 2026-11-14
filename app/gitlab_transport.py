"""Fixed-origin GitLab transport; credentials never follow DNS or HTTP redirects."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import unquote, urlsplit

import httpx

from .connector_errors import ConnectorError

DEFAULT_BASE_URL = 'https://gitlab.com'
MAX_PATH_LENGTH = 8192


def preflight_error(message: str, code: str = 'gitlab_invalid_request') -> ConnectorError:
    return ConnectorError(message, code=code, dispatched=False)


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return (
        address.is_global and not address.is_multicast and not address.is_reserved
        and not (
            isinstance(address, ipaddress.IPv6Address)
            and (address.ipv4_mapped or address.sixtofour or address.teredo)
        )
    )


def normalize_base_url(value: str, *, allow_loopback_http: bool = False) -> str:
    """Canonical origin only. Private HTTPS targets are deliberately unsupported."""
    try:
        if (
            not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in '\\%?#')
        ):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            not host or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or parsed.port == 0 or host.endswith('.')
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if not re.fullmatch(
                r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
                r'(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', host,
            ):
                raise ValueError
        loopback = bool(address and address.is_loopback)
        if parsed.scheme == 'http':
            if not allow_loopback_http or not loopback:
                raise ValueError
        elif parsed.scheme != 'https' or host == 'localhost' or loopback or (address and not _public_address(host)):
            raise ValueError
        return str(httpx.URL(value).copy_with(path='', query=None, fragment=None))
    except (ValueError, httpx.InvalidURL):
        raise preflight_error(
            'Use a GitLab HTTPS origin without credentials, a path, query, or fragment. '
            'Literal-loopback HTTP requires explicit local-test configuration.',
            'gitlab_invalid_origin',
        ) from None


def validate_path(path: str) -> str:
    """Literal transport routes; request validation restricts discovery/GraphQL."""
    if (
        not isinstance(path, str) or not path or len(path) > MAX_PATH_LENGTH
        or any(ord(c) < 33 or ord(c) > 126 for c in path)
        or any(c in path for c in '\\?#')
        or re.search(r'%(?![0-9A-Fa-f]{2})', path)
        or not (
            path in {'/user', '/personal_access_tokens/self', '/projects', '/graphql'}
            or re.fullmatch(r'/projects/[1-9][0-9]{0,18}(?:/[^/]+)*', path)
        )
    ):
        raise preflight_error('Use a permitted relative GitLab API path.')
    # Repeated decoding catches ambiguous paths through upstream reverse proxies.
    decoded = path
    while True:
        if (
            any(c in decoded for c in '\\\x00\r\n')
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in {'.', '..', ''} for part in decoded.split('/')[1:])
        ):
            raise preflight_error('GitLab API paths cannot contain traversal or ambiguous separators.')
        try:
            expanded = unquote(decoded, errors='strict')
        except UnicodeError:
            raise preflight_error('GitLab API paths must use valid UTF-8 encoding.') from None
        if expanded == decoded:
            return path
        decoded = expanded


class GitLabTransport(httpx.AsyncBaseTransport):
    """Pin DNS before attaching the request to a connection with original TLS SNI.

    Each request owns a new transport/client, so a pooled connection can never
    cross two origins sharing an address. No proxy or automatic retry is used.
    """

    def __init__(self, *, allow_loopback_http: bool = False):
        self.allow_loopback_http = allow_loopback_http
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        origin = str(request.url.copy_with(path='', query=None, fragment=None))
        normalize_base_url(origin, allow_loopback_http=self.allow_loopback_http)
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == 'https' else 80)
        try:
            async with asyncio.timeout(10):
                records = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM,
                )
            addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
            local = request.url.scheme == 'http' and self.allow_loopback_http
            if not addresses or not all(
                ipaddress.ip_address(address).is_loopback if local else _public_address(address)
                for address in addresses
            ):
                raise preflight_error(
                    'The GitLab host must resolve only to permitted network addresses.',
                    'gitlab_invalid_origin',
                )
        except (OSError, TimeoutError, ValueError):
            raise ConnectorError(
                'Could not resolve a permitted GitLab host. No request was sent.',
                code='connection_unavailable', dispatched=False, retryable=True,
            ) from None
        headers = request.headers.copy()
        headers['Host'] = request.url.netloc.decode('ascii')
        pinned = httpx.Request(
            request.method, request.url.copy_with(host=addresses[0]),
            headers=headers, stream=request.stream,
            extensions={**request.extensions, 'sni_hostname': host},
        )
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()
