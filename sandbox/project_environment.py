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
    emit('status', 'Starting project services: ' + project['name'], {'activity_version': 1, 'phase': 'environment'})
    # Project scripts do not receive the agent's live broker capability.
    env = {k: v for k, v in os.environ.items() if k not in {
        'WORKSPACE_RUN_TOKEN', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MOYAI_CREDENTIAL_PROXY_URL'}}
    if project.get('startup'):
        subprocess.run(['bash', '-euo', 'pipefail', '-c', project['startup']],
                       cwd=root, env=env, check=True, timeout=300, stdout=subprocess.DEVNULL)
    emit('status', 'Project environment ready · ' + project['commit_sha'][:8], {'activity_version': 1, 'phase': 'environment'})
