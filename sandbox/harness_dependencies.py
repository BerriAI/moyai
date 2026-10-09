"""Pinned runtime dependencies for the beta LiteLLM harness API.

The current PyPI wheel (1.104.0) lacks litellm.harness. Use its dependencies
with the complete upstream Python package at an immutable revision, avoiding
an unrelated Rust wheel build in the sandbox. No source files are vendored.
"""
from pathlib import Path
import importlib.util
import subprocess
import sys
import shutil

LITELLM_REVISION = '2cee61626d9581bc22bbdeefb1924f854f50d427'
LITELLM_SOURCE = Path('/opt/litellm-harness')
CODEX_SDK_VERSION = '0.161.0'


def ensure_pip() -> None:
    if importlib.util.find_spec('pip') is None:
        subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade'],
                       check=True, timeout=60)


def prepare_binary(binding):
    packages = {'opencode': 'opencode-ai@1.18.35', 'pi': '@earendil-works/pi-coding-agent@1.1.0'}
    if binding in packages and not shutil.which(binding):
        subprocess.run(['npm', 'install', '-g', packages[binding]], check=True, timeout=300)


def runtime_version(binding):
    """Fingerprint the installed CLI, including binaries from older snapshots."""
    if binding not in ('opencode', 'pi'):
        return ''
    try:
        result = subprocess.run([binding, '--version'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    value = result.stdout.strip()
    return value if result.returncode == 0 and value and len(value) <= 200 else ''


def prepare_codex():
    from importlib.metadata import PackageNotFoundError, version
    try:
        installed = version('openai-codex')
    except PackageNotFoundError:
        installed = ''
    if installed != CODEX_SDK_VERSION:
        ensure_pip()
        subprocess.run([sys.executable, '-m', 'pip', 'install',
                        'openai-codex==' + CODEX_SDK_VERSION], check=True, timeout=300)


def prepare_runtime():
    # An image build already fetched this revision: activate it before probing
    # the older wheel, without running pip on every turn.
    if (LITELLM_SOURCE / 'litellm' / 'harness').is_dir():
        revision = subprocess.run(['git', '-C', str(LITELLM_SOURCE), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
        if revision.returncode or revision.stdout.strip() != LITELLM_REVISION:
            raise RuntimeError('LiteLLM harness source revision does not match the pinned build')
        if str(LITELLM_SOURCE) not in sys.path:
            sys.path.insert(0, str(LITELLM_SOURCE))
    try:
        from litellm import Harness, aagent_session
        import claude_agent_sdk
        import deepagents, langchain_litellm
        return
    except ImportError:
        pass
    # Hermes creates its isolated environment without pip. Bootstrap the
    # installer in that interpreter, including when restoring older snapshots.
    ensure_pip()
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'litellm==1.104.0',
                    'claude-agent-sdk==0.2.163', 'mcp<2', 'starlette', 'uvicorn',
                    'deepagents==0.7.22', 'langchain-litellm==0.11.0'],
                   check=True, timeout=300)
    if not LITELLM_SOURCE.exists():
        subprocess.run(['git', 'init', str(LITELLM_SOURCE)], check=True, timeout=30)
    revision = subprocess.run(['git', '-C', str(LITELLM_SOURCE), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    if revision.returncode or revision.stdout.strip() != LITELLM_REVISION:
        subprocess.run(['git', '-C', str(LITELLM_SOURCE), 'fetch', '--depth', '1',
                        'https://github.com/BerriAI/litellm.git', LITELLM_REVISION], check=True, timeout=300)
        subprocess.run(['git', '-C', str(LITELLM_SOURCE), 'checkout', '--detach', LITELLM_REVISION], check=True, timeout=60)
    # Preparation must precede importing LiteLLM in the agent process.
    for name in list(sys.modules):
        if name == 'litellm' or name.startswith('litellm.'):
            del sys.modules[name]
    sys.path.insert(0, str(LITELLM_SOURCE))
    from litellm import Harness, aagent_session


if __name__ == '__main__':
    prepare_codex()
    prepare_runtime()
    prepare_binary('opencode')
    prepare_binary('pi')
