"""Best-effort OTLP export from the control plane; no gateway key enters a sandbox."""
import asyncio
from datetime import datetime
from functools import wraps
import hashlib
import logging
import time

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from sandbox.trace_content import trace_content

log = logging.getLogger(__name__)


def best_effort(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if not self.enabled:
            return
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            log.warning('Agent trace capture failed (%s)', type(exc).__name__)
    return wrapped


def identifier(value, size):
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], 'big') or 1


def context(trace_id, span_id):
    return SpanContext(trace_id, span_id, False, TraceFlags(TraceFlags.SAMPLED))


class AgentTracing:
    def __init__(self, store, settings, processor=None):
        self.store, self.settings = store, settings
        self.enabled = bool(settings.litellm_trace_endpoint and settings.litellm_trace_api_key)
        self.resource = Resource({'service.name': 'moyai-devin', 'deployment.environment.name': 'dev'})
        self.processor = processor
        if self.enabled and processor is None:
            self.processor = BatchSpanProcessor(OTLPSpanExporter(
                endpoint=settings.litellm_trace_endpoint,
                headers={'Authorization': 'Bearer ' + settings.litellm_trace_api_key},
                timeout=10,
            ), max_queue_size=2048, max_export_batch_size=64, schedule_delay_millis=5000)

    def identity(self, run, message_id):
        root = f"{run['id']}:{message_id or 0}"
        parent = None
        session = run['id']
        if run.get('parent_run_id'):
            groups = self.store.rows('SELECT * FROM agent_groups WHERE id=?', (run['agent_group_id'],))
            if groups and groups[0]['status'] in {'preparing', 'running'}:
                session = run['parent_run_id']
                root = f"{session}:{groups[0]['message_id']}"
                parent = identifier('agent:' + root, 8)
        return identifier('trace:' + root, 16), identifier(f"agent:{run['id']}:{message_id or 0}", 8), parent, session

    def content(self, value):
        return trace_content(value, secrets=(self.settings.litellm_api_key,
            self.settings.litellm_trace_api_key, self.settings.modal_token_secret))

    def emit(self, run, message_id, name, span_id, start, end, attrs, *, root=False, failed=False):
        trace_id, agent_id, parent_id, session = self.identity(run, message_id)
        parent_id = parent_id if root else agent_id
        attributes = {'session.id': session, 'agent.name': 'moyai-devin', 'moyai.run_id': run['id'],
                      'moyai.turn_id': str(message_id or 0),
                      'moyai.session_url': self.settings.public_url.rstrip('/') + '/#run=' + run['id'], **attrs}
        self.processor.on_end(ReadableSpan(
            name=name, context=context(trace_id, agent_id if root else identifier(span_id, 8)),
            parent=context(trace_id, parent_id) if parent_id else None,
            resource=self.resource, attributes=attributes, kind=SpanKind.INTERNAL,
            start_time=int(start), end_time=max(int(start), int(end)),
            status=Status(StatusCode.ERROR if failed else StatusCode.OK),
        ))

    @best_effort
    def finish_turn(self, run_id, message_id, output, status):
        run = self.store.run(run_id)
        if run['mode'] != 'modal':
            return
        messages = self.store.rows('SELECT * FROM messages WHERE id=? AND run_id=?', (message_id, run_id))
        message = messages[0] if messages else None
        started = (message.get('started_at') or message['created_at']) if message else run['created_at']
        inputs = [message['content'] if message else run['prompt']]
        if message:
            inputs += [m['content'] for m in self.store.rows(
                'SELECT content FROM messages WHERE run_id=? AND steering_parent_id=? ORDER BY id', (run_id, message_id))]
        self.emit(run, message_id, 'moyai-devin', '', datetime.fromisoformat(started).timestamp() * 1e9,
                  time.time_ns(), {'gen_ai.agent.name': 'moyai-devin', 'gen_ai.operation.name': 'invoke_agent',
                  'openinference.span.kind': 'AGENT', 'input.value': self.content('\n\n'.join(inputs)),
                  'output.value': self.content(output), 'moyai.status': status}, root=True,
                  failed=status not in {'completed', 'steered'})

    @best_effort
    def tool(self, run_id, data):
        run = self.store.run(run_id)
        if run['mode'] != 'modal' or not isinstance(data, dict):
            return
        now = time.time_ns()
        start, end = int(data['start_ns']), int(data['end_ns'])
        if not (0 < start <= end <= now + 60_000_000_000):
            return
        name = str(data['tool'])[:120]
        self.emit(run, run.get('active_message_id'), name, str(data['call_id']), start, end,
                  {'gen_ai.operation.name': 'execute_tool', 'openinference.span.kind': 'TOOL',
                   'tool.name': name, 'input.value': self.content(data.get('input')),
                   'output.value': self.content(data.get('output')),
                   'moyai.status': str(data.get('status', 'completed'))}, failed=data.get('status') == 'error')

    @best_effort
    def model(self, run, request_id, start, messages, response, status):
        # Keep images, system prompts, loaded skills and private reasoning out of traces.
        inputs = [{'role': 'user', 'content': m.get('content')}
                  for m in messages if isinstance(m, dict) and m.get('role') == 'user'][-5:]
        outputs = []
        for choice in response.get('choices', []):
            message = choice.get('message') if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            # Tool arguments/results have their own spans and privacy policy.
            outputs.append({'content': message.get('content'), 'tool_names': [
                call.get('function', {}).get('name') for call in message.get('tool_calls', []) or []
                if isinstance(call, dict)]})
        attrs = {'gen_ai.operation.name': 'chat', 'openinference.span.kind': 'LLM',
                 'gen_ai.request.model': run.get('active_model') or run.get('model', ''),
                 'llm.model_name': run.get('active_model') or run.get('model', ''),
                 'gen_ai.response.id': request_id, 'input.value': self.content(inputs),
                 'output.value': self.content(outputs), 'moyai.status': status}
        for source, target in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
            value = response.get('usage', {}).get(source)
            if isinstance(value, int):
                attrs['gen_ai.usage.' + target] = value
        self.emit(run, run.get('active_message_id'), 'chat ' + attrs['gen_ai.request.model'],
                  request_id, start, time.time_ns(), attrs, failed=status != 'completed')

    async def close(self):
        if self.processor:
            await asyncio.to_thread(self.processor.shutdown)
