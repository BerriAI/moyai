"""Run Moyai's real Python harness in a disposable local workspace.

This is the CI entry point, not a deployed Moyai HTTP client. Run it in a
throwaway container: coding agents can execute shell commands.

AGENT_MODEL is supplied by this test's caller. Native harness selection follows
production settings, but this controlled-model lane does not discover or test
model changes made only in the deployed application's environment.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import Mapping


class AgentRunError(RuntimeError):
    """An incomplete execution or missing trace receipt cannot pass an eval."""


@dataclass(frozen=True)
class AgentResult:
    output: str
    trace_id: str
    session_id: str
    agent_version: str
    model_calls: int
    tool_calls: int


def runtime_selection(environ: Mapping[str, str]) -> tuple[str, str]:
    """Resolve the production model/harness rules using only supplied settings."""
    from app.config import Settings

    class EvalSettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                       dotenv_settings, file_secret_settings):
            # A caller-provided environment must not borrow an unrelated .env or
            # parent process's model/harness and silently change the experiment.
            return (init_settings,)

    configured = {'agent_model': environ['AGENT_MODEL']}
    override = environ.get('MOYAI_EVAL_HARNESS') or environ.get('AGENT_HARNESS')
    if override:
        if override not in {'codex', 'claude-agent-sdk'}:
            raise ValueError('MOYAI_EVAL_HARNESS or AGENT_HARNESS must be codex or claude-agent-sdk.')
        configured['agent_harness'] = override
    settings = EvalSettings(**configured)
    model = settings.resolve_model()
    harness = settings.default_harness(model)
    if harness not in {'codex', 'claude-agent-sdk'}:
        raise ValueError('The resolved eval harness must be codex or claude-agent-sdk.')
    return model, harness


def source_revision(root: Path) -> str:
    unchanged = subprocess.run(
        ['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--', 'agent', 'app', 'sandbox'],
        capture_output=True, timeout=10,
    )
    if unchanged.returncode:
        raise ValueError('Commit changes to agent/, app/ and sandbox/ before attributing a Lens evaluation.')
    result = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class MoyaiAgent:
    workspace: Path
    model: str
    model_base_url: str
    model_api_key: str = field(repr=False)
    trace_endpoint: str
    trace_api_key: str = field(repr=False)
    version: str
    harness: str = 'codex'
    timeout: int = 240
    max_iterations: int = 16

    @classmethod
    def from_env(cls, *, workspace: Path, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        required = ('LITELLM_API_BASE', 'LITELLM_API_KEY', 'AGENT_MODEL',
                    'LITELLM_TRACE_ENDPOINT', 'LITELLM_TRACE_API_KEY', 'LENS_VERSION')
        missing = [key for key in required if not env.get(key, '').strip()]
        if missing:
            raise ValueError('Set ' + ', '.join(missing) + ' before running Moyai evals.')
        version = source_revision(Path(__file__).resolve().parents[1])
        if env['LENS_VERSION'] != version:
            raise ValueError('LENS_VERSION must match the checked-out Moyai commit being tested.')
        model, harness = runtime_selection(env)
        timeout = int(env.get('MOYAI_EVAL_TIMEOUT', '240'))
        if not 10 <= timeout <= 900:
            raise ValueError('MOYAI_EVAL_TIMEOUT must be between 10 and 900 seconds.')
        return cls(Path(workspace).resolve(), model, env['LITELLM_API_BASE'],
                   env['LITELLM_API_KEY'], env['LITELLM_TRACE_ENDPOINT'],
                   env['LITELLM_TRACE_API_KEY'], version, harness, timeout)

    def run(self, *, input: str) -> AgentResult:
        if not isinstance(input, str) or not input.strip():
            raise ValueError('Moyai input must be a nonempty prompt string.')
        self.workspace.mkdir(parents=True, exist_ok=False)
        repo = Path(__file__).resolve().parents[1]
        # Model and trace credentials go through a private pipe, never argv or
        # an inherited agent environment. The worker uses the normal broker.
        with tempfile.TemporaryDirectory(prefix='moyai-eval-state-') as directory:
            state = Path(directory)
            reply = state / 'result.json'
            payload = {
                'input': input, 'workspace': str(self.workspace), 'state': str(state),
                'result': str(reply), 'model': self.model, 'model_base_url': self.model_base_url,
                'model_api_key': self.model_api_key, 'trace_endpoint': self.trace_endpoint,
                'trace_api_key': self.trace_api_key, 'version': self.version,
                'harness': self.harness, 'timeout': self.timeout, 'max_iterations': self.max_iterations,
            }
            keep = {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT', 'HOME'}
            environment = {key: value for key, value in os.environ.items() if key in keep}
            environment['PYTHONPATH'] = str(repo)
            with (state / 'worker.log').open('w') as log:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'evals.agent_worker'], cwd=repo, env=environment,
                    stdin=subprocess.PIPE, stdout=log, stderr=log, text=True,
                    start_new_session=True,
                )
                try:
                    process.communicate(json.dumps(payload), timeout=self.timeout + 90)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    raise AgentRunError('Moyai exceeded its execution and trace-delivery deadline.') from None
                except BaseException:
                    self._stop(process)
                    raise
            # Do not copy arbitrary SDK stderr (which may contain provider
            # payloads) into pytest logs or GitHub comments.
            if not reply.exists():
                raise AgentRunError(f'Moyai worker exited without a result (exit {process.returncode}).')
            result = json.loads(reply.read_text())
            if process.returncode or not result.get('completed'):
                raise AgentRunError(result.get('error', 'Moyai did not finish the task.'))
            return AgentResult(**result['result'])

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
