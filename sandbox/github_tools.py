"""Local Git checkout and exact working-tree packaging, with no GitHub credential."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path('/workspace')
try:
    from .github_limits import MAX_FILE, MAX_TOTAL
except ImportError:
    from github_limits import MAX_FILE, MAX_TOTAL


class GitHubToolError(ValueError):
    pass


def git(directory, *args, env=None):
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=',
                                 '-c', 'http.followRedirects=false', *args], cwd=directory,
                                env=env, capture_output=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubToolError('Git did not finish. Inspect the checkout before continuing.') from None
    if result.returncode:
        # Git errors can echo URLs/headers; never relay raw diagnostics.
        raise GitHubToolError('Git could not complete the operation. Check that the repository, base commit and connection are available.')
    return result.stdout


def directory_path(value):
    directory = Path(value or ROOT / 'repo')
    if not directory.is_absolute():
        directory = ROOT / directory
    directory = directory.resolve()
    if not directory.is_relative_to(ROOT.resolve()) or directory == ROOT.resolve():
        raise GitHubToolError('Choose a repository directory inside /workspace.')
    return directory


def metadata(directory):
    if not (directory / '.git').is_dir() or (directory / '.git').is_symlink():
        raise GitHubToolError('Use github_checkout to prepare a regular repository checkout first.')
    try:
        data = json.loads((directory / '.git/moyai.json').read_text())
    except (OSError, ValueError):
        raise GitHubToolError('Use github_checkout first to record the repository and base.') from None
    if not re.fullmatch(r'[0-9a-f]{40}', data.get('base_sha', '')):
        raise GitHubToolError('Checkout metadata is invalid. Inspect the base before publishing.')
    return data


def checkout(broker, remote, token, directory='', repository='', number=None):
    arguments = {'repository': repository} if repository else {}
    if number is not None:
        arguments['number'] = number
    repo = broker('/tools/call', {'name': 'github_checkout', 'arguments': arguments})
    if repo.get('error'):
        return repo
    target = directory_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical = 'https://github.com/' + repo['repository'] + '.git'
    git_path = repo.get('git_path', '/github.git')
    if git_path not in {'/github.git', '/github/' + repo['repository'] + '.git'}:
        raise GitHubToolError('The shared GitHub checkout path is invalid.')
    if target.exists():
        if (target / '.git/moyai.json').exists():
            data = metadata(target)
            if data['repository'].lower() != repo['repository'].lower():
                raise GitHubToolError('This directory belongs to another repository. Choose an empty directory.')
            if number is not None and (data.get('number') != number or data['base_sha'] != repo['base_sha']):
                raise GitHubToolError('Choose a fresh directory to check out the current PR head; local files were preserved.')
            git(target, 'cat-file', '-e', data['base_sha'] + '^{commit}')
            return {**data, 'directory': str(target), 'reused': True, 'instruction': 'Existing files and local changes were preserved.'}
        if not (target / '.git').is_dir() or (target / '.git').is_symlink():
            raise GitHubToolError('The destination already exists. Choose an empty directory; files will not be overwritten.')
        if number is not None:
            raise GitHubToolError('Choose a fresh directory for a PR checkout; local files were preserved.')
        origin = git(target, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
        if origin.lower() != canonical.removesuffix('.git').lower():
            raise GitHubToolError('The existing checkout has a different origin. Choose an empty directory.')
        base = git(target, 'rev-parse', '--verify', 'refs/remotes/origin/' + repo['default_branch']).decode().strip()
    else:
        # Capability is sent in a temporary environment, never in argv, URL or Git config.
        env = dict(os.environ)
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
                   GIT_CONFIG_VALUE_0='Authorization: Bearer ' + token)
        if number is not None:
            target.mkdir()
            git(target, 'init')
            git(target, 'remote', 'add', 'origin', canonical)
            git(target, 'fetch', '--depth', '1', '--no-tags', '--', remote.rstrip('/') + git_path,
                repo['checkout_ref'], env=env)
            git(target, 'checkout', '--detach', 'FETCH_HEAD')
        else:
            git(target.parent, 'clone', '--depth', '1', '--single-branch', '--no-tags', '--branch', repo['default_branch'],
                '--', remote.rstrip('/') + git_path, str(target), env=env)
        git(target, 'remote', 'set-url', 'origin', canonical)
        base = git(target, 'rev-parse', 'HEAD').decode().strip()
    data = {'repository': repo['repository'], 'base_sha': base, 'default_branch': repo['default_branch']}
    if number is not None:
        data['number'] = number
    (target / '.git/moyai.json').write_text(json.dumps(data))
    return {**data, 'directory': str(target), 'reused': False,
            'instruction': ('Use github_update_pull_request to publish follow-up edits to this PR.' if number is not None else
                            'Use github_create_pull_request to publish actual files as a normal PR without an administrator approval step.')
                           + ' Git push is unavailable.'}


def collect(directory, title, body, request_key):
    target = directory_path(directory)
    data = metadata(target)
    raw = git(target, 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', data['base_sha'], '--')
    raw += git(target, 'ls-files', '--others', '--exclude-standard', '-z')
    try:
        paths = sorted(set(p.decode('utf-8') for p in raw.split(b'\0') if p))
    except UnicodeDecodeError:
        raise GitHubToolError('Only UTF-8 file names can be published.') from None
    # After publication the index can still describe the old checkout. Compare
    # each candidate with the published base, including previously untracked files.
    base_entries = {}
    for entry in git(target, 'ls-tree', '-r', '-z', data['base_sha']).split(b'\0'):
        if entry:
            info, name = entry.split(b'\t', 1)
            base_entries[name] = info.decode().split()
    if not paths:
        raise GitHubToolError('There are no changed files to publish.')
    if len(paths) > 10000:
        raise GitHubToolError('A PR must contain between 1 and 100 changed text files. Split larger changes.')
    files, total = [], 0
    for name in paths:
        parts = name.split('/')
        lower = name.lower()
        if (any(p in {'', '.', '..'} or p.lower() in {'.git', '.ssh', 'codeowners'} or p.lower().startswith('.env') for p in parts)
                or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or lower in {'.github', '.github/workflows', '.github/actions'}
                or lower.startswith(('.github/workflows/', '.github/actions/')) or lower.endswith(('.pem', '.key'))):
            raise GitHubToolError('Remove credential, workflow or access-control files from the change before publishing.')
        path = target / name
        for ancestor in [path, *path.parents]:
            if ancestor == target:
                break
            if ancestor.is_symlink():
                raise GitHubToolError('Symlinks cannot be published or followed.')
        if not path.exists():
            if name.encode('utf-8') not in base_entries:
                continue
            files.append({'path': name, 'content': None, 'executable': False})
            continue
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or path.stat().st_size > MAX_FILE:
            raise GitHubToolError('Publish regular text files of at most 10 MiB each.')
        raw = path.read_bytes()
        if name.encode('utf-8') in base_entries:
            base_mode, kind, sha = base_entries[name.encode('utf-8')]
            if (kind == 'blob' and base_mode == ('100755' if mode & 0o111 else '100644')
                    and git(target, 'hash-object', '--no-filters', '--', name).decode().strip() == sha):
                continue
        total += len(raw)
        if len(raw) > MAX_FILE or total > MAX_TOTAL or b'\0' in raw:
            raise GitHubToolError('Publish at most 20 MiB of UTF-8 text per publication; binary files are unsupported.')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise GitHubToolError('Binary files cannot be published with this tool.') from None
        files.append({'path': name, 'content': content, 'executable': bool(mode & 0o111)})
    if not 1 <= len(files) <= 100:
        raise GitHubToolError('A publication must contain between 1 and 100 changed text files.')
    return {'repository': data['repository'], 'base_sha': data['base_sha'], 'request_key': request_key,
            'title': title, 'body': body, 'files': files}


def advertised_tools(tools):
    result = []
    for tool in tools:
        if tool['name'] in {'github_checkout', 'github_create_pull_request', 'github_update_pull_request'}:
            properties = {'directory': {'type': 'string', 'description': 'Repository directory inside /workspace; defaults to /workspace/repo.'}}
            required = []
            if tool['name'] == 'github_checkout':
                properties['repository'] = {'type': 'string', 'description': 'Allowed owner/repository, such as BerriAI/moyai-devin. Use github_repositories to list choices; defaults to the session repository.'}
                properties['number'] = {'type': 'integer', 'minimum': 1, 'description': 'Optional PR number; use a fresh directory to check out its current head.'}
            if tool['name'] in {'github_create_pull_request', 'github_update_pull_request'}:
                properties.update({
                    'title': {'type': 'string', 'minLength': 3, 'maxLength': 250},
                    'body': {'type': 'string', 'minLength': 1, 'maxLength': 20000},
                    'request_key': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{8,80}$',
                                    'description': 'Unique name for this publication. Keep unchanged to recover an uncertain result; never retry writes automatically.'}})
                required = ['title', 'body', 'request_key']
                if tool['name'] == 'github_update_pull_request':
                    properties.pop('body')
                    properties['number'] = {'type': 'integer', 'minimum': 1}
                    required = ['title', 'number', 'request_key']
            tool = {**tool, 'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
        result.append(tool)
    return result


def call(name, args, broker, remote, token):
    if name == 'github_checkout':
        if set(args) - {'directory', 'repository', 'number'}:
            raise GitHubToolError('Checkout accepts only a directory, repository and optional PR number.')
        return checkout(broker, remote, token, **args)
    if name in {'github_create_pull_request', 'github_update_pull_request'}:
        payload = collect(directory=args.get('directory', ''), title=args['title'], body=args.get('body', ''), request_key=args['request_key'])
        if name == 'github_update_pull_request':
            payload.pop('body')
            payload['number'] = args['number']
        result = broker('/tools/call', {'name': name, 'arguments': payload})
        if result.get('commit') and not result.get('error'):
            target = directory_path(args.get('directory', ''))
            try:
                sync_publication(target, result, remote, token)
            except (GitHubToolError, OSError):
                result = {**result, 'checkout_warning': 'Publication succeeded, but local base synchronization failed. Recover with github_checkout using this PR number in a fresh directory before further edits.'}
        return result
    raise GitHubToolError('Unknown local GitHub tool.')


def sync_publication(target, result, remote, token):
    data = metadata(target)
    if not re.fullmatch(r'[0-9a-f]{40}', result.get('commit', '')) or result.get('repository') != data['repository']:
        raise GitHubToolError('Invalid publication receipt.')
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
               GIT_CONFIG_VALUE_0='Authorization: Bearer ' + token)
    git(target, 'fetch', '--no-tags', '--', remote.rstrip('/') + '/github/' + data['repository'] + '.git',
        result['commit'], env=env)
    data.update(base_sha=result['commit'], number=result['number'])
    path = target / '.git/moyai.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
