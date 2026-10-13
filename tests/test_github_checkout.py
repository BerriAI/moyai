import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agent.tools import github_tools as local


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


@pytest.mark.parametrize('kind', ['symlink', 'credential'])
@pytest.mark.parametrize('allow_empty', [False, True])
def test_unsafe_working_tree_changes_never_leave_sandbox(checkout, kind, allow_empty):
    repo, _, _, _, _ = checkout
    if kind == 'symlink':
        (repo / 'link').symlink_to(repo.parent / 'outside')
    elif kind == 'credential':
        (repo / '.env.secret').write_text('test secret')
    with pytest.raises(local.GitHubToolError):
        local.collect(str(repo), 'Test PR', 'Body', 'request-123', allow_empty=allow_empty)


def test_collects_complete_workflows_and_composite_actions(checkout):
    repo, _, _, _, _ = checkout
    contents = {'.github/workflows/test.yml': 'name: CI\non: push\n',
                '.github/actions/test/action.yml': 'name: Test\nruns:\n  using: composite\n',
                '/'.join(['nested']*30)+'/test.py': 'print(42)\n'}
    for name, content in contents.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    payload = local.collect(str(repo), 'Test PR', 'Body', 'request-123')
    assert {f['path']: f['content'] for f in payload['files']} == contents


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


@pytest.mark.parametrize('checkout', ['BerriAI/agent-shin'], indirect=True)
@pytest.mark.parametrize('source_kind', ['branch', 'commit'])
def test_cross_repository_source_ref_checkout_preserves_destination(checkout, source_kind):
    repo, initial, requests, _, server = checkout
    root = repo.parent.parent
    source = root / 'moyai-source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    (source / 'destination.txt').write_text('Moyai main\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Moyai initial')
    branch = 'release/github-parity'
    git(source, 'checkout', '-b', branch)
    (source / 'destination.txt').write_text('Moyai selected source\n')
    git(source, 'commit', '-am', 'Selected source')
    selected_sha = git(source, 'rev-parse', 'HEAD')
    if source_kind == 'commit':
        (source / 'destination.txt').write_text('Newer branch head\n')
        git(source, 'commit', '-am', 'Advance branch')
    bare = root / 'upstream/github/repositories/202.git'
    git(root, 'clone', '--bare', str(source), str(bare))
    selected_ref = branch if source_kind == 'branch' else selected_sha
    checkout_ref = 'refs/heads/' + branch if source_kind == 'branch' else selected_sha
    calls = []

    def broker(path, body):
        calls.append(body)
        assert path == '/tools/call' and body['name'] == 'github_checkout'
        if body['arguments']['repository_id'] != 202:
            return {'error': 'This repository is not selected.'}
        assert body['arguments'] == {'repository_id': 202, 'ref': selected_ref}
        return {'repository_id': 202, 'repository': 'BerriAI/moyai', 'default_branch': 'main',
                'base_branch': branch, 'base_sha': selected_sha, 'checkout_ref': checkout_ref,
                'git_path': '/github/repositories/202.git'}

    remote = f'http://127.0.0.1:{server.server_port}'
    (repo / 'edit.py').write_text('Preserve agent-shin changes\n')
    with pytest.raises(local.GitHubToolError, match='another repository'):
        local.checkout(broker, remote, 'current-capability', str(repo), repository_id=202, ref=selected_ref)
    fresh = repo.parent / 'moyai'
    checked_out = local.call('github_checkout', {'directory': str(fresh), 'repository_id': 202, 'ref': selected_ref},
                             broker, remote, 'current-capability')
    assert checked_out['repository_id'] == 202 and checked_out['base_sha'] == selected_sha
    assert checked_out['base_branch'] == branch and checked_out['checkout_ref'] == checkout_ref
    assert (fresh / 'destination.txt').read_text() == 'Moyai selected source\n'
    assert git(fresh, 'rev-parse', 'HEAD') == selected_sha
    assert git(fresh, 'remote', 'get-url', 'origin') == 'https://github.com/BerriAI/moyai.git'
    (fresh / 'destination.txt').write_text('Moyai publication\n')
    packaged = local.collect(str(fresh), 'Publish Moyai', 'Selected source', 'moyai-source-publication')
    assert packaged['repository_id'] == 202 and packaged['base_sha'] == selected_sha
    assert packaged['files'] == [{'path': 'destination.txt', 'content': 'Moyai publication\n', 'executable': False}]
    assert local.metadata(repo)['repository_id'] == initial['repository_id'] == 101
    assert (repo / 'edit.py').read_text() == 'Preserve agent-shin changes\n'
    repeated = local.checkout(broker, remote, 'current-capability', str(fresh), repository_id=202, ref=selected_ref)
    assert repeated['reused'] and (fresh / 'destination.txt').read_text() == 'Moyai publication\n'
    saved = (fresh / '.git/moyai.json').read_bytes()

    def changed_source(path, body):
        return {**broker(path, body), 'base_sha': 'f' * 40}

    with pytest.raises(local.GitHubToolError, match='fresh directory'):
        local.checkout(changed_source, remote, 'current-capability', str(fresh), repository_id=202, ref=selected_ref)
    assert (fresh / '.git/moyai.json').read_bytes() == saved
    assert (fresh / 'destination.txt').read_text() == 'Moyai publication\n'
    before = len(requests)
    rejected = local.checkout(broker, remote, 'current-capability', str(repo.parent / 'unselected'),
                              repository_id=303, ref=selected_ref)
    assert rejected.get('error') and len(requests) == before
    assert not (repo.parent / 'unselected').exists()
    assert 'current-capability' not in (fresh / '.git/config').read_text()
    destination_branch = 'feature/cross-repository'

    def publish(path, body):
        assert path == '/tools/call' and body['name'] == 'github_publish_branch'
        assert body['arguments'] == {key: value for key, value in packaged.items() if key != 'body'} | {
            'branch': destination_branch}
        git(source, 'checkout', '--detach', selected_sha)
        (source / 'destination.txt').write_text(body['arguments']['files'][0]['content'])
        git(source, 'commit', '-am', body['arguments']['title'])
        git(source, 'push', str(bare), 'HEAD:refs/heads/' + destination_branch)
        return {'kind': 'branch', 'repository_id': 202, 'repository': 'BerriAI/moyai',
                'branch': destination_branch, 'commit': git(source, 'rev-parse', 'HEAD'), 'base_branch': branch}

    result = local.call('github_publish_branch', {'directory': str(fresh), 'branch': destination_branch,
        'title': 'Publish Moyai', 'request_key': 'moyai-source-publication'}, publish, remote, 'current-capability')
    assert 'checkout_warning' not in result and result['repository_id'] == 202
    assert local.metadata(fresh)['base_sha'] == result['commit']
    assert local.metadata(fresh)['repository_id'] == 202 and local.metadata(fresh)['base_branch'] == branch
    assert all(path.startswith('/github/repositories/202.git/') for path, _ in requests[before:])
    assert git(bare, 'rev-parse', 'refs/heads/' + destination_branch) == result['commit']
    assert local.metadata(repo)['repository_id'] == 101
    assert git(root / 'upstream/github/repositories/101.git', 'rev-parse', 'refs/heads/main') == initial['base_sha']
    assert (repo / 'edit.py').read_text() == 'Preserve agent-shin changes\n'


def test_checkout_ref_and_pr_are_mutually_exclusive_before_broker(checkout):
    repo, _, _, _, server = checkout
    with pytest.raises(local.GitHubToolError, match='not both'):
        local.call('github_checkout', {'directory': str(repo.parent / 'fresh'), 'number': 100, 'ref': 'main'},
                   lambda *args: pytest.fail('Ambiguous checkout reached the broker'),
                   f'http://127.0.0.1:{server.server_port}', 'current-capability')


@pytest.mark.parametrize('ref', ['', 100, True, 'x' * 1025, '../main', 'main~1', '-main', 'HEAD', 'main:other'])
def test_checkout_invalid_source_ref_never_reaches_broker(ref):
    with pytest.raises(local.GitHubToolError, match='source ref'):
        local.checkout(lambda *args: pytest.fail('Invalid source ref reached the broker'), '', '', ref=ref)


def test_checkout_ref_rejects_changed_revision_without_recording_metadata(checkout):
    repo, initial, _, _, server = checkout
    fresh = repo.parent / 'changed-source'
    broker = lambda *args: {**initial, 'base_sha': 'a' * 40, 'checkout_ref': 'refs/heads/main',
                           'base_branch': 'main', 'git_path': '/github/repositories/101.git'}
    with pytest.raises(local.GitHubToolError, match='changed during checkout'):
        local.checkout(broker, f'http://127.0.0.1:{server.server_port}', 'current-capability',
                       str(fresh), repository_id=101, ref='main')
    assert not (fresh / '.git/moyai.json').exists()


@pytest.mark.parametrize('options', [{}, {'base_branch': 'release/test'}, {'head_branch': 'feature/test'},
                                    {'base_branch': 'release/test', 'head_branch': 'feature/test'}])
def test_pr_packaging_forwards_only_explicit_branch_options(checkout, options):
    repo, _, _, _, _ = checkout
    (repo / 'edit.py').write_text('changed = True\n')
    calls = []

    def broker(path, body):
        calls.append(body)
        return {'status': 'unconfirmed'}

    arguments = {'directory': str(repo), 'title': 'Selected branches', 'body': 'Publication',
                 'request_key': 'selected-branches', **options}
    result = local.call('github_create_pull_request', arguments, broker, '', '')
    assert result == {'status': 'unconfirmed'} and len(calls) == 1
    expected = local.collect(str(repo), arguments['title'], arguments['body'], arguments['request_key'])
    assert calls[0] == {'name': 'github_create_pull_request', 'arguments': {**expected, **options}}


@pytest.mark.parametrize('name,field', [('github_create_pull_request', 'base_branch'),
                                      ('github_create_pull_request', 'head_branch'),
                                      ('github_publish_branch', 'branch')])
def test_publication_uses_shared_branch_validation_before_packaging(name, field, monkeypatch):
    monkeypatch.setattr(local, 'collect', lambda **kwargs: pytest.fail('Invalid branch reached file packaging'))
    with pytest.raises(local.GitHubToolError, match='valid Git branch'):
        local.call(name, {field: 'main:refs/heads/other'},
                   lambda *args: pytest.fail('Invalid branch reached the broker'), '', '')


@pytest.mark.parametrize('operation', ['github_publish_branch', 'github_rebase_pull_request'])
def test_branch_publication_requires_confirmed_checkout_repository_id(checkout, operation):
    repo, _, _, _, _ = checkout
    data = local.metadata(repo)
    data.pop('repository_id')
    (repo / '.git/moyai.json').write_text(json.dumps(data))
    (repo / 'edit.py').write_text('changed = True\n')
    with pytest.raises(local.GitHubToolError, match='confirm the repository ID'):
        local.call(operation, {'directory': str(repo), 'branch': 'feature/test', 'number': 100,
            'expected_head_sha': 'b' * 40, 'title': 'Publish branch', 'request_key': 'legacy-branch'},
            lambda *args: pytest.fail('Legacy repository name was published'), '', '')


def test_branch_publication_syncs_real_ref_and_clears_pr_metadata(checkout):
    import base64
    repo, _, _, _, server = checkout
    source = repo.parent.parent / 'source'
    bare = repo.parent.parent / 'upstream/github/repositories/101.git'
    metadata_path = repo / '.git/moyai.json'
    metadata_path.write_text(json.dumps({**local.metadata(repo), 'number': 100, 'checkout_ref': 'refs/pull/100/head'}))
    branch = 'feature/branch-only'
    calls = []

    def broker(path, body):
        assert path == '/tools/call' and body['name'] == 'github_publish_branch'
        arguments = body['arguments']
        calls.append(arguments)
        assert arguments['repository_id'] == 101
        assert arguments['base_sha'] == git(source, 'rev-parse', 'HEAD')
        assert set(arguments) == {'repository_id', 'base_sha', 'files', 'title', 'branch', 'request_key'} | (
            {'expected_head_sha'} if len(calls) > 1 else set())
        for change in arguments['files']:
            path = source / change['path']
            if change['content'] is None:
                path.unlink()
            elif change.get('encoding') == 'base64':
                path.write_bytes(base64.b64decode(change['content']))
            else:
                path.write_text(change['content'])
        git(source, 'add', '.')
        git(source, 'commit', '-m', arguments['title'])
        commit = git(source, 'rev-parse', 'HEAD')
        git(source, 'push', str(bare), 'HEAD:refs/heads/' + branch)
        return {'kind': 'branch', 'repository_id': 101, 'repository': 'BerriAI/litellm',
                'branch': branch, 'commit': commit, 'base_branch': 'main'}

    (repo / 'new.bin').write_bytes(b'\x00\xffbranch publication')
    remote = f'http://127.0.0.1:{server.server_port}'
    arguments = {'directory': str(repo), 'branch': branch, 'title': 'Publish branch', 'request_key': 'branch-first'}
    result = local.call('github_publish_branch', arguments, broker, remote, 'current-capability')
    assert result['kind'] == 'branch' and 'number' not in result and 'checkout_warning' not in result
    saved = local.metadata(repo)
    assert saved['base_sha'] == result['commit'] and saved['ref'] == branch and saved['branch'] == branch
    assert saved['checkout_ref'] == 'refs/heads/' + branch and saved['base_branch'] == 'main'
    assert 'number' not in saved
    with pytest.raises(local.GitHubToolError, match='no changed files'):
        local.collect(str(repo), 'No new changes', '', 'unchanged-branch')
    (repo / 'edit.py').write_text('followup = True\n')
    revised = local.call('github_publish_branch', {**arguments, 'title': 'Revise branch',
        'request_key': 'branch-second', 'expected_head_sha': result['commit']}, broker, remote, 'current-capability')
    assert 'checkout_warning' not in revised
    assert local.metadata(repo)['base_sha'] == revised['commit'] and 'number' not in local.metadata(repo)
    assert calls[-1]['expected_head_sha'] == result['commit']
    assert [change['path'] for change in calls[-1]['files']] == ['edit.py']
    assert git(bare, 'rev-parse', 'refs/heads/' + branch) == revised['commit']


@pytest.mark.parametrize('failure', ['moved', 'invalid_branch', 'wrong_repository'])
def test_branch_sync_rejects_wrong_ref_or_repository_without_changing_metadata(checkout, failure):
    repo, initial, requests, _, server = checkout
    before = (repo / '.git/moyai.json').read_bytes()
    receipt = {'kind': 'branch', 'repository_id': 101, 'repository': initial['repository'],
               'commit': 'a' * 40, 'branch': 'main', 'base_branch': 'main'}
    if failure == 'invalid_branch':
        receipt['branch'] = 'main:refs/heads/unexpected'
    elif failure == 'wrong_repository':
        receipt['repository_id'] = 202
    count = len(requests)
    with pytest.raises(local.GitHubToolError):
        local.sync_publication(repo, receipt, f'http://127.0.0.1:{server.server_port}', 'current-capability')
    assert (repo / '.git/moyai.json').read_bytes() == before
    if failure != 'moved':
        assert len(requests) == count


@pytest.mark.parametrize('source_kind', ['branch', 'commit'])
@pytest.mark.parametrize('empty', [False, True])
def test_rebase_packages_fresh_target_base_and_syncs_same_pr(checkout, source_kind, empty):
    repo, initial, _, _, server = checkout
    source = repo.parent.parent / 'source'
    bare = repo.parent.parent / 'upstream/github/repositories/101.git'
    branch = 'moyai/rebase-test'
    git(source, 'checkout', '-b', branch)
    (source / 'edit.py').write_text('feature = "old"\n')
    git(source, 'commit', '-am', 'Old feature head')
    old_head = git(source, 'rev-parse', 'HEAD')
    git(source, 'push', str(bare), 'HEAD:refs/heads/' + branch)
    git(source, 'checkout', 'main')
    (source / 'base-only.txt').write_text('New target base content\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Advance target base')
    target_base = git(source, 'rev-parse', 'HEAD')
    git(source, 'push', str(bare), 'HEAD:refs/heads/main')
    selected_ref = 'main' if source_kind == 'branch' else target_base
    calls = []

    def broker(path, body):
        assert path == '/tools/call'
        if body['name'] == 'github_checkout':
            assert body['arguments'] == {'repository_id': 101, 'ref': selected_ref}
            return {**initial, 'base_sha': target_base, 'base_branch': 'main',
                    'checkout_ref': 'refs/heads/main' if source_kind == 'branch' else target_base,
                    'git_path': '/github/repositories/101.git'}
        assert body['name'] == 'github_rebase_pull_request'
        arguments = body['arguments']
        calls.append(arguments)
        assert arguments == {'repository_id': 101, 'base_sha': target_base, 'number': 100,
            'expected_head_sha': old_head, 'title': 'Rebase feature', 'request_key': 'rebase-same-pr',
            'files': [] if empty else [{'path': 'edit.py', 'content': 'feature = "rebased"\n', 'executable': False}]}
        assert git(source, 'rev-parse', 'HEAD') == target_base
        if not empty:
            (source / 'edit.py').write_text(arguments['files'][0]['content'])
        git(source, 'commit', '--allow-empty', '-am', arguments['title'])
        commit = git(source, 'rev-parse', 'HEAD')
        git(bare, 'fetch', '--no-tags', str(source), commit)
        git(bare, 'update-ref', 'refs/heads/' + branch, commit, old_head)
        return {'repository_id': 101, 'repository': initial['repository'], 'number': 100,
                'url': f"https://github.com/{initial['repository']}/pull/100", 'branch': branch,
                'commit': commit, 'base_branch': 'main', 'draft': False, 'state': 'open'}

    remote = f'http://127.0.0.1:{server.server_port}'
    fresh = repo.parent / 'rebase-base'
    checked_out = local.call('github_checkout', {'directory': str(fresh), 'repository_id': 101, 'ref': selected_ref},
                             broker, remote, 'current-capability')
    assert checked_out['base_sha'] == target_base and 'number' not in local.metadata(fresh)
    assert old_head != target_base and (fresh / 'base-only.txt').read_text() == 'New target base content\n'
    if not empty:
        (fresh / 'edit.py').write_text('feature = "rebased"\n')
    receipt = local.call('github_rebase_pull_request', {'directory': str(fresh), 'title': 'Rebase feature',
        'number': 100, 'expected_head_sha': old_head, 'request_key': 'rebase-same-pr'},
        broker, remote, 'current-capability')
    assert len(calls) == 1 and 'checkout_warning' not in receipt
    assert receipt['number'] == 100 and receipt['branch'] == branch and receipt['draft'] is False
    assert receipt['url'] == f"https://github.com/{initial['repository']}/pull/100"
    saved = local.metadata(fresh)
    assert saved['number'] == 100 and saved['base_sha'] == receipt['commit'] and saved['base_branch'] == 'main'
    assert git(bare, 'rev-parse', 'refs/heads/' + branch) == receipt['commit']
    assert git(bare, 'rev-parse', receipt['commit'] + '^') == target_base
    assert git(bare, 'rev-parse', 'refs/heads/main') == target_base
    assert local.metadata(repo)['base_sha'] == initial['base_sha']
    with pytest.raises(local.GitHubToolError, match='no changed files'):
        local.collect(str(fresh), 'No new changes', '', 'rebased-unchanged')


@pytest.mark.parametrize('operation', sorted(local.PUBLICATION_TOOLS))
@pytest.mark.parametrize('empty_kind', ['clean', 'unchanged_candidates'])
def test_only_rebase_can_package_empty_changes(checkout, operation, empty_kind):
    repo, _, _, _, _ = checkout
    if empty_kind == 'unchanged_candidates':
        source = repo.parent.parent / 'source'
        for directory in (repo, source):
            (directory / 'new.py').write_text('already_published = True\n')
        git(source, 'add', '.')
        git(source, 'commit', '-m', 'Saved publication base')
        commit = git(source, 'rev-parse', 'HEAD')
        git(repo, 'fetch', '--no-tags', str(source), commit)
        (repo / '.git/moyai.json').write_text(json.dumps({**local.metadata(repo), 'base_sha': commit}))
    data = local.metadata(repo)
    arguments = {'directory': str(repo), 'title': 'History-only rebase', 'body': 'Not part of rebase payload',
                 'number': 100, 'branch': 'feature/test', 'expected_head_sha': 'b' * 40,
                 'request_key': 'empty-prepared-result', 'allow_empty': True}
    calls = []

    def broker(path, body):
        calls.append((path, body))
        return {'status': 'unconfirmed'}

    with pytest.raises(local.GitHubToolError, match='no changed files'):
        local.collect(str(repo), arguments['title'], arguments['body'], arguments['request_key'])
    if operation == 'github_rebase_pull_request':
        result = local.call(operation, arguments, broker, '', '')
        assert result == {'status': 'unconfirmed'}
        assert calls == [('/tools/call', {'name': operation, 'arguments': {
            'repository_id': data['repository_id'], 'base_sha': data['base_sha'], 'files': [],
            'number': 100, 'expected_head_sha': 'b' * 40, 'title': arguments['title'],
            'request_key': arguments['request_key']}})]
    else:
        with pytest.raises(local.GitHubToolError, match='no changed files'):
            local.call(operation, arguments, broker, '', '')
        assert not calls


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
            elif change.get('encoding') == 'base64':
                import base64
                target.write_bytes(base64.b64decode(change['content']))
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
    (repo / 'image.bin').write_bytes(b'\x89PNG\x00\xff\x80complete bytes')
    result = local.call('github_create_pull_request', {'directory': str(repo), 'title': 'Large schema',
        'body': 'First publication', 'request_key': 'first-publication'}, broker, remote, 'current-capability')
    assert 'checkout_warning' not in result
    assert local.metadata(repo)['base_sha'] == result['commit']
    with pytest.raises(local.GitHubToolError, match='no changed files'):
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
    assert (fresh / 'image.bin').read_bytes() == b'\x89PNG\x00\xff\x80complete bytes'
    (repo / 'new.py').unlink()
    payload = local.collect(str(repo), 'Remove new file', 'Body', 'remove-new-file')
    assert payload['files'] == [{'path': 'new.py', 'content': None, 'executable': False}]
    assert 'current-capability' not in (fresh / '.git/config').read_text()


@pytest.mark.parametrize('allow_empty', [False, True])
def test_local_collection_keeps_large_binary_and_more_than_one_hundred_files(checkout, allow_empty):
    import base64
    repo, _, _, _, _ = checkout
    raw = b'\x00\xff\x80' + b'x' * (11 * 1024 * 1024)
    (repo / 'image.bin').write_bytes(raw)
    for index in range(105):
        (repo / f'new-{index}.txt').write_text('Complete content')
    payload = local.collect(str(repo), 'Complete files', 'Body', 'complete-files', allow_empty=allow_empty)
    assert len(payload['files']) == 106
    binary = next(file for file in payload['files'] if file['path'] == 'image.bin')
    assert binary['encoding'] == 'base64' and base64.b64decode(binary['content']) == raw


def test_advertised_update_packages_local_files():
    from app.github import TOOLS
    tools = [{'name': name, 'inputSchema': spec[2].model_json_schema()} for name, spec in TOOLS.items()]
    updated = {tool['name']: tool for tool in local.advertised_tools(tools)}
    props = updated['github_update_pull_request']['inputSchema']['properties']
    assert set(props) == {'directory', 'number', 'title', 'request_key'}
    assert 'number' in updated['github_checkout']['inputSchema']['properties']


def test_advertised_branch_tools_preserve_broker_only_operations():
    names = [*sorted(local.LOCAL_TOOLS), 'github_create_branch', 'github_open_pull_request']
    tools = [{'name': name, 'inputSchema': {'type': 'object', 'properties': {'broker_only': {'type': 'string'}}}}
             for name in names]
    advertised = {tool['name']: tool for tool in local.advertised_tools(tools)}
    branch = advertised['github_publish_branch']['inputSchema']
    assert set(branch['properties']) == {'directory', 'title', 'branch', 'expected_head_sha', 'request_key'}
    assert set(branch['required']) == {'title', 'branch', 'request_key'}
    checkout_schema = advertised['github_checkout']['inputSchema']
    assert checkout_schema['not'] == {'required': ['number', 'ref']}
    assert 'ref' in checkout_schema['properties']
    creation = advertised['github_create_pull_request']['inputSchema']
    assert {'base_branch', 'head_branch'} <= creation['properties'].keys()
    assert not {'base_branch', 'head_branch'} & set(creation['required'])
    rebase = advertised['github_rebase_pull_request']['inputSchema']
    assert set(rebase['properties']) == {'directory', 'title', 'number', 'expected_head_sha', 'request_key'}
    assert set(rebase['required']) == set(rebase['properties'])
    assert 'target base' in rebase['properties']['directory']['description']
    for tool in tools:
        assert 'allow_empty' not in advertised[tool['name']]['inputSchema']['properties']
        if tool['name'] in {'github_create_branch', 'github_open_pull_request'}:
            assert advertised[tool['name']] is tool


@pytest.mark.parametrize('failure', [OSError, ValueError, local.GitHubToolError])
@pytest.mark.parametrize('operation', sorted(local.PUBLICATION_TOOLS))
def test_successful_publication_is_preserved_when_base_sync_fails(checkout, monkeypatch, failure, operation):
    repo, _, _, _, _ = checkout
    (repo / 'new.py').write_text('new = True\n')
    receipt = {'repository': 'BerriAI/litellm', 'number': 100, 'commit': 'c' * 40,
               'url': 'https://github.com/BerriAI/litellm/pull/100'}
    if operation == 'github_publish_branch':
        receipt = {'kind': 'branch', 'repository': 'BerriAI/litellm', 'repository_id': 101,
                   'branch': 'feature/test', 'commit': 'c' * 40, 'base_branch': 'main'}
    def sync(*args):
        raise failure('synthetic secret must not appear in the receipt')
    monkeypatch.setattr(local, 'sync_publication', sync)
    calls = []
    def publish(*args):
        calls.append(args)
        return receipt
    result = local.call(operation, {'directory': str(repo), 'title': 'Publish change',
        'body': 'A change', 'number': 100, 'branch': 'feature/test', 'expected_head_sha': 'b' * 40,
        'request_key': 'sync-failure'}, publish, '', '')
    assert all(result[key] == value for key, value in receipt.items())
    assert 'succeeded' in result['checkout_warning']
    assert len(calls) == 1 and result['commit'] == receipt['commit']
    assert 'synthetic secret' not in json.dumps(result)
    if operation == 'github_publish_branch':
        assert 'branch as ref' in result['checkout_warning'] and 'PR number' not in result['checkout_warning']


@pytest.mark.parametrize('operation', sorted(local.PUBLICATION_TOOLS))
@pytest.mark.parametrize('outcome', ['relay_failure', 'broker_failure', 'rejected', 'missing_commit', 'commit_error'])
def test_unconfirmed_publication_never_becomes_a_success_warning(checkout, monkeypatch, operation, outcome):
    from types import SimpleNamespace
    repo, _, _, _, _ = checkout
    (repo / 'new.py').write_text('new = True\n')
    calls = []
    receipt = {'error': 'denied'} if outcome in {'rejected', 'commit_error'} else {'status': 'pending'}
    if outcome == 'commit_error':
        receipt['commit'] = 'c' * 40
    def fail_environment():
        raise ValueError('environment failed before publication')
    def publish(*args):
        calls.append(args)
        if outcome == 'broker_failure':
            raise ValueError('publication not confirmed')
        return receipt
    monkeypatch.setattr(local, 'sync_publication', lambda *args, **kwargs: pytest.fail('Unconfirmed receipt was synchronized'))
    options = {'relay': SimpleNamespace(url='http://127.0.0.1:1234', token='test-run',
                                       git_environment=fail_environment)} if outcome == 'relay_failure' else {}
    arguments = {'directory': str(repo), 'title': 'Publish change', 'body': 'A change',
                 'number': 100, 'branch': 'feature/test', 'expected_head_sha': 'b' * 40,
                 'request_key': 'unconfirmed-publication'}
    if outcome in {'relay_failure', 'broker_failure'}:
        with pytest.raises(ValueError):
            local.call(operation, arguments, publish, '', '', **options)
    else:
        assert local.call(operation, arguments, publish, '', '', **options) == receipt
    assert len(calls) == (0 if outcome == 'relay_failure' else 1)
