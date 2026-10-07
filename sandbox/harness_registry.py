"""Single catalog shared by the API and sandbox. No optional runtime imports here.

Adding a harness does not require editing agent.py. Register a factory and its
capabilities here; implement Moyai's lifecycle contract in that adapter.
"""
from dataclasses import dataclass
from importlib import import_module
try:
    from .harness_agent import HarnessAgent
except ImportError:
    from harness_agent import HarnessAgent


@dataclass(frozen=True)
class HarnessDefinition:
    id: str
    name: str
    module: str
    factory: str
    live_steering: bool = False
    litellm_harness: str = ''
    runtime_binding: str = ''

    def create(self, **context) -> HarnessAgent:
        module = import_module('.' + self.module, __package__) if __package__ else import_module(self.module)
        return getattr(module, self.factory)(definition=self, **context)

    def public(self):
        return {'id': self.id, 'name': self.name,
                'engine': 'litellm' if self.litellm_harness else 'native',
                'live_steering': self.live_steering}


# Keep the persisted Claude ID stable for sessions created before this refactor.
HARNESSES = {
    'hermes': HarnessDefinition('hermes', 'Hermes', 'hermes_harness', 'HermesAgent', live_steering=True),
    'claude-agent-sdk': HarnessDefinition('claude-agent-sdk', 'Claude Agent SDK',
        'claude_harness', 'ClaudeAgent'),
    'codex': HarnessDefinition('codex', 'Codex', 'litellm_harness', 'LiteLLMAgent',
        litellm_harness='CODEX', runtime_binding='codex'),
    'opencode': HarnessDefinition('opencode', 'OpenCode', 'litellm_harness', 'LiteLLMAgent',
        litellm_harness='OPENCODE', runtime_binding='opencode'),
    'deepagents': HarnessDefinition('deepagents', 'Deep Agents', 'litellm_harness', 'LiteLLMAgent',
        litellm_harness='DEEPAGENTS', runtime_binding='deepagents'),
    'tool-loop': HarnessDefinition('tool-loop', 'Tool Loop', 'litellm_harness', 'LiteLLMAgent',
        litellm_harness='TOOL_LOOP', runtime_binding='tool-loop'),
}


def resolve(harness='hermes'):
    try:
        return HARNESSES[harness]
    except (KeyError, TypeError):
        raise ValueError('Unknown agent harness. Choose an enabled harness.') from None


def create_agent(harness='hermes', **context):
    return resolve(harness).create(**context)
