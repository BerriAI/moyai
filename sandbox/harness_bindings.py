"""Runtime-specific launch and tool configuration. No agent loop or wire conversion."""
from dataclasses import dataclass
from pathlib import Path
import tempfile

_deepagents_live_compaction = False


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


def opencode_options(config):
    from litellm import OpenCodeOptions
    server = config['mcp_servers']['workspace']
    compaction = {'compaction': {'auto': False, 'prune': False}} if config.get('live_compaction') is True else {}
    return OpenCodeOptions(config={**compaction,
        'mcp': {'moyai': {'type': 'local',
        'command': [server['command'], *server.get('args', [])],
        'environment': server.get('env', {}), 'enabled': True}}})


def deepagents_options(config):
    from litellm import DeepAgentsOptions
    global _deepagents_live_compaction
    if _deepagents_live_compaction and config.get('live_compaction') is not True:
        # The public profile API cannot restore an excluded middleware. A
        # fresh process can safely select the older gateway's native policy.
        raise RuntimeError('Live compaction is no longer available. Restart the runtime to restore native compaction.')
    if config.get('live_compaction') is True:
        from deepagents import HarnessProfile, register_harness_profile
        # Applies to the main graph and its default subagents. The public profile
        # registry merges this exclusion with all other middleware and settings.
        register_harness_profile('litellm', HarnessProfile(
            excluded_middleware=frozenset({'SummarizationMiddleware'})))
        _deepagents_live_compaction = True
    return DeepAgentsOptions()


def tool_loop_options(config):
    from litellm import ToolLoopOptions
    return ToolLoopOptions(completion_kwargs={'num_retries': 0})


RUNTIME_BINDINGS = {
    'opencode': RuntimeBinding(local_sandbox, opencode_options),
    'deepagents': RuntimeBinding(local_sandbox, deepagents_options, in_process=True),
    'tool-loop': RuntimeBinding(local_sandbox, tool_loop_options, in_process=True, native_file_tools=False),
}
