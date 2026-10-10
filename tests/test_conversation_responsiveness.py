"""Conversation navigation stays responsive while another tab polls activity."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from test_workspace import workspace  # noqa: F401


async def test_slow_activity_poll_does_not_block_opening_another_conversation(workspace, monkeypatch):
    app, client = workspace
    store = app.state.store
    first = store.create_run('Existing tab', '', 'demo', [], chat_enabled=True)
    second = store.create_run('Open this conversation', '', 'demo', [], chat_enabled=True)
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, 'path', '') == '/api/runs/{run_id}/events')
    loop_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    original_run, original_events = store.run, store.events

    def read_run(*args, **kwargs):
        # Includes the initial stream lookup as well as subsequent poll reads.
        if args[0] == first['id']:
            assert threading.get_ident() != loop_thread
        return original_run(*args, **kwargs)

    def delayed_events(run_id, *args, **kwargs):
        if run_id == first['id']:
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(5), 'Test did not release the activity read'
        return original_events(run_id, *args, **kwargs)

    monkeypatch.setattr(store, 'run', read_run)
    monkeypatch.setattr(store, 'events', delayed_events)

    async def connected():
        return False

    request = SimpleNamespace(headers={}, cookies=dict(client.cookies), is_disconnected=connected)
    response = await endpoint(first['id'], request)
    stream = response.body_iterator
    polling = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url=app.state.settings.public_url,
                                    cookies=dict(client.cookies)) as other:
            loaded = await asyncio.wait_for(other.get(f"/api/runs/{second['id']}?activity=summary"), 2)
        assert loaded.status_code == 200
        assert loaded.json()['messages'][0]['content'] == 'Open this conversation'
        assert not polling.done(), 'Navigation must finish while the other read is still held'
    finally:
        release.set()
        await polling
        await stream.aclose()


@pytest.mark.parametrize('route', ['config', 'activity'])
async def test_conversation_support_reads_run_outside_the_event_loop(workspace, monkeypatch, route):
    app, client = workspace
    store = app.state.store
    run = store.create_run('Read activity', '', 'demo', [], chat_enabled=True)
    message = store.messages(run['id'])[0]
    loop_thread = threading.get_ident()
    original = store.identity if route == 'config' else store.run
    observed = []

    def checked(*args, **kwargs):
        observed.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'identity' if route == 'config' else 'run', checked)
    path = '/api/config' if route == 'config' else f"/api/runs/{run['id']}/activity?message_id={message['id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url=app.state.settings.public_url,
                                cookies=dict(client.cookies)) as reader:
        response = await reader.get(path)
    assert response.status_code == 200, response.text
    assert observed
