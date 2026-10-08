import os
import stat
import warnings
import zipfile
from io import BytesIO

import pytest
from PIL import Image

from app import artifact_files
from test_workspace import workspace


def saved(workspace, entries):
    app, client = workspace
    run = app.state.store.create_run('Create a design document', '', 'demo', [], chat_enabled=True)
    path = app.state.settings.data_dir / 'artifacts' / (run['id'] + '.zip')
    path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return client, f"/api/runs/{run['id']}/files", path


def test_existing_archive_supports_exact_file_preview_and_download(workspace):
    raw = '# Selected-day accounting\n\nKeep **UTC dates**. 🗿\n'.encode()
    client, url, _ = saved(workspace, [('result.md', 'Created design.md'), ('new-files/design.md', raw),
                                     ('new-files/repo/design.md', 'A different document'), ('changes.patch', 'diff --git')])
    response = client.get(url)
    assert response.status_code == 200
    listing = response.json()
    file, nested, summary, patch = sorted(listing['files'], key=lambda f: ['new-files/design.md', 'new-files/repo/design.md', 'result.md', 'changes.patch'].index(f['archive_path']))
    assert file['workspace_path'] == 'design.md' and nested['workspace_path'] == 'repo/design.md'
    assert summary['workspace_path'] is None and patch['workspace_path'] is None
    assert client.get(file['preview_url']).json() == {'text': raw.decode(), 'truncated': False, 'format': 'markdown'}
    download = client.get(file['url'])
    assert download.content == raw
    assert download.headers['content-disposition'] == "attachment; filename*=UTF-8''design.md"
    assert download.headers['content-type'] == 'application/octet-stream'
    assert download.headers['x-content-type-options'] == 'nosniff'
    assert download.headers['cache-control'] == 'no-store'
    assert client.get(nested['url']).content == b'A different document'
    assert client.get(url.removesuffix('/files') + '/artifact').status_code == 200
    client.cookies.clear()
    for endpoint in [url, file['url'], file['preview_url']]:
        assert client.get(endpoint).status_code == 401


def test_catalog_cap_preserves_recovery_artifacts_before_source(workspace):
    primary = {'result.md': b'Answer', 'browser.png': b'screenshot', 'changes.patch': b'root patch',
               'repositories/repo/changes.patch': b'nested patch', 'recovery-manifest.json': b'{}'}
    sources = [(f'new-files/repo/source-{i:04}.js', 'source') for i in range(artifact_files.MAX_LIST)]
    client, url, _ = saved(workspace, sources + list(primary.items()))
    listing = client.get(url).json()
    files = {file['archive_path']: file for file in listing['files']}
    assert listing['limited'] and len(files) == artifact_files.MAX_LIST
    assert primary.keys() <= files.keys()
    for name, content in primary.items():
        assert files[name]['workspace_path'] is None
        assert client.get(files[name]['url']).content == content


def test_paths_symlinks_duplicates_and_oversized_entries_are_not_served(workspace):
    link = zipfile.ZipInfo('new-files/symlink.md')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    bad = ['../escape.md', '/etc/passwd', 'new-files/../escape.md', 'new-files/a\\b',
           'new-files/a\nb', 'new-files//empty', 'new-files/./dot.md', 'new-files/c:drive']
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        client, url, path = saved(workspace, [(name, 'bad') for name in bad] + [
            (link, '/etc/passwd'), ('new-files/duplicate.md', 'one'), ('new-files/duplicate.md', 'two'),
            ('new-files/big.md', b'x' * (artifact_files.MAX_FILE + 1)), ('new-files/ok.md', '# Safe')])
    listing = client.get(url).json()
    assert [f['path'] for f in listing['files']] == ['ok.md']
    assert listing['limited']
    for name in [*bad, link.filename, 'new-files/duplicate.md', 'new-files/big.md', str(path), '../../workspace.db']:
        assert client.get(url + '/content', params={'path': name}).status_code == 404
    assert not (path.parent.parent / 'escape.md').exists()


def test_untrusted_content_is_returned_as_json_or_forced_download_never_html(workspace):
    client, url, _ = saved(workspace, [('new-files/page.html', '<script>alert(1)</script>'),
                                     ('new-files/image.svg', '<svg onload="alert(1)"/>'),
                                     ('new-files/data.bin', b'\x00\xffabc'),
                                     ('new-files/empty.txt', b''), ('new-files/désign "x".md', '# Hello')])
    files = {f['path']: f for f in client.get(url).json()['files']}
    for name in ['page.html', 'image.svg']:
        file = files[name]
        preview = client.get(file['preview_url'])
        assert preview.headers['content-type'] == 'application/json'
        assert preview.json()['format'] == 'text'
        assert client.get(file['url']).headers['content-type'] == 'application/octet-stream'
    assert client.get(files['data.bin']['preview_url']).json()['text'] is None
    assert client.get(files['empty.txt']['preview_url']).json()['text'] == ''
    assert client.get(files['désign "x".md']['url']).headers['content-disposition'] == "attachment; filename*=UTF-8''d%C3%A9sign%20%22x%22.md"


def test_large_preview_is_bounded_and_download_keeps_entire_file(workspace):
    raw = b'a' * (artifact_files.MAX_PREVIEW - 1) + '🗿more'.encode()
    client, url, _ = saved(workspace, [('new-files/long.md', raw)])
    file = client.get(url).json()['files'][0]
    preview = client.get(file['preview_url']).json()
    assert preview['truncated'] and len(preview['text']) == artifact_files.MAX_PREVIEW - 1
    assert client.get(file['url']).content == raw


def test_a_new_checkpoint_cannot_silently_change_an_open_download(workspace):
    client, url, path = saved(workspace, [('new-files/design.md', 'old')])
    old = client.get(url).json()['files'][0]
    replacement = path.with_suffix('.next.zip')
    with zipfile.ZipFile(replacement, 'w') as z:
        z.writestr('new-files/design.md', 'newest')
    os.replace(replacement, path)
    assert client.get(old['url']).status_code == 409
    assert client.get(old['preview_url']).status_code == 409
    new = client.get(url).json()['files'][0]
    assert client.get(new['url']).content == b'newest'


@pytest.mark.parametrize('format,extension', [('PNG', 'png'), ('JPEG', 'jpg'), ('WEBP', 'webp'), ('GIF', 'gif')])
def test_saved_raster_preview_is_decoded_authenticated_and_revision_pinned(workspace, format, extension):
    raw = BytesIO()
    Image.new('RGB', (24, 16), 'purple').save(raw, format=format)
    client, url, path = saved(workspace, [(f'new-files/screenshot.{extension}', raw.getvalue())])
    file = client.get(url).json()['files'][0]
    assert file['kind'] == 'image'
    response = client.get(file['inline_url'])
    assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
    assert response.headers['content-disposition'] == 'inline'
    assert response.headers['cache-control'] == 'no-store' and response.headers['x-content-type-options'] == 'nosniff'
    with Image.open(BytesIO(response.content)) as decoded:
        assert decoded.size == (24, 16) and decoded.format == 'JPEG'
    assert client.get(file['url']).content == raw.getvalue(), 'Download must preserve original bytes'
    replacement = path.with_suffix('.next.zip')
    with zipfile.ZipFile(replacement, 'w') as archive:
        archive.writestr(f'new-files/screenshot.{extension}', raw.getvalue())
        archive.writestr('new-files/other.txt', 'changed')
    os.replace(replacement, path)
    assert client.get(file['inline_url']).status_code == 409
    current = next(f for f in client.get(url).json()['files'] if f.get('kind') == 'image')
    client.cookies.clear()
    assert client.get(current['inline_url']).status_code == 401


def test_inline_preview_rejects_active_content_and_mislabeled_images(workspace):
    client, url, _ = saved(workspace, [('new-files/page.html', '<script>alert(1)</script>'),
                                     ('new-files/vector.svg', '<svg onload="alert(1)"/>'),
                                     ('new-files/fake.png', '<script>alert(1)</script>'),
                                     ('new-files/broken.png', b'\x89PNG\r\n\x1a\ninvalid')])
    for file in client.get(url).json()['files']:
        if file['name'].endswith(('.html', '.svg')):
            assert 'inline_url' not in file
        assert client.get(file['url'] + '&inline=true').status_code == 415
        assert client.get(file['url']).headers['content-type'] == 'application/octet-stream'


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'archive_size', 'expanded_size', 'entry_count'])
def test_missing_corrupt_and_bomb_archives_fail_cleanly(workspace, monkeypatch, kind):
    client, url, path = saved(workspace, [('new-files/test.md', 'some text')])
    if kind == 'missing':
        path.unlink()
    elif kind == 'corrupt':
        path.write_bytes(b'not a ZIP')
    else:
        monkeypatch.setattr(artifact_files, {'archive_size': 'MAX_ARCHIVE', 'expanded_size': 'MAX_TOTAL', 'entry_count': 'MAX_ENTRIES'}[kind], 0)
    expected = {'missing': 404, 'corrupt': 503}.get(kind, 413)
    assert client.get(url).status_code == expected
    assert client.get(url + '/content', params={'path': 'new-files/test.md'}).status_code == expected
    assert client.get('/api/runs/' + '0' * 32 + '/files').status_code == 404
    assert client.get('/api/runs/not-a-run/files').status_code == 404
