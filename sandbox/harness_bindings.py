"""Runtime-specific launch and tool configuration. No agent loop or wire conversion."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class RuntimeBinding:
    sandbox_factory: object
    options_factory: object
    in_process: bool = False
    native_file_tools: bool = True

    def tools(self, cwd, config):
        if not self.in_process:
            return []
        try:
            from .harness_tools import tools_for
        except ImportError:
            from harness_tools import tools_for
        tools = tools_for(cwd, config)
        return tools[:2] if self.native_file_tools else tools

    @property
    def instructions(self):
        return ('Use workspace_tools and workspace_call for authorized app tools.' if self.in_process
                else 'Use the advertised Moyai MCP tools directly.')


def sandbox_class(native=None):
    from litellm.harness.sandbox.local import LocalSandbox
    if native is None:
        return LocalSandbox

    class ScopedSandbox(LocalSandbox):
        def child_env(self, env=None):
            merged = {**native.env, **(env or {})}
            # Persistence setup and runtime launches must use the same home.
            # Keep runtime-specific CODEX_HOME/XDG paths inside our owned root.
            merged.update({key: native.env[key] for key in ('HOME', 'TMPDIR', 'TMP', 'TEMP') if key in native.env})
            root = native.root.resolve()
            for key in ('HOME', 'TMPDIR', 'TMP', 'TEMP', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME',
                        'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
                if key in merged and not Path(merged[key]).resolve().is_relative_to(root):
                    raise ValueError('Native runtime storage escaped its session directory')
            return super().child_env(merged)

        async def tempdir(self):
            self._check_open()
            directory = Path(native.env['TMPDIR'])
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = str(Path(tempfile.mkdtemp(prefix='litellm-harness-', dir=directory)).resolve())
            self._tempdirs.append(path)
            return path

    return ScopedSandbox


def local_sandbox(cwd, config, native=None):
    return sandbox_class(native)(cwd)


def codex_sandbox(cwd, config, native=None):
    class CodexSandbox(sandbox_class(native)):
        is_container = True  # The enclosing Modal machine provides isolation.

        async def exec(self, cmd, *, env=None, cwd=None):
            cmd = list(cmd)
            if cmd[0] == 'codex':
                server = config['mcp_servers']['workspace']
                overrides = ['model_providers.litellm.request_max_retries=0',
                             'model_providers.litellm.stream_max_retries=0']
                for key in ('command', 'args'):
                    overrides.append('mcp_servers.moyai.' + key + '=' + json.dumps(server.get(key, [])))
                overrides.extend('mcp_servers.moyai.env.' + key + '=' + json.dumps(value)
                                 for key, value in server.get('env', {}).items())
                cmd[-1:-1] = [part for entry in overrides for part in ('-c', entry)]
            return await super().exec(cmd, env=env, cwd=cwd)
    return CodexSandbox(cwd)


def codex_options(config):
    from litellm import CodexOptions
    return CodexOptions()


def opencode_options(config):
    from litellm import OpenCodeOptions
    server = config['mcp_servers']['workspace']
    return OpenCodeOptions(config={'mcp': {'moyai': {'type': 'local',
        'command': [server['command'], *server.get('args', [])],
        'environment': server.get('env', {}), 'enabled': True}}})


def deepagents_options(config):
    from litellm import DeepAgentsOptions
    return DeepAgentsOptions()


def tool_loop_options(config):
    from litellm import ToolLoopOptions
    return ToolLoopOptions(completion_kwargs={'num_retries': 0})


RUNTIME_BINDINGS = {
    'codex': RuntimeBinding(codex_sandbox, codex_options),
    'opencode': RuntimeBinding(local_sandbox, opencode_options),
    'deepagents': RuntimeBinding(local_sandbox, deepagents_options, in_process=True),
    'tool-loop': RuntimeBinding(local_sandbox, tool_loop_options, in_process=True, native_file_tools=False),
}
