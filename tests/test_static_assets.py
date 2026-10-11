import hashlib
import os
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.static_assets import versioned_index


class AssetReferences(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.urls = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs
                         if key in {'src', 'href'} and value and value.startswith('/static/'))


def test_changed_bytes_invalidate_urls_even_with_same_file_metadata(tmp_path):
    (tmp_path / 'index.html').write_text('<script src="/static/editor.js?v=manual"></script>')
    editor = tmp_path / 'editor.js'
    editor.write_bytes(b'old code')
    stat = editor.stat()
    before = AssetReferences(versioned_index(tmp_path)).urls[0]
    editor.write_bytes(b'new code')
    os.utime(editor, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = AssetReferences(versioned_index(tmp_path)).urls[0]

    assert before != after
    assert after == AssetReferences(versioned_index(tmp_path)).urls[0]
    assert parse_qs(urlsplit(after).query)['v'] == [hashlib.sha256(b'new code').hexdigest()]


def test_versions_preserve_other_query_parameters_and_fragments(tmp_path):
    (tmp_path / 'icon.svg').write_text('<svg/>')
    (tmp_path / 'index.html').write_text('''<!doctype html>
<img src='/static/icon.svg?theme=light&amp;v=old&amp;empty=#symbol'>
<link href="/static/icon.svg?v=another" rel="icon">
<script src="https://cdn.example/script.js?v=external"></script>
<a href="#content">Skip to content</a>''')

    rendered = versioned_index(tmp_path)
    icon, favicon = map(urlsplit, AssetReferences(rendered).urls)
    assert icon.fragment == 'symbol'
    assert parse_qs(icon.query, keep_blank_values=True) == {
        'theme': ['light'], 'empty': [''], 'v': parse_qs(favicon.query)['v']}
    assert 'https://cdn.example/script.js?v=external' in rendered
    assert rendered.startswith('<!doctype html>')
    assert '<a href="#content">Skip to content</a>' in rendered
    assert '&amp;' in rendered


@pytest.mark.parametrize('path', ['missing.js', '../outside.js', '%2e%2e/outside.js'])
def test_invalid_asset_references_fail_at_startup(tmp_path, path):
    (tmp_path / 'index.html').write_text(f'<script src="/static/{path}"></script>')
    with pytest.raises((FileNotFoundError, ValueError)):
        versioned_index(tmp_path)


def test_served_app_shell_is_not_cached_and_versions_match_served_assets(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        response = client.get('/')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['content-type'].startswith('text/html')
        urls = set(AssetReferences(response.text).urls)
        assert any(url.startswith('/static/skill-editor.js?v=') for url in urls)
        for url in urls:
            asset = client.get(url)
            assert asset.status_code == 200, url
            assert parse_qs(urlsplit(url).query)['v'] == [hashlib.sha256(asset.content).hexdigest()]

        # Reloads serve the current shell even if a client retained old validators.
        reload = client.get('/', headers={'If-None-Match': '"old-shell"',
                                          'If-Modified-Since': 'Thu, 31 Dec 2099 23:59:59 GMT'})
        assert reload.status_code == 200
        assert reload.text == response.text
        assert reload.headers['cache-control'] == 'no-store'
