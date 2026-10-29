"""Separate a deployment's browser origin from its optional application path."""

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit


def base_path(public_url: str) -> str:
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in public_url):
        raise ValueError('Public URLs cannot contain whitespace or control characters.')
    path = urlsplit(public_url).path.removesuffix('/')
    # A literal, canonical path is safe in HTML, cookies, proxy configs and URLs.
    if path and not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', path):
        raise ValueError('Use a public URL path containing only letters, digits, underscores and hyphens.')
    return path


def origin(public_url: str) -> str:
    parsed = urlsplit(public_url)
    return parsed._replace(path='', query='', fragment='').geturl()


def app_path(public_url: str, path: str = '/') -> str:
    """Prefix an application-owned absolute path (never an arbitrary redirect)."""
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Expected an application path.')
    return base_path(public_url) + path


def cookie_name(name: str, public_url: str) -> str:
    """Avoid ambiguity when a browser also sends a root deployment's cookie."""
    path = base_path(public_url)
    return name + '_' + hashlib.sha256(path.encode()).hexdigest()[:16] if path else name


def broker_base_url(public_url: str, broker_url: str = '') -> str:
    """Resolve machine traffic without changing the browser origin or base path."""
    if not broker_url:
        return public_url.rstrip('/')
    try:
        parsed = urlsplit(broker_url)
        loopback = parsed.hostname == 'localhost'
        if parsed.hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not (parsed.scheme == 'https' or parsed.scheme == 'http' and loopback)):
            raise ValueError
        _ = parsed.port
        if base_path(broker_url) != base_path(public_url):
            raise ValueError
        return broker_url.rstrip('/')
    except ValueError:
        # Never include operator-provided URLs, which may contain credentials.
        raise ValueError('MOYAI_BROKER_URL must use HTTPS (or loopback HTTP), without credentials, '
                         'query or fragment, and have the same canonical base path as MOYAI_PUBLIC_URL.') from None
