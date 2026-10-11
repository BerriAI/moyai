"""Hermes-specific setup lives outside the harness-neutral session lifecycle."""
import os
import sys
from agent.harnesses.harness_agent import HarnessAgent
from sandbox.hermes_compat import prepare_hermes_imports


class HermesAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition=None):
        prepare_hermes_imports()
        from run_agent import AIAgent
        from tools.mcp_tool_discovery import discover_mcp_tools
        discover_mcp_tools(allowed_mcp_names=['workspace'])
        self.relay = relay
        self.agent = AIAgent(
            model=spec['model'], provider='custom', api_mode='chat_completions',
            base_url=relay.url + '/v1', api_key=os.environ['WORKSPACE_RUN_TOKEN'],
            enabled_toolsets=['terminal', 'file', 'mcp-workspace'],
            max_iterations=spec['max_iterations'] or sys.maxsize, run_budget_seconds=spec['timeout'],
            skip_memory=True, skip_background_review=True, quiet_mode=True, cwd=cwd,
            tool_start_callback=activity.start, tool_complete_callback=activity.complete,
            interim_assistant_callback=activity.commentary, step_callback=step,
            clarify_callback=lambda *a, **k: "Ask the user for the missing information in your final response, then wait for their next chat message.")
        # The gateway compacts model input concurrently. Keep the native
        # transcript intact instead of blocking this loop on another summary.
        window = relay.context_window() if hasattr(relay, 'context_window') else {}
        if window.get('live_compaction') is True:
            self.agent.compression_enabled = False

    def validate(self):
        from model_tools import get_tool_definitions
        tools = get_tool_definitions(enabled_toolsets=['mcp-workspace'], quiet_mode=True, skip_tool_search_assembly=True)
        if not any('browser_open' in tool['function']['name'] for tool in tools):
            if self.relay.startup_failure:
                raise self.relay.startup_failure
            raise RuntimeError('Workspace MCP tools were not loaded')
        if not {'tool_search', 'tool_describe', 'tool_call'}.issubset(self.agent.valid_tool_names):
            raise RuntimeError('Workspace tool discovery was not enabled')

    def __getattr__(self, name):
        return getattr(self.agent, name)

    def run_conversation(self, prompt, *, conversation_history, system_message):
        return self.agent.run_conversation(prompt, conversation_history=conversation_history, system_message=system_message)

    def interrupt(self):
        return self.agent.interrupt()

    def close(self):
        self.agent.close()
