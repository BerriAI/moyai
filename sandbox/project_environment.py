"""Restart project services after restoring a filesystem-only snapshot."""
import os
from pathlib import Path
import subprocess


def prepare_project(spec, emit):
    project = spec.get('project_environment')
    if not project:
        return
    root = Path('/workspace/repo')
    if not root.is_dir():
        raise RuntimeError('The prepared project repository is missing')
    # Project interpreters are separate from the Hermes interpreter. Expose them
    # to agent shell tools and restarted services without replacing Hermes.
    for directory in (str(root / '.venv/bin'), '/opt/moyai-node/bin'):
        if Path(directory).is_dir() and directory not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = directory + os.pathsep + os.environ.get('PATH', '')
    emit('status', 'Starting project services: ' + project['name'], {'activity_version': 1, 'phase': 'environment'})
    # Project scripts do not receive the agent's live broker capability.
    env = {k: v for k, v in os.environ.items() if k not in {
        'WORKSPACE_RUN_TOKEN', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MOYAI_CREDENTIAL_PROXY_URL'}}
    if project.get('startup'):
        subprocess.run(['bash', '-euo', 'pipefail', '-c', project['startup']],
                       cwd=root, env=env, check=True, timeout=300, stdout=subprocess.DEVNULL)
    emit('status', 'Project environment ready · ' + project['commit_sha'][:8], {'activity_version': 1, 'phase': 'environment'})
