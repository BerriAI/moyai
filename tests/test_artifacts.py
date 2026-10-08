import json
from pathlib import Path
import subprocess
import zipfile

from sandbox.artifacts import collect_archive
from app.runner import safe_error_detail


def command(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout


def test_nested_repository_edits_survive_large_virtualenv_and_exclude_symlinks(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    artifacts = tmp_path / 'artifacts'; artifacts.mkdir()
    (artifacts / 'result.md').write_text('The actual answer')
    env = workspace / 'venv'; env.mkdir()
    for i in range(1100): (env / str(i)).write_text('irrelevant installed dependency')
    repo = workspace / 'litellm'; repo.mkdir()
    command(repo, 'init')
    (repo / 'tracked.py').write_text('old value\n')
    command(repo, 'add', '.')
    command(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'base')
    (repo / 'tracked.py').write_text('new value\n')
    (repo / 'new.py').write_text('new source with run-secret')
    secret = tmp_path / 'secret'; secret.write_text('must not leak')
    (repo / 'external.txt').symlink_to(secret)
    (repo / '.env').write_text('must not leak')
    collect_archive(workspace, artifacts, b'run-secret')
    with zipfile.ZipFile(artifacts / 'result.zip') as z:
        assert z.read('result.md') == b'The actual answer'
        patch = z.read('repositories/litellm/changes.patch')
        assert b'-old value' in patch and b'+new value' in patch
        assert z.read('new-files/litellm/new.py') == b'new source with [redacted]'
        assert not any('venv/' in n or n.endswith(('.env', 'external.txt')) for n in z.namelist())
        manifest = json.loads(z.read('recovery-manifest.json'))
        assert manifest['repositories'][0]['path'] == 'litellm' and not manifest['omitted']
    # The exported patch actually applies to the base revision.
    command(repo, 'checkout', '--', 'tracked.py')
    subprocess.run(['git', '-C', str(repo), 'apply', '-'], input=patch, check=True)
    assert (repo / 'tracked.py').read_text() == 'new value\n'


def test_cloud_error_diagnostics_redact_credentials_and_request_urls():
    detail = safe_error_detail(RuntimeError('Snapshot failed; token=hidden Bearer other https://example.com/?key=secret sk-secret custom-private'), ['custom-private'])
    assert 'Snapshot failed' in detail
    assert all(secret not in detail for secret in ('hidden', 'other', 'example.com', 'sk-secret', 'custom-private'))
