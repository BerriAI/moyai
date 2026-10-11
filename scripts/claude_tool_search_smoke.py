"""Live SDK/relay/gateway discovery probe with only a read-only local tool.

Set GATEWAY_BASE_URL and GATEWAY_API_KEY, then run this module. No generated
shell/file/browser commands are permitted. The broker uses a disposable database
with no connected accounts. Inference is billed; model_list only reads that DB.
SMOKE_MODEL defaults to openai/gpt-6-astra. SMOKE_EAGER=1 measures the old behavior.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import uvicorn

from app.config import Settings
import app.harness_gateway as gateway
from app.main import create_app
from app.security import digest
from sandbox.broker_relay import BrokerRelay
from agent.harnesses.claude_harness import ClaudeAgent


def main():
    model = os.environ.get('SMOKE_MODEL', 'openai/gpt-6-astra')
    eager = os.environ.get('SMOKE_EAGER') == '1'
    requests, events = [], []
    original_payload = gateway.authorized_payload

    def observe(*args):
        payload = original_payload(*args)
        tools = payload.get('tools', [])
        item = {'tool_schema_bytes': len(json.dumps(tools).encode()),
                'mcp_definitions': sum(tool['name'].startswith('mcp__') for tool in tools),
                'tool_reference': '"tool_reference"' in json.dumps(payload.get('messages'))}
        requests.append(item)
        print(f'Model request {len(requests)}: {json.dumps(item)}', flush=True)
        return payload

    gateway.authorized_payload = observe
    with tempfile.TemporaryDirectory(prefix='moyai-tool-search-') as directory:
        root = Path(directory)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        settings = Settings(_env_file=None, data_dir=root / 'state', public_url=url,
            session_titles_enabled=False, temporal_enabled=False,
            litellm_api_base=os.environ['GATEWAY_BASE_URL'],
            litellm_api_key=os.environ.pop('GATEWAY_API_KEY'), agent_model=model,
            litellm_trace_api_key='', raindrop_write_key='', langfuse_secret_key='',
            langsmith_api_key='', braintrust_api_key='')
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                               log_level='error', access_log=False))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        if not server.started:
            raise RuntimeError('Local verification broker did not start')
        token = secrets.token_hex(24)
        run = app.state.store.create_run('Synthetic tool discovery verification', '', 'modal', [],
            model=model, user_id='fixture:tool-search', harness='claude-agent-sdk', chat_enabled=True)
        app.state.store.claim_message(run['id'])
        app.state.store.update_run(run['id'], status='running', token_hash=digest(token))
        relay = BrokerRelay(f'{url}/broker/{run["id"]}', token).start()
        previous_env = {key: os.environ.get(key) for key in ('WORKSPACE_RUN_TOKEN', 'CLAUDE_CONFIG_DIR')}
        os.environ.update(WORKSPACE_RUN_TOKEN=token, CLAUDE_CONFIG_DIR=str(root / 'sdk-config'))

        def started(call_id, name, arguments):
            events.append(name)
            print(f'Tool: {name}', flush=True)

        agent = ClaudeAgent(spec={'model': model, 'max_iterations': 5, 'timeout': 90}, relay=relay,
            config={'mcp_servers': {'workspace': {'command': sys.executable,
                'args': [str(Path(__file__).resolve().parents[1] / 'agent/tools/mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': token}}}},
            activity=SimpleNamespace(start=started, complete=lambda *args: None, commentary=lambda text: None),
            step=lambda: None, cwd=str(root), definition=None)
        options = agent.options
        # Never allow model-generated commands or writes on the developer host.
        def safe_options(system):
            configured = options(system)
            return replace(configured, tools=[] if eager else ['ToolSearch'],
                allowed_tools=['mcp__moyai__model_list', *([] if eager else ['ToolSearch'])],
                env={**configured.env, 'ENABLE_TOOL_SEARCH': 'false' if eager else 'true'})
        agent.options = safe_options
        try:
            print(f'{model} | {"eager baseline" if eager else "lazy discovery"} | real gateway', flush=True)
            prompt = ('Call mcp__moyai__model_list once, then reply exactly tool-search-live-ok.' if eager else
                'Use ToolSearch with query select:mcp__moyai__model_list and max_results 1. '
                'Then call mcp__moyai__model_list once and reply exactly tool-search-live-ok.')
            result = agent.run_conversation(prompt, conversation_history=[],
                system_message='Synthetic transport check. Only tool discovery and the read-only model_list are permitted.')
            assert result['completed'], result['final_response']
            assert events.count('mcp__moyai__model_list') == 1, events
            assert 'tool-search-live-ok' in result['final_response'], result['final_response']
            if not eager:
                assert events.index('ToolSearch') < events.index('mcp__moyai__model_list')
                assert requests[0]['mcp_definitions'] == 0, requests[0]
                assert any(item['tool_reference'] for item in requests), requests
            report = {'model': model, 'eager': eager, 'completed': True, 'tools': events, 'requests': requests}
            if path := os.environ.get('SMOKE_REPORT'):
                Path(path).write_text(json.dumps(report, indent=2))
            print('PASS: read-only broker call completed exactly once.', flush=True)
        finally:
            agent.close()
            relay.close()
            server.should_exit = True
            worker.join(10)
            gateway.authorized_payload = original_payload
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == '__main__':
    main()
