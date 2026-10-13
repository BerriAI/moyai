"""Local Git checkout and exact working-tree packaging, with no GitHub credential."""
import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path('/workspace')
from sandbox.access_transport import git_environment
from agent.github_paths import publishable_path
from agent.github_refs import validate_branch_name, validate_checkout_ref

PUBLICATION_TOOLS = frozenset({'github_create_pull_request', 'github_update_pull_request',
                               'github_publish_branch', 'github_rebase_pull_request'})
LOCAL_TOOLS = PUBLICATION_TOOLS | {'github_checkout'}


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


def checkout(broker, remote, token, directory='', repository='', number=None, repository_id=None, ref=None, *, env=None):
    if number is not None and ref is not None:
        raise GitHubToolError('Choose a PR number or a source ref, not both.')
    if ref is not None:
        try:
            ref = validate_checkout_ref(ref)
        except ValueError:
            raise GitHubToolError('Choose a valid branch or full commit SHA as the source ref.') from None
    arguments = {'repository': repository} if repository else {}
    if repository_id is not None:
        arguments['repository_id'] = repository_id
    if number is not None:
        arguments['number'] = number
    if ref:
        arguments['ref'] = ref
    repo = broker('/tools/call', {'name': 'github_checkout', 'arguments': arguments})
    if repo.get('error'):
        return repo
    target = directory_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical = 'https://github.com/' + repo['repository'] + '.git'
    git_path = repo.get('git_path', '/github.git')
    if type(repo.get('repository_id')) is not int or repo['repository_id'] <= 0 or git_path != f"/github/repositories/{repo['repository_id']}.git":
        raise GitHubToolError('The shared GitHub checkout path is invalid.')
    if target.exists():
        if (target / '.git/moyai.json').exists():
            data = metadata(target)
            if not data.get('repository_id'):
                # Resolve legacy metadata through the authorized broker, never from a redirecting Git URL.
                old = broker('/tools/call', {'name': 'github_repository', 'arguments': {'repository': data['repository']}})
                data['repository_id'] = old.get('repository_id')
            if data.get('repository_id') != repo['repository_id']:
                raise GitHubToolError('This directory belongs to another repository. Choose an empty directory.')
            if number is not None and (data.get('number') != number or data['base_sha'] != repo['base_sha']):
                raise GitHubToolError('Choose a fresh directory to check out the current PR head; local files were preserved.')
            if ref and (data.get('ref') != ref or data['base_sha'] != repo['base_sha']):
                raise GitHubToolError('Choose a fresh directory to check out this source ref; local files were preserved.')
            git(target, 'cat-file', '-e', data['base_sha'] + '^{commit}')
            data['repository'] = repo['repository']
            (target / '.git/moyai.json').write_text(json.dumps(data))
            git(target, 'remote', 'set-url', 'origin', canonical)
            return {**data, 'directory': str(target), 'reused': True, 'instruction': 'Existing files and local changes were preserved.'}
        if not (target / '.git').is_dir() or (target / '.git').is_symlink():
            raise GitHubToolError('The destination already exists. Choose an empty directory; files will not be overwritten.')
        if number is not None or ref:
            raise GitHubToolError('Choose a fresh directory for a PR or source-ref checkout; local files were preserved.')
        origin = git(target, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
        if origin.lower() != canonical.removesuffix('.git').lower():
            if not origin.startswith('https://github.com/'):
                raise GitHubToolError('The existing checkout has a different origin. Choose an empty directory.')
            old = broker('/tools/call', {'name': 'github_repository', 'arguments': {'repository': origin.removeprefix('https://github.com/')}})
            if old.get('repository_id') != repo['repository_id']:
                raise GitHubToolError('The existing checkout has a different origin. Choose an empty directory.')
        base = git(target, 'rev-parse', '--verify', 'refs/remotes/origin/' + repo['default_branch']).decode().strip()
    else:
        # Capability is sent in a temporary environment, never in argv, URL or Git config.
        env = env if env is not None else git_environment(remote, token)
        if number is not None or ref:
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
        if (number is not None or ref) and base != repo['base_sha']:
            raise GitHubToolError('The source ref changed during checkout. Choose a fresh directory and inspect the current revision.')
    data = {'repository_id': repo['repository_id'], 'repository': repo['repository'], 'base_sha': base,
            'default_branch': repo['default_branch'], 'base_branch': repo.get('base_branch', repo['default_branch'])}
    if repo.get('checkout_ref'):
        data['checkout_ref'] = repo['checkout_ref']
    if ref:
        data['ref'] = ref
    if number is not None:
        data['number'] = number
    (target / '.git/moyai.json').write_text(json.dumps(data))
    return {**data, 'directory': str(target), 'reused': False,
            'instruction': ('Use github_update_pull_request to publish follow-up edits to this PR. An external PR requires explicit requester approval through github_request_pull_request_write_access for this saved chat.' if number is not None else
                            'Use github_create_pull_request to publish actual files as a normal PR without an administrator approval step, or github_publish_branch to publish without a PR.')
                           + ' Git push is unavailable.'}


def collect(directory, title, body, request_key, *, allow_empty=False):
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
    if not paths and not allow_empty:
        raise GitHubToolError('There are no changed files to publish.')
    files = []
    for name in paths:
        if not publishable_path(name):
            raise GitHubToolError('Remove credential or access-control files from the change before publishing.')
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
        if not stat.S_ISREG(mode):
            raise GitHubToolError('Only regular files can be published.')
        raw = path.read_bytes()
        if name.encode('utf-8') in base_entries:
            base_mode, kind, sha = base_entries[name.encode('utf-8')]
            if (kind == 'blob' and base_mode == ('100755' if mode & 0o111 else '100644')
                    and git(target, 'hash-object', '--no-filters', '--', name).decode().strip() == sha):
                continue
        try:
            if b'\0' in raw:
                raise UnicodeError()
            content = raw.decode('utf-8')
            encoding = {}
        except UnicodeError:
            content = base64.b64encode(raw).decode()
            encoding = {'encoding': 'base64'}
        files.append({'path': name, 'content': content, 'executable': bool(mode & 0o111), **encoding})
    if not files and not allow_empty:
        raise GitHubToolError('There are no changed files to publish.')
    return {**({'repository_id': data['repository_id']} if data.get('repository_id') else {'repository': data['repository']}), 'base_sha': data['base_sha'], 'request_key': request_key,
            'title': title, 'body': body, 'files': files}


def advertised_tools(tools):
    result = []
    for tool in tools:
        if tool['name'] in LOCAL_TOOLS:
            properties = {'directory': {'type': 'string', 'description': 'Repository directory inside /workspace; defaults to /workspace/repo.'}}
            required = []
            if tool['name'] == 'github_checkout':
                properties['repository_id'] = {'type': 'integer', 'minimum': 1, 'description': 'Permanent ID from github_repositories. Preferred over a repository name.'}
                properties['repository'] = {'type': 'string', 'description': 'Allowed owner/repository, such as BerriAI/moyai. Use github_repositories to list choices; defaults to the session repository.'}
                properties['number'] = {'type': 'integer', 'minimum': 1, 'description': 'Optional PR number; use a fresh directory to check out its current head.'}
                properties['ref'] = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                                     'description': 'Optional branch or full commit SHA; mutually exclusive with number. Use a fresh directory.'}
            if tool['name'] in PUBLICATION_TOOLS:
                properties.update({
                    'title': {'type': 'string', 'minLength': 3, 'maxLength': 250},
                    'body': {'type': 'string', 'minLength': 1, 'maxLength': 20000},
                    'request_key': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{8,80}$',
                                    'description': 'Unique name for this publication. Keep unchanged to recover an uncertain result; never retry writes automatically.'}})
                required = ['title', 'body', 'request_key']
                if tool['name'] in {'github_update_pull_request', 'github_rebase_pull_request'}:
                    properties.pop('body')
                    properties['number'] = {'type': 'integer', 'minimum': 1}
                    required = ['title', 'number', 'request_key']
                    if tool['name'] == 'github_rebase_pull_request':
                        properties['directory']['description'] = (
                            'Fresh checkout of the current PR target base branch or full SHA, with feature edits reapplied; '
                            'do not use a checkout of the old PR head.')
                        properties['expected_head_sha'] = {'type': 'string', 'pattern': '^[0-9a-f]{40}$',
                                                           'description': 'Exact old PR head SHA being replaced; distinct from the checkout base SHA.'}
                        required += ['directory', 'expected_head_sha']
                elif tool['name'] == 'github_publish_branch':
                    properties.pop('body')
                    properties['branch'] = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                                            'description': 'Destination branch; never publishes a pull request.'}
                    properties['expected_head_sha'] = {'type': 'string', 'pattern': '^[0-9a-f]{40}$',
                                                       'description': 'Current remote head required when updating an owned branch.'}
                    required = ['title', 'branch', 'request_key']
                else:
                    properties['base_branch'] = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                                                 'description': 'Optional PR base branch; omission preserves default-branch publication.'}
                    properties['head_branch'] = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                                                 'description': 'Optional new PR head branch; existing foreign branches cannot be overwritten.'}
            tool = {**tool, 'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
            if tool['name'] == 'github_checkout':
                tool['inputSchema']['not'] = {'required': ['number', 'ref']}
        result.append(tool)
    return result


def call(name, args, broker, remote, token, *, relay=None):
    options = {}
    if relay is not None:
        remote, token = relay.url, relay.token
        options['env'] = relay.git_environment()
    if name == 'github_checkout':
        if set(args) - {'directory', 'repository', 'repository_id', 'number', 'ref'}:
            raise GitHubToolError('Checkout accepts only a directory, repository and optional PR number or source ref.')
        return checkout(broker, remote, token, **args, **options)
    if name in PUBLICATION_TOOLS:
        fields = ('branch',) if name == 'github_publish_branch' else (
            ('base_branch', 'head_branch') if name == 'github_create_pull_request' else ())
        try:
            branches = {field: validate_branch_name(args[field]) for field in fields if field in args}
        except ValueError as exc:
            raise GitHubToolError(str(exc)) from None
        payload = collect(directory=args.get('directory', ''), title=args['title'], body=args.get('body', ''),
                          request_key=args['request_key'], allow_empty=name == 'github_rebase_pull_request')
        if name in {'github_update_pull_request', 'github_rebase_pull_request'}:
            payload.pop('body')
            payload['number'] = args['number']
            if name == 'github_rebase_pull_request':
                if type(payload.get('repository_id')) is not int or payload['repository_id'] <= 0:
                    raise GitHubToolError('Use github_checkout to confirm the repository ID before rebasing a PR.')
                payload['expected_head_sha'] = args['expected_head_sha']
        elif name == 'github_publish_branch':
            if type(payload.get('repository_id')) is not int or payload['repository_id'] <= 0:
                raise GitHubToolError('Use github_checkout to confirm the repository ID before publishing a branch.')
            payload.pop('body')
            payload['branch'] = branches['branch']
            if 'expected_head_sha' in args:
                payload['expected_head_sha'] = args['expected_head_sha']
        else:
            payload.update(branches)
        result = broker('/tools/call', {'name': name, 'arguments': payload})
        if result.get('commit') and not result.get('error'):
            target = directory_path(args.get('directory', ''))
            try:
                sync_publication(target, result, remote, token, **options)
            except (ValueError, OSError):
                source = 'this branch as ref' if result.get('kind') == 'branch' else 'this PR number'
                result = {**result, 'checkout_warning': 'Publication succeeded, but local base synchronization failed. '
                          f'Recover with github_checkout using {source} in a fresh directory before further edits.'}
        return result
    raise GitHubToolError('Unknown local GitHub tool.')


def sync_publication(target, result, remote, token, *, env=None):
    data = metadata(target)
    if (not isinstance(result.get('commit'), str) or not re.fullmatch(r'[0-9a-f]{40}', result['commit'])
            or type(result.get('repository_id')) is not int or result['repository_id'] <= 0
            or not isinstance(result.get('repository'), str) or not result['repository']
            or (data.get('repository_id') and result['repository_id'] != data['repository_id'])):
        raise GitHubToolError('Invalid publication receipt.')
    branch_only = result.get('kind') == 'branch'
    if branch_only:
        if not isinstance(result.get('branch'), str) or not isinstance(result.get('base_branch'), str):
            raise GitHubToolError('Invalid branch publication receipt.')
        try:
            checkout_ref = 'refs/heads/' + validate_branch_name(result['branch'])
        except ValueError:
            raise GitHubToolError('Invalid branch publication receipt.') from None
    else:
        if type(result.get('number')) is not int or result['number'] <= 0:
            raise GitHubToolError('Invalid PR publication receipt.')
        checkout_ref = result['commit']
    env = env if env is not None else git_environment(remote, token)
    git(target, 'fetch', '--no-tags', '--', remote.rstrip('/') + f"/github/repositories/{result['repository_id']}.git",
        checkout_ref, env=env)
    if git(target, 'rev-parse', 'FETCH_HEAD').decode().strip() != result['commit']:
        raise GitHubToolError('The published branch changed before local synchronization. Recover its current head in a fresh directory.')
    data.update(base_sha=result['commit'], repository_id=result['repository_id'], repository=result['repository'])
    if branch_only:
        data.pop('number', None)
        data.update(branch=result['branch'], ref=result['branch'], checkout_ref=checkout_ref, base_branch=result['base_branch'])
    else:
        data['number'] = result['number']
        if 'base_branch' in result:
            data['base_branch'] = result['base_branch']
    path = target / '.git/moyai.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
