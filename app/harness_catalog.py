"""Historical harness identifiers accepted by saved product configuration.

No executable factories remain; all current execution uses Pydantic AI.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessDefinition:
    id: str
    name: str
    module: str
    factory: str
    live_steering: bool = False
    litellm_harness: str = ''
    runtime_binding: str = ''
    durable_context: bool = False
    server_owned: bool = False


    def public(self):
        return {'id': self.id, 'name': self.name,
                'engine': 'temporal' if self.server_owned else 'litellm' if self.litellm_harness else 'native',
                'live_steering': self.live_steering}


# Keep the persisted Claude ID stable for sessions created before this refactor.
HARNESSES = {
    'deepagents-temporal': HarnessDefinition('deepagents-temporal', 'Deep Agents + Temporal', '', '', live_steering=True, server_owned=True),
    'hermes': HarnessDefinition('hermes', 'Hermes', 'hermes_harness', 'HermesAgent', live_steering=True),
    'claude-agent-sdk': HarnessDefinition('claude-agent-sdk', 'Claude Agent SDK',
        'claude_harness', 'ClaudeAgent', live_steering=True, durable_context=True),
    'codex': HarnessDefinition('codex', 'Codex', 'codex_harness', 'CodexAgent', live_steering=True, durable_context=True),
    'opencode': HarnessDefinition('opencode', 'OpenCode', 'litellm_harness', 'LiteLLMAgent',
        live_steering=True, litellm_harness='OPENCODE', runtime_binding='opencode', durable_context=True),
    'deepagents': HarnessDefinition('deepagents', 'Deep Agents', 'litellm_harness', 'LiteLLMAgent',
        live_steering=True, litellm_harness='DEEPAGENTS', runtime_binding='deepagents', durable_context=True),
    'tool-loop': HarnessDefinition('tool-loop', 'Tool Loop', 'litellm_harness', 'LiteLLMAgent',
        live_steering=True, litellm_harness='TOOL_LOOP', runtime_binding='tool-loop', durable_context=True),
    'pi': HarnessDefinition('pi', 'Pi', 'litellm_harness', 'LiteLLMAgent',
        live_steering=True, litellm_harness='PI', runtime_binding='pi', durable_context=True),
}


def resolve(harness='hermes'):
    try:
        return HARNESSES[harness]
    except (KeyError, TypeError):
        raise ValueError('Unknown agent harness. Choose an enabled harness.') from None
