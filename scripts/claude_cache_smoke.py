"""Live, tool-free SDK/gateway cache probe; safe to run without a cloud sandbox.

GATEWAY_BASE_URL and GATEWAY_API_KEY are required. SMOKE_MODEL defaults to
anthropic/claude-sonnet-4-5. Makes three billed inference calls with synthetic
content; fails unless the provider reports a cache read. No external writes.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import threading
import time

import uvicorn

from app.config import Settings
from app.main import create_app
from app.security import digest
from agent.activity import ActivityReporter
from sandbox.broker_relay import BrokerRelay
from agent.harnesses.harness_registry import create_agent


def main():
    root = Path(os.environ.get('SMOKE_ROOT', 'work/claude-cache-smoke')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, data_dir=root / secrets.token_hex(6),
                        public_url='http://127.0.0.1:8792', session_titles_enabled=False,
                        litellm_api_base=os.environ['GATEWAY_BASE_URL'],
                        litellm_api_key=os.environ.pop('GATEWAY_API_KEY'),
                        agent_model=os.environ.get('SMOKE_MODEL', 'anthropic/claude-sonnet-4-5'))
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8792, log_level='error', access_log=False))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.1)
    if not server.started: raise RuntimeError('Local probe broker did not start')
    token = secrets.token_hex(24)
    os.environ['WORKSPACE_RUN_TOKEN'] = token
    run = app.state.store.create_run('Synthetic SDK cache probe', '', 'modal', [],
        model=settings.agent_model, harness='claude-agent-sdk', chat_enabled=True)
    app.state.store.update_run(run['id'], status='running', token_hash=digest(token))
    relay = BrokerRelay(f'http://127.0.0.1:8792/broker/{run["id"]}', token).start()
    events = []
    activity = ActivityReporter(lambda kind, message, data=None: events.append(kind))
    agent = create_agent('claude-agent-sdk', spec={'model': settings.agent_model, 'max_iterations': 2, 'timeout': 90},
        relay=relay, config={'mcp_servers': {'workspace': {'command': 'unused'}}},
        activity=activity, step=lambda: None, cwd=str(root))
    options = agent.options
    # Exercise the final adapter/SDK/relay/accounting path, with every native and
    # connected tool removed. Full coding-tool verification belongs in a sandbox.
    agent.options = lambda system: replace(options(system), tools=[], allowed_tools=[], mcp_servers={})
    system = 'This is a synthetic prompt-caching verification. Answer only cache-probe-ok.\n' + '\n'.join(
        f'Reference entry {n}: stable synthetic content for validating prompt caching through the authenticated model gateway.'
        for n in range(500))
    try:
        print('Runtime: Claude Agent SDK 0.2.163 | tool execution: disabled', flush=True)
        # Keep the system prefix stable but make every request unique, including
        # repeated probe runs, so a gateway response cache cannot mimic a hit.
        probe_id = secrets.token_hex(8)
        for index in range(3):
            result = agent.run_conversation(f'Reply exactly cache-probe-ok. Probe {probe_id}, request {index + 1}.', conversation_history=[], system_message=system)
            if not result['completed']:
                raise RuntimeError(result['final_response'])
            rows = app.state.store.rows('SELECT cache_creation_input_tokens,cache_read_input_tokens,prompt_tokens,completion_tokens FROM model_requests WHERE run_id=? ORDER BY created_at', (run['id'],))
            print(f'Request {index + 1}: {json.dumps(rows[-1])}', flush=True)
        assert any((row['cache_read_input_tokens'] or 0) > 0 for row in rows), 'Provider did not report a cache hit'
        assert not events, 'Tool-free probe emitted unexpected activity'
        report = {'runtime': 'claude-agent-sdk', 'sdk_version': '0.2.163', 'model': settings.agent_model,
                  'tool_execution': 'disabled', 'cache_hit_verified': True, 'requests': rows}
        (root / 'verification.json').write_text(json.dumps(report, indent=2))
        print('PASS: native SDK inference and provider-confirmed prompt-cache reuse.', flush=True)
    finally:
        agent.close()
        relay.close()
        server.should_exit = True
        worker.join(10)


if __name__ == '__main__':
    main()
