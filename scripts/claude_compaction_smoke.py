"""Verify native SDK compaction through the real broker and gateway.

GATEWAY_BASE_URL / GATEWAY_API_KEY select the gateway. Provider inference is
billed. Uses a disposable database, synthetic files and only the Read tool.
The smaller verification window changes compaction timing, never max_tokens.
Run: python -m scripts.claude_compaction_smoke --output report.json
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

import httpx
import uvicorn
from claude_agent_sdk import HookMatcher

from app.config import Settings
from app.main import create_app
from app.security import digest
from sandbox.broker_relay import BrokerRelay
from agent.harnesses.claude_harness import ClaudeAgent
from agent.context_store import ContextStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='openai/gpt-6-astra')
    parser.add_argument('--steps', type=int, default=16)
    parser.add_argument('--window', type=int, default=70000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    requests, calls, counters, comments = [], [], [], []
    original_send = httpx.AsyncClient.send

    async def observed_send(client, request, **kwargs):
        if request.url.path.endswith('/utils/token_counter'):
            counters.append(1)
        if request.url.path.endswith('/v1/messages'):
            body = json.loads(request.content)
            item = {'model': body['model'],
                    'max_tokens': body.get('max_tokens'), 'request_bytes': len(request.content)}
            requests.append(item)
            print(f"Inference {len(requests)}: output allowance {item['max_tokens']}", flush=True)
        return await original_send(client, request, **kwargs)

    with tempfile.TemporaryDirectory(prefix='moyai-native-compact-') as directory:
        root = Path(directory)
        paths = [root / f'receipt-{secrets.token_hex(6)}.txt' for _ in range(args.steps)]
        marker = 'violet-' + secrets.token_hex(4)
        for index, path in enumerate(paths):
            next_step = f'Read this next file: {paths[index + 1]}' if index + 1 < len(paths) else 'All steps finished. Reply native-compaction-ok followed by the original codeword.'
            path.write_text(f'Receipt {index + 1}. ' + (f'Original codeword: {marker}.\n' if index == 0 else '\n')
                + '\n'.join(f'Log item {i}: step {index + 1} completed synthetic diagnostic data.' for i in range(600))
                + '\n' + next_step + '\n')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        settings = Settings(_env_file=None, data_dir=root / 'state', public_url=url,
            session_titles_enabled=False, temporal_enabled=False,
            litellm_api_base=os.environ['GATEWAY_BASE_URL'], litellm_api_key=os.environ.pop('GATEWAY_API_KEY'),
            agent_model=args.model, litellm_trace_api_key='', raindrop_write_key='', langfuse_secret_key='',
            langsmith_api_key='', braintrust_api_key='')
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        assert server.started, 'Local verification broker did not start'
        token = secrets.token_hex(24)
        run = app.state.store.create_run('Native compaction verification', '', 'modal', [], model=args.model,
            user_id='fixture:native-compaction', harness='claude-agent-sdk', chat_enabled=True)
        app.state.store.claim_message(run['id'])
        app.state.store.update_run(run['id'], status='running', token_hash=digest(token))
        relay = BrokerRelay(f'{url}/broker/{run["id"]}', token).start()
        policy = relay.context_window()
        print(f"{args.model} | broker budget {policy['input_budget']} | verification window {args.window}", flush=True)
        assert 0 < args.window <= policy['input_budget']
        relay.context_window = lambda: {**policy, 'input_budget': args.window}
        fallbacks = []
        def unexpected_fallback(*a, **k):
            fallbacks.append(1)
            raise AssertionError('This probe must complete with native compaction alone')
        relay.compact = unexpected_fallback
        saved_env = {key: os.environ.get(key) for key in ('WORKSPACE_RUN_TOKEN', 'CLAUDE_CONFIG_DIR')}
        os.environ.update(WORKSPACE_RUN_TOKEN=token, CLAUDE_CONFIG_DIR=str(root / 'sdk-config'))
        store = ContextStore(root / 'context.db', run['id'])
        store.initialize([])
        def commentary(text):
            if text.startswith('The agent compacted'):
                comments.append(text)
                print(f'Native compaction {len(comments)} completed; tool receipts preserved.', flush=True)
        def started(call_id, name, arguments):
            calls.append(arguments['file_path'])
            print(f'Read receipt {len(calls)}/{args.steps} exactly once.', flush=True)
        agent = ClaudeAgent(spec={'model': args.model, 'max_iterations': args.steps * 3, 'timeout': 600},
            relay=relay, config={'mcp_servers': {'workspace': {}}},
            activity=SimpleNamespace(start=started, complete=lambda *a: None, commentary=commentary),
            step=lambda: None, cwd=str(root), definition=None, context_store=store)
        async def restricted_hook(event, call_id, context):
            if event['hook_event_name'] == 'PreToolUse':
                expected = str(paths[len(calls)]) if len(calls) < len(paths) else None
                if event['tool_name'] != 'Read' or event['tool_input'].get('file_path') != expected:
                    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                        'permissionDecision': 'deny', 'permissionDecisionReason': 'Only the next synthetic receipt may be read once.'}}
            return await agent.tool_hook(event, call_id, context)
        options = agent.options
        def safe_options(system):
            configured = options(system)
            return replace(configured, tools=['Read'], allowed_tools=['Read'], mcp_servers={},
                hooks={name: [HookMatcher(hooks=[restricted_hook])] for name in
                       ('PreToolUse', 'PostToolUse', 'PostToolUseFailure')})
        agent.options = safe_options
        httpx.AsyncClient.send = observed_send
        started_at = time.monotonic()
        try:
            result = agent.run_conversation(
                f'Read {paths[0]} completely. Follow the next-file pointer at the end of each receipt, '
                'one Read per response, until all receipts are complete. Never reread a file. '
                'Preserve the original codeword across context summaries. At the end return native-compaction-ok and that codeword.',
                conversation_history=[], system_message='Read-only synthetic verification. Only these local receipt files are permitted.')
            report = {'model': args.model, 'completed': result['completed'], 'native_compactions': agent.native_compactions,
                'steps': len(calls), 'unique_steps': len(set(calls)), 'marker_preserved': marker in result['final_response'],
                'fallbacks': len(fallbacks), 'remote_token_counts': len(counters), 'requests': requests,
                'verification_window': args.window, 'broker_input_budget': policy['input_budget'],
                'elapsed_seconds': round(time.monotonic() - started_at, 2),
                'journal_records': store.db.execute('SELECT count(*) FROM journal').fetchone()[0],
                'accounted_requests': len(app.state.store.rows('SELECT * FROM model_requests'))}
            if args.output: args.output.write_text(json.dumps(report, indent=2) + '\n')
            assert result['completed'], result['final_response']
            assert agent.native_compactions >= 2, report
            assert calls == [str(path) for path in paths], report
            assert marker in result['final_response'], report
            assert not fallbacks and not counters, report
            assert not store.pending and not agent.journal.pending
            print('PASS: repeated native compaction, original codeword retained, no duplicate tools, zero remote token counts.', flush=True)
            print(json.dumps({key: value for key, value in report.items() if key != 'requests'}), flush=True)
        finally:
            httpx.AsyncClient.send = original_send
            agent.close()
            store.close()
            relay.close()
            server.should_exit = True
            thread.join(10)
            for key, value in saved_env.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value


if __name__ == '__main__':
    main()
