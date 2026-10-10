"""Opt-in real SDK/model smoke test. Uses an isolated local DB, never production sessions.
Run with GATEWAY_API_KEY supplied securely and GATEWAY_BASE_URL configured.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

import uvicorn

from app.config import Settings
from app.main import create_app
from app.security import digest
from agent.activity import ActivityReporter
from sandbox.broker_relay import BrokerRelay
from agent.context_store import ContextStore
from agent.harnesses.harness_registry import create_agent


def main():
    harness = os.environ.get('SMOKE_HARNESS', 'claude-agent-sdk')
    root = Path('/workspace/harness-smoke') / harness
    root.mkdir(exist_ok=True, parents=True)
    proof = 'sdk-proof-' + secrets.token_hex(4) + '.py'
    settings = Settings(_env_file=None, data_dir=root / 'state', public_url='http://127.0.0.1:8791',
                        litellm_api_key=os.environ.pop('GATEWAY_API_KEY'),
                        litellm_api_base=os.environ['GATEWAY_BASE_URL'], temporal_enabled=False,
                        session_titles_enabled=False,
                        agent_model=os.environ.get('SMOKE_MODEL', 'anthropic/claude-sonnet-4-5'))
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8791, log_level='error', access_log=False))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.1)
    if not server.started:
        raise RuntimeError('Local broker did not start')
    token = secrets.token_hex(24)
    os.environ['WORKSPACE_RUN_TOKEN'] = token
    # Advertise the read-only credential catalog against this isolated DB. No
    # Temporal workflow is submitted and no production credential store is used.
    settings.temporal_enabled = True
    run = app.state.store.create_run('SDK live smoke', '', 'modal', [], model=settings.agent_model,
                                    user_id='fixture:harness-smoke', harness=harness, chat_enabled=True)
    app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest(token))
    relay = BrokerRelay(f'http://127.0.0.1:8791/broker/{run["id"]}', token).start()
    events = []
    def emit(kind, message, data=None):
        events.append({'kind': kind, 'message': message, 'data': data})
        if kind == 'tool':
            print(f"Tool {data['phase']}: {data['tool']}", flush=True)
    activity = ActivityReporter(emit)
    context = ContextStore(root / 'context.sqlite3', run['id'])
    context.initialize([])
    agent = create_agent(harness, spec={'model': settings.agent_model, 'max_iterations': 12, 'timeout': 150}, relay=relay,
                        config={'mcp_servers': {'workspace': {'command': sys.executable,
                            'args': [str(Path(__file__).resolve().parents[1] / 'agent/tools/mcp_bridge.py')],
                            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': token}}}},
                        activity=activity, step=lambda: None, cwd=str(root), context_store=context)
    try:
        print(f'Live gateway: {harness} | {settings.agent_model}', flush=True)
        result = agent.run_conversation(f'Use your file or shell tools to create {proof} containing Python code print("harness-live-ok"). Read it back and execute it with python to verify it prints harness-live-ok. Also call the workspace credentials_list tool to verify the connection (discover via workspace_tools/workspace_call if needed); do not request access or reveal credential values. Reply with the execution result.',
                                       conversation_history=[], system_message=f'Work only in {root}. Use tools to do the requested work.')
        assert result['completed'], {k: v for k, v in result.items() if k != 'messages'}
        import subprocess
        executed = subprocess.run([sys.executable, str(root / proof)], capture_output=True, text=True, timeout=10)
        assert executed.returncode == 0 and executed.stdout.strip() == 'harness-live-ok'
        assert any(('credentials_list' in e.get('data', {}).get('tool', '') or e.get('data', {}).get('tool') == 'workspace_call') and e['data']['phase'] == 'completed' for e in events), {'answer': result['final_response'], 'events': events}
        first_calls = app.state.store.run(run['id'])['model_calls']
        assert not context.pending
        context.close()
        context = ContextStore(root / 'context.sqlite3', run['id'])
        agent.context_store = context
        print('PASS: file created, executed and broker tool completed. Restore saved context.', flush=True)
        followup = agent.run_conversation('What file did you just create? Read it again and return its content. Do not write anything.',
                                         conversation_history=[], system_message=f'Work only in {root}.')
        assert followup['completed'], followup['final_response']
        assert not any('SAVED CONVERSATION REFERENCE:' in str(m.get('content')) for m in followup['messages'])
        report = {'harness': harness, 'model': settings.agent_model, 'completed': result['completed'], 'answer': result['final_response'],
                  'followup_completed': followup['completed'], 'followup_answer': followup['final_response'],
                  'cache_usage': app.state.store.rows('SELECT cache_read_input_tokens,cache_creation_input_tokens FROM model_requests WHERE run_id=? ORDER BY created_at', (run['id'],)),
                  'first_model_calls': first_calls, 'total_model_calls': app.state.store.run(run['id'])['model_calls'],
                  'tool_events': events, 'file_content': (root / proof).read_text(), 'execution_stdout': executed.stdout}
        (root / 'verification.json').write_text(json.dumps(report, indent=2))
        print(f"PASS: cold context restore completed; {report['total_model_calls']} model requests; output: {executed.stdout.strip()}", flush=True)
    finally:
        agent.close()
        context.close()
        relay.close()
        server.should_exit = True
        worker.join(10)


if __name__ == '__main__':
    main()
