"""Authenticated pass-through for native harness APIs.

Moyai owns authorization, model pinning and accounting. LiteLLM AI Gateway owns
Messages/Responses protocol handling. No request/response format translation.
"""
import asyncio
import json
import time

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse, JSONResponse

from .spend import UsageCapture
from .native_trace import NativeModelContent
from .context_compaction import compaction_payload, private_compaction_payload, compaction_result, SummaryFailure, SUMMARY_ATTEMPTS
from .context_budget import ContextPressure, provider_context_rejection
from .broker_diagnostics import model_gateway_error, upstream_headers
from .model_selection import ASTRA_ULTRAFAST, gateway_payload


NATIVE_ROUTES = {'/v1/messages', '/v1/responses'}


def authorized_payload(body, route, model, context):
    """Apply server policy without changing the provider's native schema."""
    if not isinstance(body, dict):
        raise HTTPException(422, 'Expected a JSON object.')
    if model == ASTRA_ULTRAFAST and route != '/v1/responses':
        raise HTTPException(422, 'GPT-6 Astra Ultrafast requires the Responses API. Use Codex or regular GPT-6 Astra.')
    field = 'messages' if route == '/v1/messages' else 'input'
    if not isinstance(body.get(field), (list, str) if field == 'input' else list):
        raise HTTPException(422, f'{field} is required.')
    # Only provider API fields, never LiteLLM routing/key overrides from a sandbox.
    common = {'model', 'stream', 'tools', 'tool_choice', 'temperature', 'top_p'}
    native = ({'messages', 'system', 'max_tokens', 'stop_sequences', 'thinking', 'output_config', 'cache_control'}
              if route == '/v1/messages' else
              {'input', 'instructions', 'max_output_tokens', 'parallel_tool_calls', 'reasoning',
               'text', 'truncation', 'include', 'store'})
    payload = {k: v for k, v in body.items() if k in common | native}
    # Full input belongs to this run; provider-side IDs could reference another run.
    if body.get('previous_response_id'):
        raise HTTPException(422, 'Use full conversation input, not previous_response_id.')
    payload['model'] = model
    limit = 'max_tokens' if route == '/v1/messages' else 'max_output_tokens'
    if limit in payload and (type(payload[limit]) is not int or payload[limit] < 1):
        raise HTTPException(422, 'Invalid output limit.')
    if context:
        if route == '/v1/messages':
            existing = payload.get('system', [])
            if not isinstance(existing, (str, list)):
                raise HTTPException(422, 'system must be text or content blocks.')
            blocks = [{'type': 'text', 'text': existing}] if isinstance(existing, str) else existing
            payload['system'] = [{'type': 'text', 'text': context}, *blocks]
        else:
            if not isinstance(payload.get('instructions', ''), str):
                raise HTTPException(422, 'instructions must be text.')
            payload['instructions'] = context + '\n\n' + (payload.get('instructions') or '')
    if route == '/v1/responses':
        payload['store'] = False
    return payload


class NativeUsageCapture(UsageCapture):
    """Observe native usage fields; never rewrite the response bytes."""
    def __init__(self, streaming: bool, *, route: str, content: NativeModelContent | None = None) -> None:
        super().__init__(streaming)
        self.route = route
        self.failed = False
        self.content = content

    def consume(self, value):
        if not isinstance(value, dict):
            return
        if self.content is not None:
            self.content.consume(value)
        kind = value.get('type')
        if kind in {'error', 'response.failed', 'response.incomplete'} or value.get('status') in {'failed', 'incomplete'}:
            self.failed = True
        if kind == 'message_start':
            value = value.get('message', {})
        elif kind in {'response.completed', 'response.failed', 'response.incomplete'}:
            self.done = True
            value = value.get('response', {})
        elif kind == 'message_stop':
            self.done = True
        previous = self.usage.copy()
        super().consume(value)
        self.usage = {**previous, **self.usage}
        for native, canonical in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
            if native in self.usage:
                self.usage[canonical] = self.usage[native]
        # Messages reports uncached input separately; Responses includes cached
        # input in input_tokens already. Keep the native cache fields as well.
        if 'input_tokens' in self.usage and self.route == '/v1/messages':
            counts = [self.usage.get(key, 0) for key in
                      ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
            if all(type(value) is int and value >= 0 for value in counts):
                self.usage['prompt_tokens'] = sum(counts)
            else:
                self.usage.pop('prompt_tokens', None)
        counts = [self.usage.get(key) for key in ('prompt_tokens', 'completion_tokens')]
        if all(type(value) is int and value >= 0 for value in counts):
            self.usage['total_tokens'] = sum(counts)


class HarnessGateway:
    def __init__(self, *, settings, store, spend, checkpoints, require_run, read_body,
                 model_slots, memory, skills, tracing, context_budget):
        self.settings, self.store, self.spend = settings, store, spend
        self.checkpoints, self.require_run, self.read_body = checkpoints, require_run, read_body
        self.model_slots, self.memory, self.skills = model_slots, memory, skills
        self.tracing = tracing
        self.context_budget = context_budget
        self.native_sessions = None
        from .context_maintenance import ContextMaintenance
        self.maintenance = ContextMaintenance(self)
        from .live_context import LiveContext
        self.live_context = LiveContext(self)

    async def context_window(self, run_id, request):
        run = self.require_run(run_id, request)
        try:
            model = self.settings.resolve_model(fallback=run['active_model'] or run['model'])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        budget = await self.context_budget.measure({'model': model, 'messages': []})
        context = '\n\n'.join(x for x in [self.skills.context(run), self.memory.context(run)] if x)
        # The SDK cannot see broker-injected context. Reserve its byte bound as
        # well as room for a new tool result before the next native compact.
        window = max(1, budget.input_budget - len(context.encode()) - 4096)
        self.require_run(run_id, request)
        return {'model': model, 'input_budget': window, 'live_compaction': True}

    async def forward(self, run_id, request, route):
        if route != '/context/compact':
            return await self._forward_once(run_id, request, route)
        self.require_run(run_id, request)
        # Decrypt once while the transport envelope is fresh. A valid recovery
        # can outlive its five-minute envelope TTL; authorization is still
        # rechecked for every inference attempt below.
        body = await self.read_body(request, route)
        return await self.summarize(run_id, request, body)

    async def summarize_private(self, run, request, history: list[str], model: str, summary_bytes: int) -> str:
        response = await self.summarize(run['id'], request,
            {'history': history.copy(), 'summary_bytes': summary_bytes},
            expected_run=dict(run), private=True, expected_model=model)
        return json.loads(response.body)['summary']

    async def summarize(self, run_id, request, body, *, expected_run=None, private=False, expected_model=None):
        # Only tool-free maintenance is retried. Each attempt goes through run
        # authorization, admission, usage accounting and cleanup independently.
        for attempt in range(SUMMARY_ATTEMPTS):
            try:
                return await self._forward_once(run_id, request, '/context/compact', attempt, body,
                    expected_run=expected_run, private=private, expected_model=expected_model)
            except SummaryFailure as exc:
                retry = exc.retryable and attempt + 1 < SUMMARY_ATTEMPTS
                labels = {
                    'summary_too_large': 'The context summary exceeded its saved size budget.',
                    'incomplete_output': 'The model stopped before finishing the context summary.',
                    'upstream_unavailable': 'The summary service is temporarily unavailable.',
                    'gateway_unreachable': 'The summary service could not be reached.',
                }
                message = labels.get(exc.reason, 'The model did not return a usable context summary.')
                message += ' Retrying the summary; task actions will not be repeated.' if retry else ' Saved records are preserved.'
                if not private:
                    self.store.event(run_id, 'context', message, {
                        'reason': exc.reason, 'attempt': attempt + 1, 'request_id': exc.request_id,
                        'summary_bytes': exc.summary_bytes, 'retrying': retry,
                    })
                    await self.checkpoints.flush()
                if not retry:
                    raise
                if exc.transient:
                    await asyncio.sleep(attempt + 1)

    async def _forward_once(self, run_id, request, route, attempt=0, compaction_body=None, *,
                            expected_run=None, private=False, expected_model=None):
        compact = route == '/context/compact'
        if (route not in NATIVE_ROUTES and not compact) or (private and not compact):
            raise HTTPException(404, 'Unsupported model endpoint.')
        def current_run():
            current = self.require_run(run_id, request)
            if expected_run and any(current[key] != expected_run[key] for key in
                                    ('token_hash', 'active_message_id', 'active_user_id', 'active_model')):
                raise HTTPException(409, 'The originating response has ended.')
            if expected_model is not None:
                try:
                    selected = self.settings.resolve_model(fallback=current['active_model'] or current['model'])
                except ValueError:
                    raise HTTPException(409, 'The originating model is no longer available.') from None
                if selected != expected_model:
                    raise HTTPException(409, 'The originating model has changed.')
            return current
        run = current_run()
        # Internal private work already has a bounded snapshot. Let it queue
        # behind the foreground request that scheduled it, including capacity 1.
        if self.model_slots.locked() and not private:
            raise HTTPException(429, 'Waiting for a model request slot.',
                                headers={'X-Moyai-Model-Queue': '1', 'Retry-After': '3'})
        slot_acquired = False
        client = None
        upstream = None
        request_id = None
        gateway_id = ''
        capture = None
        content = None
        status = 'failed'
        started = time.time_ns()
        finished = False

        async def finish():
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                if upstream is not None:
                    await upstream.aclose()
                if client is not None:
                    await client.aclose()
                if request_id:
                    self.spend.finish(request_id, capture, status)
                    if status == 'completed' and capture and not compact:
                        self.context_budget.remember(payload, capture.usage, run_id + route)
                    response = capture.response if capture else {}
                    if content is not None:
                        response = {**response, 'choices': content.choices}
                    self.tracing.model(run, request_id, started, content.messages if content else [],
                        response, status, gateway_id=gateway_id)
                    await self.checkpoints.flush()
            finally:
                if slot_acquired:
                    self.model_slots.release()

        try:
            try:
                model = self.settings.resolve_model(fallback=run['active_model'] or run['model'])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if not compact:
                expected_run, expected_model = dict(run), model
            body = compaction_body if compact else await self.read_body(request, route)
            if compact:
                # Both summary paths omit injections and content tracing. Only
                # the public journal protocol can return a durable partial cursor.
                payload = (private_compaction_payload(body['history'], model, body['summary_bytes'], attempt)
                           if private else compaction_payload(body, model, attempt))
                partial_cursor = not private and type(body.get('cursor_protocol')) is int and body['cursor_protocol'] == 1
            else:
                if self.native_sessions is not None:
                    self.native_sessions.observe_scope(run)
                context = '\n\n'.join(x for x in [self.skills.context(run), self.memory.context(run)] if x)
                payload = authorized_payload(body, route, model, context)
                field = 'messages' if route == '/v1/messages' else 'input'
                items = payload[field]
                if isinstance(items, str):
                    items = [{'role': 'user', 'content': items}]
                payload[field] = await asyncio.to_thread(self.store.attachments.with_images, run, items, protocol=route)
            while True:
                try:
                    if compact:
                        checked_budget = await self.context_budget.check(payload)
                    else:
                        payload, checked_budget = await self.live_context.prepare(run, request, payload, route)
                    break
                except ContextPressure as exc:
                    if not compact:
                        self.store.event(run_id, 'context', 'Compacting before the next model request.', exc.budget)
                        await self.checkpoints.flush()
                        raise
                    if private:
                        raise SummaryFailure('summary_input_too_large', retryable=False) from None
                    if not partial_cursor:
                        raise HTTPException(422, 'Reconnect the runtime to compact smaller journal batches safely. Saved records are preserved.') from None
                    if len(body['entries']) == 1:
                        raise HTTPException(422, 'The model cannot fit the summary and one journal excerpt. '
                            'Saved records are preserved; choose a model with more input room.') from None
                    # Consume a prefix only. The returned cursor tells the store
                    # exactly which records were summarized; the rest stay pending.
                    body = {**body, 'entries': body['entries'][:max(1, len(body['entries']) // 2)]}
                    payload = compaction_payload(body, model, attempt)
            current_run()
            await self.model_slots.acquire()
            slot_acquired = True
            current_run()
            admitted = self.store.execute(
                "UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 "
                "WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) "
                "AND status IN ('running','reconnecting','awaiting_approval') AND token_hash=? AND active_message_id IS NOT DISTINCT FROM ? "
                "AND active_user_id IS NOT DISTINCT FROM ? AND active_model IS NOT DISTINCT FROM ? AND (coalesce(active_model,'')!='' OR model IS NOT DISTINCT FROM ?)",
                (run_id, self.settings.max_agent_iterations, self.settings.max_agent_iterations * 3,
                 run['token_hash'], run['active_message_id'], run['active_user_id'], run['active_model'], run['model']))
            if not admitted:
                raise HTTPException(429, 'This run reached its model request limit.')
            request_id = self.spend.begin(run, model)
            if self.tracing.enabled and not compact:
                # A private working summary is model input, never a new public
                # user message. Trace the original request's user text instead.
                content = NativeModelContent(body, route)
            capture = NativeUsageCapture(bool(payload.get('stream')), route=route, content=content)
            headers = {'Authorization': 'Bearer ' + self.settings.litellm_api_key,
                       'x-litellm-call-id': request_id,
                       'x-litellm-spend-logs-metadata': json.dumps({'moyai_request_id': request_id})}
            if route == '/v1/messages':
                headers['anthropic-version'] = request.headers.get('anthropic-version', '2023-06-01')
                if request.headers.get('anthropic-beta'):
                    headers['anthropic-beta'] = request.headers['anthropic-beta']
            await self.checkpoints.flush()
            current_run()
            client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30))
            base = self.settings.litellm_api_base.rstrip('/')
            upstream_route = '/v1/chat/completions' if compact else route
            url = base + upstream_route.removeprefix('/v1') if base.endswith('/v1') else base + upstream_route
            upstream = await client.send(client.build_request('POST', url,
                json=gateway_payload(payload, upstream_route), headers=headers), stream=True)
            gateway_id = self.spend.headers(request_id, upstream, capture.streaming)
            if upstream.status_code >= 400:
                raw_error = bytearray()
                async for chunk in upstream.aiter_bytes():
                    raw_error.extend(chunk[:8192 - len(raw_error)])
                    if len(raw_error) >= 8192:
                        break
                if provider_context_rejection(upstream.status_code, raw_error):
                    if compact:
                        if partial_cursor and len(body['entries']) > 1:
                            compaction_body['entries'] = body['entries'][:max(1, len(body['entries']) // 2)]
                            raise SummaryFailure('summary_input_too_large')
                        raise SummaryFailure('summary_input_too_large', retryable=False)
                    self.store.event(run_id, 'context', 'The provider requested further context reduction.', checked_budget.public())
                    raise ContextPressure(checked_budget.public())
                if compact:
                    transient = upstream.status_code in {408, 429, 500, 502, 503, 504}
                    raise SummaryFailure('upstream_unavailable' if transient else 'upstream_rejected',
                                         retryable=transient, transient=transient)
                raise model_gateway_error(request_id, upstream, raw_error)
            if not capture.streaming:
                raw = bytearray()
                async for chunk in upstream.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise HTTPException(502, 'Model response exceeded the size limit.')
                capture.feed(bytes(raw))
                capture.finish()
                result = (JSONResponse({**compaction_result(raw, body.get('summary_bytes', 12_000)),
                                       **({} if private else {'through_seq': body['entries'][-1]['seq']})})
                          if compact else Response(bytes(raw), media_type='application/json',
                                                   headers=upstream_headers(request_id, upstream)))
                status = 'completed' if capture.done and not capture.failed else 'failed'
                await finish()
                return result
        except httpx.HTTPError as exc:
            await finish()
            if compact:
                error = SummaryFailure('gateway_unreachable', transient=True)
                error.request_id = request_id
                raise error from None
            raise HTTPException(502, 'Model gateway could not be reached.',
                                headers=upstream_headers(request_id, error=exc)) from None
        except SummaryFailure as exc:
            exc.request_id = request_id
            await finish()
            raise
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                status = 'interrupted'
            await finish()
            raise

        async def stream():
            nonlocal status
            try:
                async for chunk in upstream.aiter_bytes():
                    current = self.store.run(run_id)
                    if current['status'] not in {'running', 'reconnecting', 'awaiting_approval'}:
                        status = 'interrupted'
                        break
                    capture.feed(chunk)
                    yield chunk
                capture.finish()
                if status != 'interrupted':
                    status = 'completed' if capture.done and not capture.failed else 'failed'
            except asyncio.CancelledError:
                status = 'interrupted'
                raise
            finally:
                await finish()
        return StreamingResponse(stream(), media_type='text/event-stream', headers=upstream_headers(request_id, upstream))
