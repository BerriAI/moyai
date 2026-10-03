"""Deterministic probe run by test_tool_discovery in the pinned Hermes runtime.

Uses only the local fixture broker; no model or external provider is called.
"""
import json

from run_agent import AIAgent
from model_tools import get_tool_definitions, handle_function_call
from tools.mcp_tool_discovery import discover_mcp_tools
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.tool_search import ToolSearchConfig, assemble_tool_defs
from agent.tool_executor import _unwrap_tool_search_call


def main():
    scope = ['terminal', 'file', 'mcp-workspace']
    discovered = discover_mcp_tools(allowed_mcp_names=['workspace'])
    assert discovered
    agent = AIAgent(model='test-model', provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='fixture-only', enabled_toolsets=scope,
        quiet_mode=True, skip_memory=True, skip_background_review=True,
        skip_context_files=True, save_trajectories=False)
    try:
        raw = get_tool_definitions(enabled_toolsets=scope, quiet_mode=True,
                                   skip_tool_search_assembly=True)
        visible = {tool['function']['name'] for tool in agent.tools}
        assert {'terminal', 'read_file', 'write_file', 'tool_search', 'tool_describe', 'tool_call'} <= visible
        assert not any(name.startswith('mcp__workspace__') for name in visible)
        before = json.dumps(assemble_tool_defs(raw, config=ToolSearchConfig.from_raw(
            {'enabled': 'off'})).tool_defs, separators=(',', ':'))
        after = json.dumps(agent.tools, separators=(',', ':'))
        assert len(after) < len(before) * 0.8, (len(before), len(after))

        def dispatch(name, arguments, toolsets=scope):
            return json.loads(handle_function_call(name, arguments, enabled_toolsets=toolsets))

        searched = dispatch('tool_search', {'queries': ['linear search', 'github repositories', 'slack search']})
        targets = ['mcp__workspace__linear_search', 'mcp__workspace__github_repositories', 'mcp__workspace__slack_search']
        for result, expected in zip(searched['results'], targets):
            assert expected in result['matches'], searched
        assert all('parameters' not in record for record in searched['tools'].values())
        descriptions = dispatch('tool_describe', {'names': targets})['tools']
        assert set(descriptions) == set(targets)
        assert 'query' in descriptions[targets[0]]['parameters']['properties']
        # PR packaging must still use the sandbox-facing schema, not the
        # broker's raw files payload or an unrestricted GitHub API wrapper.
        pr = dispatch('tool_describe', {'names': ['mcp__workspace__github_create_pull_request']})['tools']
        fields = pr['mcp__workspace__github_create_pull_request']['parameters']['properties']
        assert {'directory', 'title', 'body', 'request_key'} <= fields.keys()
        assert 'files' not in fields

        for name in targets:
            args = {} if name.endswith('repositories') else {'query': 'fixture'}
            call = {'calls': [{'name': name, 'arguments': args}]}
            # Hermes must expose the real name/args to activity, tracing, and
            # guardrails before dispatching through the existing MCP handler.
            assert _unwrap_tool_search_call(agent, 'tool_call', call) == (name, args, None)
            assert 'fixture-ok' in json.dumps(dispatch('tool_call', call))

        restricted = ['terminal', 'file']
        hidden = dispatch('tool_search', {'queries': ['linear search']}, restricted)
        assert hidden['results'][0]['matches'] == []
        denied = dispatch('tool_call', {'calls': [{'name': targets[0], 'arguments': {'query': 'fixture'}}]}, restricted)
        assert 'not available' in json.dumps(denied)
        invalid = dispatch('tool_call', {'calls': [{'name': targets[0], 'arguments': {'unexpected': 'bad'}}]})
        assert 'error' in invalid
        # Revocation is applied by the fixture immediately before this request
        # reaches the real broker, after the tool's schema was discovered.
        revoked = dispatch('tool_call', {'calls': [{'name': targets[2], 'arguments': {'query': 'revoked'}}]})
        assert 'HTTP 403' in json.dumps(revoked)
        denied_write = dispatch('tool_call', {'calls': [{'name': 'mcp__workspace__linear_comment',
            'arguments': {'issue_id': 'LIT-123', 'body': 'fixture write; deny it'}}]})
        assert 'denied' in json.dumps(denied_write).lower()
        print('TOOL_SEARCH_PROOF ' + json.dumps({'before_schema_chars': len(before),
            'after_schema_chars': len(after), 'raw_tool_count': len(raw),
            'visible_tool_count': len(agent.tools), 'verified_services': ['linear', 'github', 'slack']}))
    finally:
        agent.close()


if __name__ == '__main__':
    try:
        main()
    finally:
        shutdown_mcp_servers()
