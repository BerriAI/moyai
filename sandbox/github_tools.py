"""Local Git checkout and exact working-tree packaging, with no GitHub credential."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path('/workspace')
MAX_FILE = 1024 * 1024
MAX_TOTAL = 2 * MAX_FILE


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
        raise GitHubToolError('Use github_checkout first to record the approved repository and base.') from None
    if not re.fullmatch(r'[0-9a-f]{40}', data.get('base_sha', '')):
        raise GitHubToolError('Checkout metadata is invalid. Inspect the base before publishing.')
    return data


def checkout(broker, remote, token, directory=''):
    repo = broker('/tools/call', {'name': 'github_checkout', 'arguments': {}})
    if repo.get('error'):
        return repo
    target = directory_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical = 'https://github.com/' + repo['repository'] + '.git'
    if target.exists():
        if (target / '.git/moyai.json').exists():
            data = metadata(target)
            if data['repository'].lower() != repo['repository'].lower():
                raise GitHubToolError('This directory belongs to another repository. Choose an empty directory.')
            git(target, 'cat-file', '-e', data['base_sha'] + '^{commit}')
            return {**data, 'directory': str(target), 'reused': True, 'instruction': 'Existing files and local changes were preserved.'}
        if not (target / '.git').is_dir() or (target / '.git').is_symlink():
            raise GitHubToolError('The destination already exists. Choose an empty directory; files will not be overwritten.')
        origin = git(target, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
        if origin.lower() != canonical.removesuffix('.git').lower():
            raise GitHubToolError('The existing checkout has a different origin. Choose an empty directory.')
        base = git(target, 'rev-parse', '--verify', 'refs/remotes/origin/' + repo['default_branch']).decode().strip()
    else:
        # Capability is sent in a temporary environment, never in argv, URL or Git config.
        env = dict(os.environ)
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
                   GIT_CONFIG_VALUE_0='Authorization: Bearer ' + token)
        git(target.parent, 'clone', '--depth', '1', '--single-branch', '--no-tags', '--branch', repo['default_branch'],
            '--', remote.rstrip('/') + '/github.git', str(target), env=env)
        git(target, 'remote', 'set-url', 'origin', canonical)
        base = git(target, 'rev-parse', 'HEAD').decode().strip()
    data = {'repository': repo['repository'], 'base_sha': base, 'default_branch': repo['default_branch']}
    (target / '.git/moyai.json').write_text(json.dumps(data))
    return {**data, 'directory': str(target), 'reused': False,
            'instruction': 'Work here, then use github_create_pull_request to package actual files for approval. Git push is unavailable.'}


def collect(directory, title, body, request_key):
    target = directory_path(directory)
    data = metadata(target)
    raw = git(target, 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', data['base_sha'], '--')
    raw += git(target, 'ls-files', '--others', '--exclude-standard', '-z')
    try:
        paths = sorted(set(p.decode('utf-8') for p in raw.split(b'\0') if p))
    except UnicodeDecodeError:
        raise GitHubToolError('Only UTF-8 file names can be published.') from None
    if not 1 <= len(paths) <= 100:
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
            files.append({'path': name, 'content': None, 'executable': False})
            continue
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or path.stat().st_size > MAX_FILE:
            raise GitHubToolError('Publish regular text files of at most 1 MiB each.')
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) > MAX_FILE or total > MAX_TOTAL or b'\0' in raw:
            raise GitHubToolError('Publish at most 2 MiB of UTF-8 text per PR; binary files are unsupported.')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise GitHubToolError('Binary files cannot be published with this tool.') from None
        files.append({'path': name, 'content': content, 'executable': bool(mode & 0o111)})
    return {'repository': data['repository'], 'base_sha': data['base_sha'], 'request_key': request_key,
            'title': title, 'body': body, 'files': files}


def advertised_tools(tools):
    result = []
    for tool in tools:
        if tool['name'] in {'github_checkout', 'github_create_pull_request'}:
            properties = {'directory': {'type': 'string', 'description': 'Repository directory inside /workspace; defaults to /workspace/repo.'}}
            required = []
            if tool['name'] == 'github_create_pull_request':
                properties.update({
                    'title': {'type': 'string', 'minLength': 3, 'maxLength': 250},
                    'body': {'type': 'string', 'minLength': 1, 'maxLength': 20000},
                    'request_key': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{8,80}$',
                                    'description': 'Unique name for this publication. Keep unchanged to recover an uncertain result; never retry writes automatically.'}})
                required = ['title', 'body', 'request_key']
            tool = {**tool, 'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
        result.append(tool)
    return result


def call(name, args, broker, remote, token):
    if name == 'github_checkout':
        if set(args) - {'directory'}:
            raise GitHubToolError('Checkout accepts only a directory.')
        return checkout(broker, remote, token, **args)
    if name == 'github_create_pull_request':
        payload = collect(directory=args.get('directory', ''), title=args['title'], body=args['body'], request_key=args['request_key'])
        return broker('/tools/call', {'name': name, 'arguments': payload})
    raise GitHubToolError('Unknown local GitHub tool.')
