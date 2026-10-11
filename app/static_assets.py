"""Version the app shell's local assets from the bytes shipped in this build."""
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


ASSET_ATTRIBUTE = re.compile(r'''(?P<prefix>\s(?:src|href)\s*=\s*)(?P<quote>["'])(?P<url>/static/[^"']+)(?P=quote)''')


def versioned_index(directory: Path) -> str:
    """Render once at startup; manual query versions cannot leave stale scripts.

    These URLs bust browser caches, but are not immutable asset addresses: a
    rolling deployment still needs compatible frontend/API builds.
    """
    directory = directory.resolve()
    versions: dict[Path, str] = {}

    def version(match: re.Match[str]) -> str:
        url = urlsplit(html.unescape(match['url']))
        asset = (directory / unquote(url.path.removeprefix('/static/'))).resolve()
        if not asset.is_relative_to(directory):
            raise ValueError(f'Static asset is outside its directory: {url.path}')
        if asset not in versions:
            versions[asset] = hashlib.sha256(asset.read_bytes()).hexdigest()
        query = [(key, value) for key, value in parse_qsl(url.query, keep_blank_values=True) if key != 'v']
        query.append(('v', versions[asset]))
        value = urlunsplit(url._replace(query=urlencode(query)))
        quote = match['quote']
        return f"{match['prefix']}{quote}{html.escape(value, quote=True)}{quote}"

    return ASSET_ATTRIBUTE.sub(version, (directory / 'index.html').read_text(encoding='utf-8'))
