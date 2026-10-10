"""Private worker for evals.agent; credentials are accepted only on stdin."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time

import uvicorn

from app.config import Settings
from app.main import create_app
from app.security import digest
from agent.agent import run as run_agent
from sandbox.broker_relay import BrokerRelay

from .agent import AgentRunError, source_revision


class SpanReceipts:
    def __init__(self):
        self.spans = {}

    def on_end(self, span):
        self.spans[format(span.context.span_id, '016x')] = span.attributes.get('openinference.span.kind')

    def shutdown(self):
        pass


def execute(payload):
    root = Path(__file__).resolve().parents[1]
    if source_revision(root) != payload['version']:
        raise AgentRunError('Moyai source changed after evaluation started.')
    state, workspace = Path(payload['state']), Path(payload['workspace'])
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    broker_url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    settings = Settings(
        _env_file=None, data_dir=state / 'broker', public_url=broker_url,
        litellm_api_base=payload['model_base_url'], litellm_api_key=payload['model_api_key'],
        agent_model=payload['model'], temporal_enabled=False,
        session_titles_enabled=False, memory_review_enabled=False,
        litellm_spend_recovery_enabled=False,
        trace_environment='lens-eval', moyai_build_sha=payload['version'],
        litellm_trace_endpoint=payload['trace_endpoint'], litellm_trace_api_key=payload['trace_api_key'],
    )
    app = create_app(settings)
    captured = SpanReceipts()
    app.state.tracing.processor = captured
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    relay = None
    run = message = None
    finished = False
    tool_calls = 0
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise AgentRunError('The isolated Moyai broker did not become ready.')
            time.sleep(.05)
        token = secrets.token_hex(24)
        os.environ['WORKSPACE_RUN_TOKEN'] = token
        store, tracing = app.state.store, app.state.tracing
        run = store.create_run(payload['input'], '', 'modal', [], model=payload['model'],
                               harness=payload['harness'], user_id='fixture:lens-eval', chat_enabled=True)
        message = store.claim_message(run['id'])
        if message is None:
            raise AgentRunError('Moyai could not claim the evaluation input.')
        store.update_run(run['id'], status='running', token_hash=digest(token))
        # Some older commits create trace identity on the first child span.
        # Capture it now so every model/tool/root event uses the same identity.
        trace_id = format(tracing.identity(store.run(run['id']), message['id'])[0], '032x')
        relay = BrokerRelay(f'{broker_url}/broker/{run["id"]}', token).start()

        def emit(kind, _message, data=None, **_extra):
            nonlocal tool_calls
            if kind == 'trace':
                tracing.tool(run['id'], data)
                tool_calls += 1

        outcome = run_agent(
            {'run_id': run['id'], 'prompt': payload['input'], 'harness': payload['harness'],
             'model': payload['model'], 'max_iterations': payload['max_iterations'],
             'timeout': payload['timeout'], 'transport_recovery_seconds': 30,
             'chat_enabled': True, 'tracing_enabled': True, 'omit_private_tool_payloads': True},
            workspace=workspace, session=state, emit=emit,
            relay=relay, config={'mcp_servers': {'workspace': {
                'command': sys.executable, 'args': [str(root / 'agent' / 'tools' / 'mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': token},
            }}},
        )
        result = outcome['result']
        if (outcome['exit_code'] or not result.get('completed') or result.get('failed')
                or result.get('interrupted') or result.get('partial') or outcome['context_pending']):
            raise AgentRunError('Moyai stopped before completing the task or settling all tool calls.')
        raw_output, output = result.get('final_response'), outcome['summary']
        if (not isinstance(raw_output, str) or not raw_output.strip()
                or not isinstance(output, str) or not output.strip()):
            raise AgentRunError('Moyai completed without an output.')
        model_calls = store.run(run['id'])['model_calls']
        if model_calls < 1 or tool_calls < 1:
            raise AgentRunError('A coding regression must execute a real model request and a tool.')
        store.finish_message(run['id'], message['id'], output)
        finished = True
        store.update_run(run['id'], status='completed', summary=output)
        wait_for_traces(store, trace_id, captured.spans)
        return {'output': output, 'trace_id': trace_id, 'session_id': run['id'],
                'agent_version': payload['version'], 'model_calls': model_calls, 'tool_calls': tool_calls}
    finally:
        def shutdown_server():
            server.should_exit = True
            thread.join(timeout=15)

        # Run every cleanup even if saving a failed turn or one close fails.
        with ExitStack() as cleanup:
            cleanup.callback(listener.close)
            cleanup.callback(shutdown_server)
            if relay:
                cleanup.callback(relay.close)
            if run and message and not finished:
                app.state.store.finish_message(run['id'], message['id'], 'Evaluation execution failed.', 'failed')
                app.state.store.update_run(run['id'], status='failed', error='Evaluation execution failed.')


def wait_for_traces(store, trace_id, spans, *, timeout=45):
    if not {'AGENT', 'LLM', 'TOOL'} <= set(spans.values()):
        raise AgentRunError('Moyai did not produce all required agent, model and tool spans.')
    deadline = time.monotonic() + timeout
    while True:
        receipts = store.rows('SELECT span_id,delivered_at,last_error FROM trace_outbox WHERE trace_id=?', (trace_id,))
        if set(spans) <= {row['span_id'] for row in receipts if row['delivered_at'] is not None}:
            return
        if time.monotonic() >= deadline:
            raise AgentRunError('Lens did not acknowledge all agent, model and tool spans before the deadline.')
        time.sleep(.1)


def main():
    payload = json.load(sys.stdin)
    try:
        result = {'completed': True, 'result': execute(payload)}
    except Exception as exc:
        # Known errors are written by us; unexpected exception messages can
        # contain credentials or provider bodies and must stay out of artifacts.
        message = str(exc) if isinstance(exc, AgentRunError) else f'Moyai execution failed ({type(exc).__name__}).'
        Path(payload['result']).write_text(json.dumps({'completed': False, 'error': message}))
        return 1
    Path(payload['result']).write_text(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
