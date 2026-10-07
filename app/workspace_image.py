"""The shared, dependency-complete Modal workspace image."""
from pathlib import Path
import re

import modal

from .config import Settings

SANDBOX_FILES = Path(__file__).parent.parent / "sandbox"


def workspace_image(settings: Settings) -> modal.Image:
    revision = settings.hermes_revision
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("HERMES_REVISION must be a full commit SHA")
    return (modal.Image.debian_slim(python_version="3.14")
            .apt_install("git", "chromium", "xvfb", "ffmpeg", "openbox", "tint2", "xterm", "xdotool", "xclip", "x11-xserver-utils", "ca-certificates", "build-essential", "libffi-dev", "ripgrep", "nodejs", "npm")
            .pip_install("playwright==1.58.0", "pillow==12.3.0")
            .env({"HERMES_RUNTIME_DIR": "/opt/hermes-tools", "PYTHONPATH": "/opt/hermes"})
            .run_commands(f"git init /opt/hermes && cd /opt/hermes && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth 1 origin {revision} && git checkout --detach FETCH_HEAD",
                          "cd /opt/hermes && python -m pm.build_env --source /opt/hermes --out /opt/hermes-env --no-install-project --extra mcp",
                          "cd /opt/hermes && /opt/hermes-env/bin/python -c 'from run_agent import AIAgent; import mcp; from cryptography.fernet import Fernet'")
            # Cache dependency layers independently of changes to the agent.
            .add_local_file(SANDBOX_FILES / "harness_dependencies.py", "/opt/workspace-runner/harness_dependencies.py", copy=True)
            .run_commands("/opt/hermes-env/bin/python /opt/workspace-runner/harness_dependencies.py")
            .add_local_file(SANDBOX_FILES / "install_access_tools.py", "/opt/workspace-runner/install_access_tools.py", copy=True)
            .run_commands("python /opt/workspace-runner/install_access_tools.py")
            .add_local_dir(SANDBOX_FILES, remote_path="/opt/workspace-runner", copy=True,
                           ignore=["**/__pycache__/**", "**/*.pyc"])
            .run_commands("python /opt/workspace-runner/hermes_compat.py",
                          "cd /opt/hermes && /opt/hermes-env/bin/python -c 'from run_agent import AIAgent; import mcp; import claude_agent_sdk'")
            .env({"PYTHONUNBUFFERED": "1", "PYTHONPATH": "/opt/hermes", "HERMES_PYTHON": "/opt/hermes-env/bin/python", "HERMES_HOME": "/tmp/hermes-home", "GIT_TERMINAL_PROMPT": "0"}))
