"""Check final model requests before inference, independently of generation caps."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import time

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelContextLimits(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    context_window: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    default_output_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_default(self):
        if self.default_output_tokens and self.default_output_tokens > self.max_output_tokens:
            raise ValueError('default_output_tokens exceeds max_output_tokens')
        return self


class ContextPressure(HTTPException):
    def __init__(self, budget):
        self.budget = budget
        super().__init__(409, {
            'code': 'context_compaction_required',
            'message': 'The next request needs context compaction before inference.',
            **budget,
        }, headers={'X-Moyai-Context': 'compact'})


def provider_context_rejection(status, raw):
    """Only a structured, definite pre-generation context rejection can recover."""
    if status not in {400, 413}:
        return False
    try:
        error = json.loads(raw)['error']
        return error.get('code') in {'context_length_exceeded', 'prompt_too_long', 'input_too_long'} or error.get('type') == 'context_length_exceeded'
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def positive(value):
    return value if type(value) is int and value > 0 else None


def counting_input(payload, *, anthropic_images=False):
    """Count a conservative serialization, including all tools and opaque items.

    Keep images as images for the gateway counter. Never count a URL as its
    image's token cost, fetch external URLs here, or translate the inference wire.
    """
    images = []

    def visit(value, content_block=False):
        if isinstance(value, list):
            return [visit(item, content_block) for item in value]
        if not isinstance(value, dict):
            return value
        kind = value.get('type')
        if content_block and kind in {'image_url', 'input_image', 'image'}:
            if kind == 'image':
                source = value.get('source', {})
                url = (f"data:{source.get('media_type')};base64,{source.get('data')}"
                       if source.get('type') == 'base64' else source.get('url'))
                item = {'url': url}
            else:
                item = value.get('image_url')
                if isinstance(item, str):
                    item = {'url': item, 'detail': value.get('detail', 'auto')}
            if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                raise HTTPException(422, 'Cannot count the image in this request.')
            if anthropic_images:
                url = item['url']
                if url.startswith('data:'):
                    header, separator, data = url.partition(';base64,')
                    if not separator or not header[5:] or not data:
                        raise HTTPException(422, 'Cannot count the image in this request.')
                    source = {'type': 'base64', 'media_type': header[5:], 'data': data}
                else:
                    source = {'type': 'url', 'url': url}
                images.append({'type': 'image', 'source': source})
            else:
                images.append({'type': 'image_url', 'image_url': item})
            return '[image counted separately]'
        if content_block and kind in {'input_audio', 'audio', 'input_file', 'file', 'video', 'video_url'}:
            raise HTTPException(422, 'This media type needs a supported token counter before inference.')
        return {key: visit(item, key in {'content', 'input'}) for key, item in value.items()}

    fields = {key: payload[key] for key in ('messages', 'input', 'system', 'instructions',
        'tools', 'tool_choice', 'response_format', 'text') if key in payload}
    try:
        serialized = json.dumps(visit(fields), ensure_ascii=False, separators=(',', ':'))
        byte_count = len(serialized.encode())
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise HTTPException(422, 'The request cannot be represented for token counting.') from None
    content = [{'type': 'text', 'text': serialized}, *images]
    return [{'role': 'user', 'content': content}], byte_count, bool(images)


@dataclass(frozen=True)
class Budget:
    input_tokens: int
    input_budget: int
    output_tokens: int
    context_window: int
    method: str

    def public(self):
        return vars(self)


class ContextBudget:
    def __init__(self, settings):
        self.settings = settings
        self.cache = {}
        self.usage = {}
        self.lock = asyncio.Lock()

    @property
    def base(self):
        return self.settings.litellm_api_base.rstrip('/').removesuffix('/v1')

    @property
    def headers(self):
        return {'Authorization': 'Bearer ' + self.settings.litellm_api_key}

    async def limits(self, model):
        configured = self.settings.model_context_limits.get(model)
        if configured is not None:
            return ModelContextLimits.model_validate(configured)
        key = (self.base, hashlib.sha256(self.settings.litellm_api_key.encode()).digest(), model)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(self.base + '/model/info', headers=self.headers,
                                                params={'include_team_models': 'true'})
                    response.raise_for_status()
                    rows = [row for row in response.json()['data'] if row.get('model_name') == model]
                candidates = []
                for row in rows:
                    info = row.get('model_info', {})
                    # max_tokens is ambiguous (often an output limit). Never use
                    # it as a context window. Absent an explicit shared window,
                    # reserving output inside max_input_tokens is conservative.
                    input_limit = positive(info.get('max_input_tokens'))
                    output_limit = positive(info.get('max_output_tokens'))
                    window = positive(info.get('context_window')) or input_limit
                    candidates.append(ModelContextLimits(context_window=window,
                        max_input_tokens=input_limit, max_output_tokens=output_limit))
                if not candidates:
                    raise ValueError('No limits for selected alias')
                # A routed alias must fit every candidate deployment.
                limits = ModelContextLimits(**{field: min(getattr(c, field) for c in candidates)
                    for field in ('context_window', 'max_input_tokens', 'max_output_tokens')})
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(503, 'The selected model has no verified context limits. '
                    'Configure MODEL_CONTEXT_LIMITS or allow gateway model metadata access.') from None
            if len(self.cache) >= 128:
                self.cache.clear()
            self.cache[key] = (time.monotonic() + 300, limits)
            return limits

    @staticmethod
    def fingerprint(payload):
        # Cache directives move between turns without changing model input.
        # Keep only hashes and sizes, never a second copy of private context.
        def clean(value, wire=True):
            if isinstance(value, list):
                return [clean(item, wire) for item in value]
            if isinstance(value, dict):
                return {key: clean(item, wire and key in {'content', 'system', 'tools'})
                        for key, item in value.items() if not (wire and key == 'cache_control')}
            return value
        def part(value):
            raw = json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            return hashlib.sha256(raw).digest(), len(raw)
        field = 'messages' if 'messages' in payload else 'input'
        items = payload.get(field, [])
        static = {key: payload[key] for key in ('model', 'system', 'instructions', 'tools',
                  'tool_choice', 'response_format', 'text') if key in payload}
        return part(static), tuple(part(item) for item in (items if isinstance(items, list) else [items]))

    def usage_key(self, scope, model):
        return self.base, hashlib.sha256(self.settings.litellm_api_key.encode()).digest(), scope, model

    def remember(self, payload, usage, scope):
        tokens = positive(usage.get('prompt_tokens'))
        if not scope or tokens is None or payload.get('truncation') == 'auto':
            return
        if len(self.usage) >= 128:
            self.usage.clear()
        self.usage[self.usage_key(scope, payload['model'])] = (time.monotonic() + 300, self.fingerprint(payload), tokens)

    async def count(self, payload, *, input_budget=0, scope=''):
        messages, byte_count, images = counting_input(payload)
        tokens, method = byte_count, 'utf8_upper_estimate'
        anchor = self.usage.get(self.usage_key(scope, payload['model'])) if scope else None
        if anchor and anchor[0] > time.monotonic():
            static, parts = self.fingerprint(payload)
            old_static, old_parts = anchor[1]
            if static == old_static and parts[:len(old_parts)] == old_parts:
                delta = parts[len(old_parts):]
                # Newly added images still require the provider counter. An
                # unchanged image prefix is already included in response usage.
                field = 'messages' if 'messages' in payload else 'input'
                items = payload.get(field, [])
                if isinstance(items, list):
                    images = counting_input({field: items[len(old_parts):]})[2]
                tokens = anchor[2] + sum(size + 16 for _, size in delta)
                method = 'usage_plus_utf8_delta'
        # Ordinary turns have no counting round trip. Exact counting is a
        # fallback for uncertain/large inputs, not the native compaction loop.
        if not images and tokens < input_budget * .8:
            return tokens, method
        conservative = tokens
        reasons = []
        for anthropic_images in ((False, True) if images else (False,)):
            # LiteLLM's Anthropic counter expects native image/source blocks.
            # Negotiate by verified counter provenance, not gateway alias names
            # or the harness protocol. Never change the actual inference wire.
            counted = counting_input(payload, anthropic_images=True)[0] if anthropic_images else messages
            reason = 'invalid_response'
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(self.base + '/utils/token_counter', params={'call_endpoint': 'true'},
                        headers=self.headers, json={'model': payload['model'], 'messages': counted})
                    response.raise_for_status()
                    value = response.json()
                    count = positive(value.get('total_tokens'))
                    if count is None or value.get('error'):
                        raise ValueError('Invalid token count')
                    kind = value.get('tokenizer_type', '')
                    providers = {'anthropic_api'} if anthropic_images else {
                        'openai_api', 'anthropic_api', 'google_api', 'gemini_api'}
                    provider_counter = kind in providers
                    if images and not provider_counter:
                        # OpenAI's counter can silently omit native image blocks;
                        # a positive count alone does not prove it saw the image.
                        reason = 'unverified_image_counter'
                        raise ValueError('No verified provider image counter')
                    tokens = count if provider_counter else max(count, conservative)
                    method = 'provider_serialization_estimate' if provider_counter else 'conservative_gateway_estimate'
                    return tokens, method
            except httpx.HTTPStatusError as exc:
                reason = f'http_{exc.response.status_code}'
            except httpx.HTTPError:
                reason = 'transport_error'
            except (ValueError, TypeError, AttributeError):
                pass
            reasons.append(reason)
        if images:
            # Upstream bodies can contain private prompts, image data or keys.
            logging.getLogger(__name__).warning('Image token counting unavailable: %s', ', '.join(reasons))
            raise HTTPException(503, 'Image token counting is unavailable. The request was not sent to the model.') from None
        return conservative, method

    async def measure(self, payload, *, scope=''):
        limits = await self.limits(payload['model'])
        supplied = [payload[key] for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in payload]
        if any(positive(value) is None for value in supplied):
            raise HTTPException(422, 'Invalid output limit.')
        if len(supplied) > 1:
            raise HTTPException(422, 'Specify only one output-token limit.')
        # Without a configured default, reserve the model maximum. Do not
        # silently reduce or insert any generation field in the actual request.
        output = supplied[0] if supplied else (limits.default_output_tokens or limits.max_output_tokens)
        if output > limits.max_output_tokens:
            raise HTTPException(422, 'The requested output allowance exceeds the selected model maximum.')
        capacity = min(limits.max_input_tokens, limits.context_window - output)
        # Reserve protocol/translation overhead and estimation uncertainty.
        available = capacity - max(512, math.ceil(capacity * .1))
        if available <= 0:
            raise HTTPException(422, 'The selected model has no input room with this output allowance. '
                'Choose compatible model limits or an explicit output allowance.')
        tokens, method = await self.count(payload, input_budget=available, scope=scope)
        return Budget(tokens, available, output, limits.context_window, method)

    async def check(self, payload, *, scope=''):
        budget = await self.measure(payload, scope=scope)
        if budget.input_tokens > budget.input_budget:
            raise ContextPressure(budget.public())
        return budget
