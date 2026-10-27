"""Separate a deployment's browser origin from its optional application path."""

import hashlib
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
