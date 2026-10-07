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
from .context_compaction import compaction_payload, compaction_result


NATIVE_ROUTES = {'/v1/messages', '/v1/responses'}


def authorized_payload(body, route, model, context):
    """Apply server policy without changing the provider's native schema."""
    if not isinstance(body, dict):
        raise HTTPException(422, 'Expected a JSON object.')
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
    value = payload.get(limit, 8192)
    if type(value) is not int or value < 1:
        raise HTTPException(422, 'Invalid output limit.')
    payload[limit] = min(value, 16000)
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
    failed = False

    def consume(self, value):
        if not isinstance(value, dict):
            return
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
        if 'input_tokens' in self.usage and any(key in self.usage for key in ('cache_read_input_tokens', 'cache_creation_input_tokens')):
            self.usage['prompt_tokens'] = sum(self.usage.get(key, 0) for key in
                ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        if 'prompt_tokens' in self.usage and 'completion_tokens' in self.usage:
            self.usage['total_tokens'] = self.usage['prompt_tokens'] + self.usage['completion_tokens']


class HarnessGateway:
    def __init__(self, *, settings, store, spend, checkpoints, require_run, read_body,
                 model_slots, memory, skills, tracing):
        self.settings, self.store, self.spend = settings, store, spend
        self.checkpoints, self.require_run, self.read_body = checkpoints, require_run, read_body
        self.model_slots, self.memory, self.skills = model_slots, memory, skills
        self.tracing = tracing

    async def forward(self, run_id, request, route):
        compact = route == '/context/compact'
        if route not in NATIVE_ROUTES and not compact:
            raise HTTPException(404, 'Unsupported model endpoint.')
        run = self.require_run(run_id, request)
        if self.model_slots.locked():
            raise HTTPException(429, 'Waiting for a model request slot.',
                                headers={'X-Moyai-Model-Queue': '1', 'Retry-After': '3'})
        await self.model_slots.acquire()
        client = None
        upstream = None
        request_id = None
        capture = None
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
                    # Only usage/status: native output can contain private reasoning.
                    self.tracing.model(run, request_id, started, [],
                        {'model': model, 'usage': capture.usage if capture else {}}, status)
                    await self.checkpoints.flush()
            finally:
                self.model_slots.release()

        try:
            try:
                model = self.settings.resolve_model(fallback=run['active_model'] or run['model'])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            body = await self.read_body(request, route)
            if compact:
                # Persistable summaries must never receive requester-private
                # memory, skills, attachments or the native SDK transcript.
                payload = compaction_payload(body, model)
            else:
                context = '\n\n'.join(x for x in [self.skills.context(run), self.memory.context(run)] if x)
                payload = authorized_payload(body, route, model, context)
                field = 'messages' if route == '/v1/messages' else 'input'
                items = payload[field]
                if isinstance(items, str):
                    items = [{'role': 'user', 'content': items}]
                payload[field] = self.store.attachments.with_images(run, items, protocol=route)
            admitted = self.store.execute(
                "UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 "
                "WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) "
                "AND status IN ('running','reconnecting','awaiting_approval')",
                (run_id, self.settings.max_agent_iterations, self.settings.max_agent_iterations * 3))
            if not admitted:
                raise HTTPException(429, 'This run reached its model request limit.')
            request_id = self.spend.begin(run, model)
            capture = NativeUsageCapture(bool(payload.get('stream')))
            headers = {'Authorization': 'Bearer ' + self.settings.litellm_api_key,
                       'x-litellm-call-id': request_id,
                       'x-litellm-spend-logs-metadata': json.dumps({'moyai_request_id': request_id})}
            if route == '/v1/messages':
                headers['anthropic-version'] = request.headers.get('anthropic-version', '2023-06-01')
                if request.headers.get('anthropic-beta'):
                    headers['anthropic-beta'] = request.headers['anthropic-beta']
            await self.checkpoints.flush()
            client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30))
            base = self.settings.litellm_api_base.rstrip('/')
            upstream_route = '/v1/chat/completions' if compact else route
            url = base + upstream_route.removeprefix('/v1') if base.endswith('/v1') else base + upstream_route
            upstream = await client.send(client.build_request('POST', url, json=payload, headers=headers), stream=True)
            self.spend.headers(request_id, upstream, capture.streaming)
            if upstream.status_code >= 400:
                raise HTTPException(502, f'Model gateway rejected the request ({upstream.status_code}).')
            if not capture.streaming:
                raw = bytearray()
                async for chunk in upstream.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise HTTPException(502, 'Model response exceeded the size limit.')
                capture.feed(bytes(raw))
                capture.finish()
                result = JSONResponse(compaction_result(raw)) if compact else Response(bytes(raw), media_type='application/json')
                status = 'completed' if capture.done and not capture.failed else 'failed'
                await finish()
                return result
        except httpx.HTTPError:
            await finish()
            raise HTTPException(502, 'Model gateway could not be reached.') from None
        except BaseException:
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
        return StreamingResponse(stream(), media_type='text/event-stream')
