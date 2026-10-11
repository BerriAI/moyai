"""Real sockets and PostgreSQL ownership during broker/coordinator shutdown."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
import signal
from urllib.parse import urlsplit
from unittest.mock import Mock

import httpx
import pytest
import uvicorn

from app.context_budget import ModelContextLimits
from app.main import create_app
from app.security import digest
from render_start import RenderOverlapServer
from test_broker_overlap import verified, status  # noqa: F401
from test_coordinator_handoff import handoff_settings  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401
from test_slack import event, signed


CLIENT = httpx.AsyncClient


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def serving(app):
    class Server(RenderOverlapServer):
        # The subprocess tests cover OS signal delivery. Here call the same
        # handler without installing process-wide handlers in pytest's loop.
        @contextmanager
        def capture_signals(self):
            yield

    server = Server(uvicorn.Config(app, host='127.0.0.1', port=0,
        timeout_graceful_shutdown=3, log_level='critical', proxy_headers=False),
        listener_grace_seconds=1)
    task = asyncio.create_task(server.serve())
    try:
        await wait_until(lambda: server.started or task.done())
        assert not task.done(), 'Server startup failed'
        port = server.servers[0].sockets[0].getsockname()[1]
        async with CLIENT(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                          headers={'Host': urlsplit(app.state.settings.public_url).netloc}) as client:
            yield server, client, task
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)


async def test_broker_listener_grace_preserves_admitted_model_stream_and_owner(verified, monkeypatch):
    broker, _ = verified
    first, replacement = broker(), broker()
    started, finish = asyncio.Event(), asyncio.Event()
    provider_calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"type":"response.created","response":{"id":"old-stream"}}\n\n'
            started.set()
            await finish.wait()
            yield b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"old-stream","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'

    def upstream(request):
        if request.url.path != '/v1/responses':
            return httpx.Response(200, json={'data': [], 'total_tokens': 10})
        provider_calls.append(request)
        if len(provider_calls) == 1:
            return httpx.Response(200, stream=Stream())
        return httpx.Response(200, json={'id': 'new-call', 'status': 'completed',
            'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}})

    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: CLIENT(
        transport=httpx.MockTransport(upstream), **kw))

    def request(app):
        app.state.settings.litellm_api_base = 'https://synthetic.invalid/v1'
        app.state.settings.model_context_limits = {app.state.settings.resolve_model(): ModelContextLimits(
            context_window=65536, max_input_tokens=65536, max_output_tokens=8192)}
        run = app.state.store.create_run('shutdown fixture', '', 'modal', [])
        app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
        return f"/broker/{run['id']}/v1/responses"

    old_route, new_route = request(first), request(replacement)
    async with AsyncExitStack() as stack:
        old, old_client, old_task = await stack.enter_async_context(serving(first))
        _, new_client, _ = await stack.enter_async_context(serving(replacement))
        old.handle_exit(signal.SIGTERM, None)
        assert (await old_client.get('/health')).status_code == 200
        running = asyncio.create_task(old_client.post(old_route,
            headers={'Authorization': 'Bearer cap'}, json={'input': 'hello', 'stream': True}))
        try:
            await asyncio.wait_for(started.wait(), 3)
            original = first.state.store.rows('SELECT id FROM model_requests')[0]['id']
            response = await new_client.post(new_route, headers={'Authorization': 'Bearer cap'},
                                             json={'input': 'world', 'stream': False})
            assert response.status_code == 200, response.text
            assert response.json()['id'] == 'new-call'
            await wait_until(lambda: not old.servers[0].is_serving())
            assert not old_task.done() and not running.done()
            assert not first.state.model_slots._closed
            assert replacement.state.broker_requests.work.recover() == 0
            assert status(replacement, 'model_requests', 'id', original) == 'pending'
            assert replacement.state.model_slots.snapshot()['active'] == 1
            finish.set()
            streamed = await asyncio.wait_for(running, 3)
            assert streamed.status_code == 200 and 'response.completed' in streamed.text
            await asyncio.wait_for(asyncio.shield(old_task), 5)
            assert first.state.model_slots._closed
            assert status(replacement, 'model_requests', 'id', original) == 'completed'
            assert replacement.state.model_slots.snapshot()['active'] == 0
            assert len(provider_calls) == 2
        finally:
            finish.set()
            await asyncio.gather(running, return_exceptions=True)


async def test_coordinator_grace_retains_leader_and_standby_signed_hook_admission(handoff_settings, monkeypatch):
    first = create_app(handoff_settings)
    second = create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    calls = []
    for app, name in [(first, 'first'), (second, 'second')]:
        async def handle(payload, missing, owner=name):
            calls.append((owner, payload['event_id']))
        monkeypatch.setattr(app.state.slack, 'receive_payload', handle)
    monkeypatch.setattr(second.state.slack, 'recover', Mock(wraps=second.state.slack.recover))
    closing, release = asyncio.Event(), asyncio.Event()
    close = first.state.slack.shutdown

    async def close_after_receipt(**kwargs):
        closing.set()
        await release.wait()
        await close(**kwargs)
    monkeypatch.setattr(first.state.slack, 'shutdown', close_after_receipt)

    async with AsyncExitStack() as stack:
        old, old_client, old_task = await stack.enter_async_context(serving(first))
        _, new_client, _ = await stack.enter_async_context(serving(second))
        try:
            await wait_until(lambda: first.state.coordinator_handoff.status == 'leader')
            assert second.state.coordinator_handoff.status == 'standby'
            old.handle_exit(signal.SIGTERM, None)
            assert (await old_client.post('/hooks/slack/events', **signed(event('EvOldGrace')))).status_code == 200
            assert (await new_client.post('/hooks/slack/events', **signed(event('EvStandbyGrace')))).status_code == 200
            await wait_until(lambda: len(calls) == 2)
            assert all(owner == 'first' for owner, _ in calls)
            await asyncio.wait_for(closing.wait(), 5)
            assert not old.servers[0].is_serving()
            assert not old_task.done()
            assert second.state.coordinator_handoff.status == 'standby'
            second.state.slack.recover.assert_not_called()
            assert (await new_client.post('/hooks/slack/events', **signed(event('EvDuringCleanup')))).status_code == 200
            assert len(calls) == 2
            release.set()
            await wait_until(lambda: second.state.coordinator_handoff.status == 'leader')
            await wait_until(lambda: len(calls) == 3)
            assert calls[-1] == ('second', 'EvDuringCleanup')
            assert len({event_id for _, event_id in calls}) == 3
            second.state.slack.recover.assert_called_once()
        finally:
            release.set()
