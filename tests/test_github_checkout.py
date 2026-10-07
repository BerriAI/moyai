import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from sandbox import github_tools as local


def git(directory, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=directory, stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch, request):
    repository = getattr(request, 'param', '')
    git_path = '/github/repositories/101.git'
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    (source / 'edit.py').write_text('before = 1\n')
    (source / 'delete.py').write_text('old\n')
    (source / '.gitignore').write_text('ignored.txt\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Initial')
    base = git(source, 'rev-parse', 'HEAD')
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    destination = upstream / git_path.lstrip('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(upstream, 'clone', '--bare', str(source), str(destination))
    root = tmp_path / 'workspace'
    root.mkdir()
    monkeypatch.setattr(local, 'ROOT', root)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_git(self):
            requests.append((self.path, self.headers.get('Authorization')))
            assert self.headers['Authorization'] == 'Bearer current-capability'
            path, _, query = self.path.partition('?')
            env = {**os.environ, 'GIT_PROJECT_ROOT': str(upstream), 'GIT_HTTP_EXPORT_ALL': '1',
                   'PATH_INFO': path, 'QUERY_STRING': query, 'REQUEST_METHOD': self.command,
                   'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                   'HTTP_GIT_PROTOCOL': self.headers.get('Git-Protocol', ''), 'REMOTE_USER': 'moyai',
                   'CONTENT_LENGTH': self.headers.get('Content-Length', '0')}
            raw = self.rfile.read(int(env['CONTENT_LENGTH']))
            response = subprocess.run(['git', 'http-backend'], env=env, input=raw, capture_output=True, check=True).stdout
            head, _, body = response.partition(b'\r\n\r\n')
            self.send_response(200)
            for line in head.split(b'\r\n'):
                name, _, value = line.decode().partition(':')
                if name.lower() != 'status':
                    self.send_header(name, value.strip())
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = handle_git

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def broker(path, body):
        assert path == '/tools/call' and body == {'name': 'github_checkout', 'arguments': {'repository': repository} if repository else {}}
        return {'repository_id': 101, 'repository': repository or 'BerriAI/litellm', 'default_branch': 'main', 'base_sha': base, 'git_path': git_path}
    try:
        result = local.checkout(broker, f'http://127.0.0.1:{server.server_port}', 'current-capability', repository=repository)
        yield root / 'repo', result, requests, broker, server
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_real_git_checkout_preserves_files_and_never_stores_capability(checkout):
    repo, result, requests, broker, server = checkout
    assert (repo / 'edit.py').read_text() == 'before = 1\n'
    assert result['repository'] == 'BerriAI/litellm'
    assert all(auth == 'Bearer current-capability' for _, auth in requests)
    assert {path.split('?')[0] for path, _ in requests} == {'/github/repositories/101.git/info/refs', '/github/repositories/101.git/git-upload-pack'}
    config = (repo / '.git/config').read_text()
    assert 'current-capability' not in config and 'extraHeader' not in config
    assert 'https://github.com/BerriAI/litellm.git' in config
    (repo / 'edit.py').write_text('keep this edit\n')
    again = local.checkout(broker, f'http://127.0.0.1:{server.server_port}', 'rotated-capability')
    assert again['reused'] and (repo / 'edit.py').read_text() == 'keep this edit\n'


def test_packages_committed_uncommitted_new_deleted_executable_and_ignores_ignored(checkout):
    repo, _, _, _, _ = checkout
    (repo / 'edit.py').write_text('committed = 2\n')
    git(repo, 'add', 'edit.py'); git(repo, 'commit', '-m', 'Local commit')
    (repo / 'delete.py').unlink()
    (repo / 'new.py').write_text('print("new")\n')
    (repo / 'new.py').chmod(0o755)
    (repo / 'ignored.txt').write_text('excluded')
    payload = local.collect(str(repo), 'Test PR', 'Body', 'request-123')
    files = {f['path']: f for f in payload['files']}
    assert set(files) == {'edit.py', 'delete.py', 'new.py'}
    assert files['edit.py']['content'] == 'committed = 2\n'
    assert files['delete.py']['content'] is None
    assert files['new.py']['executable'] is True
    assert payload['base_sha'] == json.loads((repo / '.git/moyai.json').read_text())['base_sha']


@pytest.mark.parametrize('kind', ['symlink', 'binary', 'credential', 'workflow', 'large'])
def test_unsafe_working_tree_changes_never_leave_sandbox(checkout, kind):
    repo, _, _, _, _ = checkout
    if kind == 'symlink':
        (repo / 'link').symlink_to(repo.parent / 'outside')
    elif kind == 'binary':
        (repo / 'blob').write_bytes(b'x\0y')
    elif kind == 'credential':
        (repo / '.env.secret').write_text('test secret')
    elif kind == 'workflow':
        (repo / '.github/workflows').mkdir(parents=True)
        (repo / '.github/workflows/test.yml').write_text('test')
    else:
        (repo / 'large').write_bytes(b'x' * (local.MAX_FILE + 1))
    with pytest.raises(local.GitHubToolError):
        local.collect(str(repo), 'Test PR', 'Body', 'request-123')


def test_escape_and_existing_nonrepo_refused(checkout):
    repo, _, _, broker, server = checkout
    with pytest.raises(local.GitHubToolError, match='inside'):
        local.directory_path('/etc')
    occupied = repo.parent / 'occupied'; occupied.mkdir()
    (occupied / 'keep').write_text('preserve')
    with pytest.raises(local.GitHubToolError, match='already exists'):
        local.checkout(broker, f'http://127.0.0.1:{server.server_port}', 'current-capability', str(occupied))
    assert (occupied / 'keep').read_text() == 'preserve'


@pytest.mark.parametrize('checkout', ['BerriAI/litellm', 'BerriAI/moyai'], indirect=True)
def test_specific_repository_real_checkout_and_publication_payload(checkout):
    repo, result, requests, broker, server = checkout
    target = result['repository']
    assert {path.split('?')[0] for path, _ in requests} == {
        '/github/repositories/101.git/info/refs', '/github/repositories/101.git/git-upload-pack'}
    assert 'current-capability' not in (repo / '.git/config').read_text()
    (repo / 'new.py').write_text('answer = 42\n')
    assert local.collect(str(repo), 'Change this repository', 'Verified checkout', 'repo-test-123')['repository_id'] == 101
    def different(path, body):
        return {'repository_id': 999, 'git_path': '/github/repositories/999.git', 'repository': 'BerriAI/other', 'default_branch': 'main', 'base_sha': result['base_sha']}
    with pytest.raises(local.GitHubToolError, match='another repository'):
        local.checkout(different, f'http://127.0.0.1:{server.server_port}', 'current-capability', str(repo))
    assert (repo / 'new.py').read_text() == 'answer = 42\n'


@pytest.mark.parametrize('checkout', ['BerriAI/litellm'], indirect=True)
def test_publish_then_revise_real_checkout_without_republishing_unchanged_files(checkout):
    repo, initial, _, _, server = checkout
    source = repo.parent.parent / 'source'
    upstream = repo.parent.parent / 'upstream/github/repositories/101.git'
    calls = []

    def broker(path, body):
        assert path == '/tools/call'
        args = body['arguments']
        if body['name'] == 'github_checkout':
            return {**initial, 'base_sha': git(source, 'rev-parse', 'HEAD'), 'number': 100,
                    'checkout_ref': 'refs/pull/100/head', 'git_path': '/github/repositories/101.git'}
        calls.append(body)
        for change in args['files']:
            target = source / change['path']
            if change['content'] is None:
                target.unlink()
            else:
                target.write_text(change['content'])
        git(source, 'add', '.')
        git(source, 'commit', '-m', args['title'])
        commit = git(source, 'rev-parse', 'HEAD')
        git(source, 'push', str(upstream), 'HEAD:refs/heads/moyai/test', 'HEAD:refs/pull/100/head')
        return {'repository_id': 101, 'repository': 'BerriAI/litellm', 'number': 100, 'commit': commit, 'branch': 'moyai/test',
                'url': 'https://github.com/BerriAI/litellm/pull/100'}

    remote = f'http://127.0.0.1:{server.server_port}'
    (repo / 'schema.d.ts').write_text('x' * 2_931_023)
    (repo / 'new.py').write_text('keep = True\n')
    result = local.call('github_create_pull_request', {'directory': str(repo), 'title': 'Large schema',
        'body': 'First publication', 'request_key': 'first-publication'}, broker, remote, 'current-capability')
    assert 'checkout_warning' not in result
    assert local.metadata(repo)['base_sha'] == result['commit']
    with pytest.raises(local.GitHubToolError, match='between 1 and 100'):
        local.collect(str(repo), 'No edits', 'Body', 'no-new-edits')
    (repo / 'edit.py').write_text('after = 2\n')
    result = local.call('github_update_pull_request', {'directory': str(repo), 'number': 100,
        'title': 'Follow-up fix', 'request_key': 'followup-fix'}, broker, remote, 'current-capability')
    assert 'checkout_warning' not in result
    assert [f['path'] for f in calls[-1]['arguments']['files']] == ['edit.py']
    assert 'body' not in calls[-1]['arguments']
    assert (repo / 'new.py').read_text() == 'keep = True\n'
    # A new checkout can resume the PR after sandbox loss, without touching old files.
    fresh = repo.parent / 'fresh'
    receipt = local.checkout(broker, remote, 'current-capability', str(fresh), 'BerriAI/litellm', number=100)
    assert receipt['base_sha'] == result['commit'] and receipt['number'] == 100
    assert (fresh / 'edit.py').read_text() == 'after = 2\n'
    assert (fresh / 'schema.d.ts').stat().st_size == 2_931_023
    (repo / 'new.py').unlink()
    payload = local.collect(str(repo), 'Remove new file', 'Body', 'remove-new-file')
    assert payload['files'] == [{'path': 'new.py', 'content': None, 'executable': False}]
    assert 'current-capability' not in (fresh / '.git/config').read_text()


def test_local_total_budget_and_byte_limits(checkout, monkeypatch):
    repo, _, _, _, _ = checkout
    monkeypatch.setattr(local, 'MAX_TOTAL', 10)
    (repo / 'one').write_text('123456')
    (repo / 'two').write_text('123456')
    with pytest.raises(local.GitHubToolError, match='20 MiB'):
        local.collect(str(repo), 'Total budget', 'Body', 'total-budget')


def test_advertised_update_packages_local_files():
    from app.github import TOOLS
    tools = [{'name': name, 'inputSchema': spec[2].model_json_schema()} for name, spec in TOOLS.items()]
    updated = {tool['name']: tool for tool in local.advertised_tools(tools)}
    props = updated['github_update_pull_request']['inputSchema']['properties']
    assert set(props) == {'directory', 'number', 'title', 'request_key'}
    assert 'number' in updated['github_checkout']['inputSchema']['properties']


def test_successful_publication_is_preserved_when_base_sync_fails(checkout, monkeypatch):
    repo, _, _, _, _ = checkout
    (repo / 'new.py').write_text('new = True\n')
    receipt = {'repository': 'BerriAI/litellm', 'number': 100, 'commit': 'c' * 40,
               'url': 'https://github.com/BerriAI/litellm/pull/100'}
    def sync(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(local, 'sync_publication', sync)
    result = local.call('github_create_pull_request', {'directory': str(repo), 'title': 'Publish change',
        'body': 'A change', 'request_key': 'sync-failure'}, lambda *args: receipt, '', '')
    assert result['url'] == receipt['url'] and 'succeeded' in result['checkout_warning']
