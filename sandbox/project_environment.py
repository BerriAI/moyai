"""Restart project services after restoring a filesystem-only snapshot."""
import os
from pathlib import Path
import re
import subprocess

from agent.tools.github_tools import GitHubToolError, metadata


REPAIR_NOTICE = ('The prepared project needs repair. Workspace files are preserved and authorized tools remain available. '
                 'Inspect the checkout and repair it before explicitly rerunning project startup or verification.')


def command_environment():
    return {key: value for key, value in os.environ.items() if key not in {
        'WORKSPACE_RUN_TOKEN', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MOYAI_CREDENTIAL_PROXY_URL'}}


def needs_repair(phase, error_type, exit_code=None):
    health = {'status': 'needs_repair', 'phase': phase, 'error_type': error_type}
    if exit_code is not None:
        health['exit_code'] = exit_code
    return health


def project_root_health(spec):
    project = spec.get('project_environment')
    if not project:
        return None
    root = Path('/workspace/repo')
    if not root.is_dir():
        return needs_repair('root', 'MissingProjectRoot')
    try:
        def git(*arguments):
            return subprocess.run(['git', '-C', str(root), *arguments], env=command_environment(),
                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10).stdout.strip()
        if Path(git('rev-parse', '--show-toplevel')).resolve() != root.resolve():
            return needs_repair('root', 'ProjectRootMismatch')
        origin = git('remote', 'get-url', 'origin').removesuffix('.git').lower()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return needs_repair('root', type(exc).__name__, getattr(exc, 'returncode', None))
    if project.get('repository_id'):
        try:
            recorded = metadata(root)
        except (GitHubToolError, AttributeError, TypeError):
            recorded = {}
        identity, repository = recorded.get('repository_id'), recorded.get('repository')
        if (type(identity) is int and identity > 0 and isinstance(repository, str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository)):
            if identity != project['repository_id'] or origin != 'https://github.com/' + repository.lower():
                return needs_repair('root', 'ProjectRootMismatch')
            return None
    if origin != 'https://github.com/' + project['repository'].lower():
        return needs_repair('root', 'ProjectRootMismatch')
    return None


def activate_project(spec):
    if not spec.get('project_environment'):
        return
    health = project_root_health(spec)
    if health:
        return health
    root = Path('/workspace/repo')
    # Project interpreters are separate from the Hermes interpreter. Expose them
    # to agent shell tools and restarted services without replacing Hermes.
    for directory in (str(root / '.venv/bin'), '/opt/moyai-node/bin'):
        if Path(directory).is_dir() and directory not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = directory + os.pathsep + os.environ.get('PATH', '')


def prepare_project(spec, emit):
    if not spec.get('project_environment'):
        return
    health = activate_project(spec)
    if health:
        emit('status', REPAIR_NOTICE, {'activity_version': 1, 'phase': 'environment', 'project_health': health})
        return health
    root = Path('/workspace/repo')
    project = spec['project_environment']
    emit('status', 'Starting project services: ' + project['name'], {'activity_version': 1, 'phase': 'environment'})
    env = command_environment()
    for phase in ('startup', 'verify'):
        if project.get(phase):
            try:
                subprocess.run(['bash', '-euo', 'pipefail', '-c', project[phase]],
                               cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            except (OSError, subprocess.CalledProcessError) as exc:
                health = needs_repair(phase, type(exc).__name__, getattr(exc, 'returncode', None))
                emit('status', REPAIR_NOTICE, {'activity_version': 1, 'phase': 'environment', 'project_health': health})
                return health
    health = {'status': 'ready', 'phase': 'verify'}
    emit('status', 'Project environment ready · ' + project['commit_sha'][:8],
         {'activity_version': 1, 'phase': 'environment', 'project_health': health})
    return health
