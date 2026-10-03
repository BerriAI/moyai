"""Durable OTLP export from the control plane; no gateway key enters a sandbox."""
import asyncio
from datetime import datetime
from functools import wraps
import hashlib
import logging
import time

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from sandbox.trace_content import trace_content
from sandbox.memory_history import private_memory as is_memory_tool
from .trace_outbox import TraceOutbox

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
        with store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS trace_contexts (
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT,
                session_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                PRIMARY KEY(run_id,message_id))''')
        if self.enabled and processor is None:
            self.processor = TraceOutbox(store, settings)

    def identity(self, run, message_id, connection=None):
        if connection is None:
            with self.store.connect() as conn:
                return self.identity(run, message_id, conn)
        saved = connection.execute('SELECT * FROM trace_contexts WHERE run_id=? AND message_id=?',
                                   (run['id'], message_id or 0)).fetchone()
        if saved:
            return (int(saved['trace_id'], 16), int(saved['span_id'], 16),
                    int(saved['parent_id'], 16) if saved['parent_id'] else None,
                    saved['session_id'], saved['agent_name'])
        root = f"{run['id']}:{message_id or 0}"
        parent = None
        session = run['id']
        if run.get('parent_run_id'):
            groups = connection.execute('SELECT * FROM agent_groups WHERE id=?', (run['agent_group_id'],)).fetchall()
            if groups and groups[0]['status'] in {'preparing', 'running'}:
                session = run['parent_run_id']
                root = f"{session}:{groups[0]['message_id']}"
                parent = identifier('agent:' + root, 8)
        trace_id = identifier('trace:' + root, 16)
        span_id = identifier(f"agent:{run['id']}:{message_id or 0}", 8)
        name = ' '.join(self.content(run.get('agent_label') or 'moyai-devin').split())[:160] or 'moyai-devin'
        # Freeze attribution before a group finishes or a label changes. A late
        # journal replay must keep the original trace, parent and agent name.
        connection.execute('INSERT INTO trace_contexts VALUES(?,?,?,?,?,?,?)',
            (run['id'], message_id or 0, format(trace_id, '032x'), format(span_id, '016x'),
             format(parent, '016x') if parent else None, session, name))
        return trace_id, span_id, parent, session, name

    def content(self, value):
        return trace_content(value, secrets=(self.settings.litellm_api_key,
            self.settings.litellm_trace_api_key, self.settings.modal_token_secret))

    def emit(self, run, message_id, name, span_id, start, end, attrs, *, root=False, failed=False, connection=None):
        trace_id, agent_id, parent_id, session, agent_name = self.identity(run, message_id, connection)
        parent_id = parent_id if root else agent_id
        attributes = {'session.id': session, 'agent.name': agent_name, 'gen_ai.agent.name': agent_name,
                      'moyai.run_id': run['id'],
                      'moyai.turn_id': str(message_id or 0),
                      'moyai.session_url': self.settings.public_url.rstrip('/') + '/#run=' + run['id'], **attrs}
        span = ReadableSpan(
            name=agent_name if root else name, context=context(trace_id, agent_id if root else identifier(span_id, 8)),
            parent=context(trace_id, parent_id) if parent_id else None,
            resource=self.resource, attributes=attributes, kind=SpanKind.INTERNAL,
            start_time=int(start), end_time=max(int(start), int(end)),
            status=Status(StatusCode.ERROR if failed else StatusCode.OK),
        )
        if isinstance(self.processor, TraceOutbox):
            self.processor.enqueue(span, connection)
        else:
            self.processor.on_end(span)

    @best_effort
    def finish_turn(self, run_id, message_id, output, status, connection=None):
        def rows(sql, values):
            return [dict(r) for r in connection.execute(sql, values).fetchall()] if connection is not None else self.store.rows(sql, values)
        run = rows('SELECT * FROM runs WHERE id=?', (run_id,))[0]
        if run['mode'] != 'modal':
            return
        messages = rows('SELECT * FROM messages WHERE id=? AND run_id=?', (message_id, run_id))
        message = messages[0] if messages else None
        started = (message.get('started_at') or message['created_at']) if message else run['created_at']
        inputs = [message['content'] if message else run['prompt']]
        if message:
            inputs += [m['content'] for m in rows(
                'SELECT content FROM messages WHERE run_id=? AND steering_parent_id=? ORDER BY id', (run_id, message_id))]
        self.emit(run, message_id, 'moyai-devin', '', datetime.fromisoformat(started).timestamp() * 1e9,
                  time.time_ns(), {'gen_ai.operation.name': 'invoke_agent',
                  'openinference.span.kind': 'AGENT', 'input.value': self.content('\n\n'.join(inputs)),
                  'output.value': self.content(output), 'moyai.status': status}, root=True,
                  failed=status not in {'completed', 'steered'}, connection=connection)

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
        private_memory = is_memory_tool(name)
        self.emit(run, run.get('active_message_id'), name, str(data['call_id']), start, end,
                  {'gen_ai.operation.name': 'execute_tool', 'openinference.span.kind': 'TOOL',
                   'tool.name': name, 'input.value': self.content('[private tool payload omitted]' if private_memory else data.get('input')),
                   'output.value': self.content('[private tool payload omitted]' if private_memory else data.get('output')),
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

    @best_effort
    def feedback(self, answer, turn_id, user_id, rating, comment, source, revision, connection=None):
        """Attach a human rating to the answer's existing trace as a child span."""
        def one(sql, values):
            if connection is not None:
                row = connection.execute(sql, values).fetchone()
                return dict(row) if row else None
            rows = self.store.rows(sql, values)
            return rows[0] if rows else None
        run = one('SELECT * FROM runs WHERE id=?', (answer['run_id'],))
        # Only rate turns that were traced; never invent a trace for older answers.
        if not run or not one('SELECT 1 FROM trace_contexts WHERE run_id=? AND message_id=?', (run['id'], turn_id)):
            return
        verdict = {'up': 'helpful', 'down': 'not helpful', None: 'cleared'}[rating]
        summary = f'Human feedback: {verdict}' + (f'\nComment: {comment}' if comment else '')
        stamp = time.time_ns()
        self.emit(run, turn_id, 'human_feedback', f'feedback:{answer["id"]}:{user_id}:{revision}', stamp, stamp,
                  {'gen_ai.operation.name': 'feedback', 'openinference.span.kind': 'EVALUATOR',
                   'input.value': self.content(answer['content']), 'output.value': self.content(summary),
                   'feedback.rating': rating or 'cleared', 'feedback.score': {'up': 1, 'down': 0}.get(rating, -1),
                   'feedback.comment': self.content(comment), 'feedback.source': source,
                   'feedback.user_id': user_id, 'feedback.revision': revision,
                   'moyai.message_id': str(answer['id']), 'moyai.status': 'completed'},
                  connection=connection)

    def start(self):
        if isinstance(self.processor, TraceOutbox):
            self.processor.start()

    async def close(self):
        if isinstance(self.processor, TraceOutbox):
            await self.processor.close()
        elif self.processor:
            await asyncio.to_thread(self.processor.shutdown)
