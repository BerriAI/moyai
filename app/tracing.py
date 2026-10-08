"""Durable OTLP export from the control plane; no gateway key enters a sandbox."""
import asyncio
from datetime import datetime
from functools import wraps
import hashlib
import json
import logging
import time

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from sandbox.trace_content import private_tool, trace_content
from .slack_mentions import MENTION
from .trace_outbox import RaindropEventOutbox, TraceOutbox
from .user_preferences import UserPreferences

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
    def __init__(self, store, settings, processor=None, *, preferences=None):
        self.store, self.settings = store, settings
        self.preferences = preferences or UserPreferences(store, None, None)
        destinations = settings.trace_destinations()
        self.enabled = bool(destinations)
        self.resource = Resource({'service.name': 'moyai',
                                  'deployment.environment.name': settings.trace_environment})
        self.processor = processor
        with store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS trace_contexts (
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT,
                session_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                PRIMARY KEY(run_id,message_id))''')
        self.outboxes = [TraceOutbox(store, *destination) for destination in destinations] if processor is None else []
        raindrop = next((d for d in destinations if d[0] == 'trace_outbox_raindrop'), None)
        self.events = (RaindropEventOutbox(store, raindrop[1].removesuffix('/traces') + '/events/track', raindrop[2])
                       if raindrop and processor is None else None)

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
        inherited_trace = None
        session = run['id']
        if run.get('parent_run_id'):
            groups = connection.execute('SELECT * FROM agent_groups WHERE id=?', (run['agent_group_id'],)).fetchall()
            if groups and groups[0]['status'] in {'preparing', 'running'}:
                parent_run = connection.execute('SELECT * FROM runs WHERE id=?', (run['parent_run_id'],)).fetchone()
                if parent_run:
                    self.store.root_id_in(connection, run['id'])
                    inherited_trace, parent, _, session, _ = self.identity(dict(parent_run), groups[0]['message_id'], connection)
        trace_id = inherited_trace or identifier('trace:' + root, 16)
        span_id = identifier(f"agent:{run['id']}:{message_id or 0}", 8)
        name = ' '.join(self.content(run.get('agent_label') or 'moyai').split())[:160] or 'moyai'
        # Freeze attribution before a group finishes or a label changes. A late
        # journal replay must keep the original trace, parent and agent name.
        connection.execute('INSERT INTO trace_contexts VALUES(?,?,?,?,?,?,?)',
            (run['id'], message_id or 0, format(trace_id, '032x'), format(span_id, '016x'),
             format(parent, '016x') if parent else None, session, name))
        return trace_id, span_id, parent, session, name

    def content(self, value):
        return trace_content(value, secrets=(self.settings.litellm_api_key, self.settings.litellm_trace_api_key,
            self.settings.raindrop_write_key, self.settings.langfuse_secret_key,
            self.settings.langfuse_public_key, self.settings.langsmith_api_key,
            self.settings.braintrust_api_key, self.settings.modal_token_secret))

    def model_content(self, messages):
        """Bound whole JSON messages without cutting JSON syntax or private data.

        Raindrop accepts legacy content; LangSmith's GenAI mapper requires
        parts. Keep both representations of the same sanitized text.
        """
        cleaned = [{'role': m['role'], 'content': self.content(m['content']) if m.get('content') is not None else None,
                    **({'tool_names': [self.content(n)[:120] for n in m['tool_names'][:100]]}
                       if 'tool_names' in m else {})} for m in messages]
        while True:
            legacy = json.dumps(cleaned, ensure_ascii=False)
            genai = json.dumps([{**m, 'parts': [{'type': 'text', 'content': m['content']}] if m['content'] is not None else []}
                                for m in cleaned],
                               ensure_ascii=False)
            if max(len(legacy), len(genai)) <= 16000:
                return legacy, genai
            # Shrink values, never the encoded JSON. Output choices are bounded
            # below, and tool names are capped, so content reduction terminates.
            for message in cleaned:
                if isinstance(message['content'], str):
                    message['content'] = message['content'][:len(message['content']) // 2] + ' [truncated]'
                if 'tool_names' in message:
                    message['tool_names'] = message['tool_names'][:min(10, len(message['tool_names']) // 2)]

    def user_identity(self, run, message_id, connection=None):
        """Export SSO email, without changing internal ownership or account IDs."""
        if connection is None:
            with self.store.connect() as conn:
                return self.user_identity(run, message_id, conn)
        # Late completion/replay must use this turn's author, not the next
        # active participant in a shared session.
        message = connection.execute(
            "SELECT user_id FROM messages WHERE run_id=? AND id=? AND role='user'",
            (run['id'], message_id)).fetchone() if message_id else None
        user = message['user_id'] if message else run.get('active_user_id') or run.get('owner_id')
        if not user:
            return None
        account = connection.execute('''SELECT u.kind,u.email,linked.email AS sso_email
            FROM users u LEFT JOIN users linked ON linked.id=u.linked_user_id AND linked.kind='google'
            WHERE u.id=?''', (user,)).fetchone()
        if account:
            email = account['email'] if account['kind'] == 'google' else account['sso_email']
            if email:
                return email
        # Unlinked Slack and shared-password accounts have no SSO email.
        # Preserve stable attribution rather than inventing an email or
        # leaking raw provider IDs. Never substitute the session owner's email.
        return format(identifier('user:' + user, 16), '032x')

    def emit(self, run, message_id, name, span_id, start, end, attrs, *, root=False, failed=False, connection=None):
        trace_id, agent_id, parent_id, session, agent_name = self.identity(run, message_id, connection)
        parent_id = parent_id if root else agent_id
        attributes = {'session.id': session, 'agent.name': agent_name, 'gen_ai.agent.name': agent_name,
                      'moyai.run_id': run['id'],
                      'moyai.turn_id': str(message_id or 0),
                      'moyai.environment': self.settings.trace_environment,
                      'moyai.session_url': self.settings.public_url.rstrip('/') + '/#run=' + run['id'],
                      # Raindrop groups spans into conversations and turns by these keys.
                      'traceloop.association.properties.convo_id': session,
                      'traceloop.association.properties.event_id': format(trace_id, '032x'), **attrs}
        user = self.user_identity(run, message_id, connection)
        if user:
            attributes['user.id'] = user
            attributes['traceloop.association.properties.user_id'] = attributes['user.id']
        kind = attrs.get('openinference.span.kind')
        if kind == 'TOOL':
            # Lens renders tool content from GenAI attributes; OpenInference
            # input/output remains available to the other backends.
            attributes['gen_ai.tool.call.arguments'] = attrs.get('input.value', '')
            attributes['gen_ai.tool.call.result'] = attrs.get('output.value', '')
        elif kind == 'AGENT':
            _, attributes['gen_ai.input.messages'] = self.model_content([
                {'role': 'user', 'content': attrs.get('input.value', '')}])
            _, attributes['gen_ai.output.messages'] = self.model_content([
                {'role': 'assistant', 'content': attrs.get('output.value', '')}])
        attributes.update({
            'traceloop.span.kind': {'AGENT': 'workflow', 'LLM': 'task', 'TOOL': 'tool'}.get(kind, 'task'),
            'traceloop.entity.name': agent_name if root else name,
            'traceloop.entity.input': attrs.get('input.value', ''),
            'traceloop.entity.output': attrs.get('output.value', ''),
            'traceloop.association.properties.event': 'moyai',
        })
        if self.settings.langfuse_public_key and self.settings.langfuse_secret_key:
            # Propagate filterable context to every observation (Langfuse v4).
            attributes.update({
                'langfuse.observation.type': {'AGENT': 'agent', 'LLM': 'generation', 'TOOL': 'tool'}.get(
                    attrs.get('openinference.span.kind'), 'span'),
                'langfuse.trace.name': 'moyai',
                'langfuse.trace.tags': ['moyai'],
                'langfuse.environment': self.settings.langfuse_tracing_environment,
                'langfuse.observation.metadata.run_id': run['id'],
                'langfuse.observation.metadata.turn_id': str(message_id or 0),
                'langfuse.observation.metadata.session_url': attributes['moyai.session_url'],
            })
        if self.settings.langsmith_api_key:
            attributes.update({
                'langsmith.span.kind': {'AGENT': 'chain', 'LLM': 'llm', 'TOOL': 'tool'}.get(kind, 'chain'),
                'langsmith.span.tags': 'moyai,' + self.settings.trace_environment,
                'langsmith.metadata.thread_id': session,
                'langsmith.metadata.session_id': session,
                'langsmith.metadata.run_id': run['id'],
                'langsmith.metadata.turn_id': str(message_id or 0),
                'langsmith.metadata.session_url': attributes['moyai.session_url'],
                'langsmith.metadata.environment': self.settings.trace_environment,
            })
        if self.settings.braintrust_api_key:
            attributes.update({
                'braintrust.span_attributes.type': {'AGENT': 'task', 'LLM': 'llm', 'TOOL': 'tool'}.get(kind, 'task'),
                'braintrust.metadata.session_id': session,
                'braintrust.metadata.run_id': run['id'],
                'braintrust.metadata.turn_id': str(message_id or 0),
                'braintrust.metadata.session_url': attributes['moyai.session_url'],
                'braintrust.metadata.environment': self.settings.trace_environment,
                'braintrust.tags': ['moyai', self.settings.trace_environment],
            })
        error = self.content(attrs.get('output.value') or attrs.get('moyai.status') or 'Operation failed') if failed else None
        span = ReadableSpan(
            name=agent_name if root else name, context=context(trace_id, agent_id if root else identifier(span_id, 8)),
            parent=context(trace_id, parent_id) if parent_id else None,
            resource=self.resource, attributes=attributes, kind=SpanKind.INTERNAL,
            start_time=int(start), end_time=max(int(start), int(end)),
            status=Status(StatusCode.ERROR, error) if failed else Status(StatusCode.OK),
            # LangSmith maps exception events to errors; status alone is lost.
            events=[Event('exception', {'exception.message': error}, timestamp=int(end))] if failed else [],
        )
        if self.processor is not None:
            self.processor.on_end(span)
        for outbox in self.outboxes:
            outbox.enqueue(span, connection)
        if root and self.events:
            self.events.enqueue(span, connection)

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
        self.emit(run, message_id, 'moyai', '', datetime.fromisoformat(started).timestamp() * 1e9,
                  time.time_ns(), {'gen_ai.operation.name': 'invoke_agent',
                  'openinference.span.kind': 'AGENT', 'input.value': self.content('\n\n'.join(inputs)),
                  'output.value': self.content(output), 'moyai.status': status,
                  **self.slack_source(run_id, rows)}, root=True,
                  failed=status not in {'completed', 'steered'}, connection=connection)

    def slack_source(self, run_id, rows):
        """Link the turn to its Slack thread with the Lens `agent.source.*` contract."""
        events = rows('SELECT thread_ts,context_json FROM slack_events WHERE run_id=?', (run_id,))
        if not events:
            return {}
        context = json.loads(events[0]['context_json'] or '{}')
        url = context.get('permalink', '')
        if not url:
            return {}
        root = next((m['text'] for m in context.get('messages', []) if m.get('ts') == events[0]['thread_ts']), '')
        mentioned = sorted(set(MENTION.findall(root)))
        names = {row['user_id']: row['name'] for row in rows(
            f"SELECT user_id,name FROM slack_mention_names WHERE name!='' AND user_id IN ({','.join('?' * len(mentioned))})",
            tuple(mentioned))}
        title = MENTION.sub(lambda match: '@' + names.get(match[1], 'someone'), root)
        return {'agent.source.type': 'slack', 'agent.source.url': url,
                'agent.source.title': self.content(' '.join(title.split()))[:200]}

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
        omit = private_tool(name) and self.preferences.for_run(run)['omit_private_tool_payloads']
        self.emit(run, run.get('active_message_id'), name, str(data['call_id']), start, end,
                  {'gen_ai.operation.name': 'execute_tool', 'openinference.span.kind': 'TOOL',
                   'gen_ai.tool.name': name, 'gen_ai.tool.call.id': str(data['call_id']),
                   'tool.name': name, 'input.value': self.content('[private tool payload omitted]' if omit else data.get('input')),
                   'output.value': self.content('[private tool payload omitted]' if omit else data.get('output')),
                   'moyai.status': str(data.get('status', 'completed'))}, failed=data.get('status') == 'error')

    @best_effort
    def model(self, run, request_id, start, messages, response, status, *, gateway_id: str = ''):
        # Keep images, system prompts, loaded skills and private reasoning out of traces.
        inputs = [{'role': 'user', 'content': m.get('content')}
                  for m in messages if isinstance(m, dict) and m.get('role') == 'user'][-5:]
        outputs = []
        for choice in response.get('choices', [])[:5]:
            message = choice.get('message') if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            # Tool arguments/results have their own spans and privacy policy.
            outputs.append({'role': 'assistant', 'content': message.get('content'), 'tool_names': [
                call.get('function', {}).get('name') for call in message.get('tool_calls', []) or []
                if isinstance(call, dict)]})
        input_value, input_messages = self.model_content(inputs)
        output_value, output_messages = self.model_content(outputs)
        attrs = {'gen_ai.operation.name': 'chat', 'openinference.span.kind': 'LLM',
                 'gen_ai.request.model': run.get('active_model') or run.get('model', ''),
                 'llm.model_name': run.get('active_model') or run.get('model', ''),
                 'input.value': input_value,
                 'output.value': output_value, 'moyai.status': status}
        model = attrs['gen_ai.request.model']
        provider = model.partition('/')[0] if '/' in model else 'openai'
        attrs.update({'gen_ai.system': provider, 'gen_ai.provider.name': provider,
                      'gen_ai.input.messages': input_messages,
                      'gen_ai.output.messages': output_messages,
                      'input.mime_type': 'application/json', 'output.mime_type': 'application/json'})
        for source, value in [('gen_ai.response.id', response.get('id')),
                              ('gen_ai.response.model', response.get('model')), ('litellm.call_id', gateway_id)]:
            if isinstance(value, str) and value:
                attrs[source] = self.content(value)[:200]
        usage = response.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        for source, target in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens'),
                               ('cache_read_input_tokens', 'cache_read.input_tokens'),
                               ('cache_creation_input_tokens', 'cache_write.input_tokens')]:
            value = usage.get(source)
            if type(value) is int and value >= 0:
                attrs['gen_ai.usage.' + target] = value
        reasoning = usage.get('reasoning_tokens')
        output = attrs.get('gen_ai.usage.output_tokens')
        if type(reasoning) is int and type(output) is int and 0 <= reasoning <= output:
            attrs['gen_ai.usage.reasoning.output_tokens'] = reasoning
        tier = response.get('service_tier') or usage.get('service_tier')
        if provider in {'openai', 'anthropic'} and isinstance(tier, str) and tier:
            attrs[provider + '.response.service_tier'] = self.content(tier)[:100]
        # OTEL standardizes cache totals; Anthropic's TTL price split remains
        # a provider-specific extension until semantic conventions cover it.
        creation = usage.get('cache_creation')
        if provider == 'anthropic' and isinstance(creation, dict):
            for key in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'):
                value = creation.get(key)
                if type(value) is int and value >= 0:
                    attrs['anthropic.usage.cache_creation.' + key] = value
        if all('gen_ai.usage.' + key in attrs for key in ('input_tokens', 'output_tokens')):
            attrs['gen_ai.usage.total_tokens'] = attrs['gen_ai.usage.input_tokens'] + attrs['gen_ai.usage.output_tokens']
        # Let Langfuse map GenAI usage to its canonical input/output pricing
        # keys; overriding usage_details with *_tokens breaks inferred costs.
        self.emit(run, run.get('active_message_id'), 'chat ' + attrs['gen_ai.request.model'],
                  request_id, start, time.time_ns(), attrs, failed=status != 'completed')

    def start(self):
        for outbox in self.exporters():
            outbox.start()

    def exporters(self):
        return [*self.outboxes, *([self.events] if self.events else [])]

    async def close(self):
        await asyncio.gather(*(outbox.close() for outbox in self.exporters()))
        if self.processor:
            await asyncio.to_thread(self.processor.shutdown)
