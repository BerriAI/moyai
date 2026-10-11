"""Steering during generation must be prompt, tool-safe, and correctly billed."""
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import uvicorn

from agent.continuation import AgentSteer
from test_spend import active
from test_workspace import workspace  # noqa: F401


def test_listener_interrupts_pending_generation_but_never_polls_during_tools():
    controls, interrupts = [], []
    interrupted = threading.Event()
    def control():
        controls.append(True)
        return {'steer_message_id': 42}
    agent = SimpleNamespace(interrupt=lambda: (interrupts.append(True), interrupted.set()))
    steer = AgentSteer(SimpleNamespace(control=control))
    steer.listen(agent)
    try:
        assert not interrupted.wait(0.05) and not controls
        with steer.model_wait():
            assert interrupted.wait(2)
            assert steer.message_id == 42 and steer.requested
        steer.step(agent)
        assert len(interrupts) == len(controls) == 1
    finally:
        steer.close()
    assert not steer.thread.is_alive()


def test_response_cannot_reach_tools_until_inflight_control_interrupt_is_settled():
    checking, release, left_window, interrupted = (threading.Event() for _ in range(4))
    def control():
        checking.set()
        assert release.wait(2)
        return {'steer_message_id': 8}
    steer = AgentSteer(SimpleNamespace(control=control))
    steer.listen(SimpleNamespace(interrupt=interrupted.set))
    def response():
        with steer.model_wait():
            assert checking.wait(2)
        # In production the relay can only forward the model response now.
        assert interrupted.is_set()
        left_window.set()
    thread = threading.Thread(target=response)
    thread.start()
    try:
        assert checking.wait(2)
        assert not left_window.wait(0.05)
        release.set()
        thread.join(timeout=3)
        assert left_window.is_set()
    finally:
        release.set()
        steer.close()
        thread.join(timeout=3)


def test_leaving_generation_window_prevents_late_interrupts():
    target = [None]
    interrupted = threading.Event()
    steer = AgentSteer(SimpleNamespace(control=lambda: {'steer_message_id': target[0]}))
    steer.listen(SimpleNamespace(interrupt=interrupted.set))
    try:
        with steer.model_wait():
            pass
        target[0] = 42  # Tool round is executing now: no model request pending.
        assert not interrupted.wait(1.1)
        steer.step(SimpleNamespace(interrupt=interrupted.set))
        assert interrupted.is_set()
    finally:
        steer.close()


def test_disconnected_model_client_still_records_original_turn_cost(workspace, monkeypatch):
    import asyncio
    app, _ = workspace
    run = active(app)
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'server-key'
    entered, release = threading.Event(), threading.Event()
    async def upstream(request):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.002'}, json={
            'choices':[{'message':{'role':'assistant','content':'Old response discarded'}}],
            'usage':{'prompt_tokens':20,'completion_tokens':4,'total_tokens':24}})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    client = None
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        client = socket.create_connection(('127.0.0.1', port))
        body = json.dumps({'messages':[{'role':'user','content':'Slow old turn'}], 'stream':True}).encode()
        headers = (f'POST /broker/{run["id"]}/v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                   f'Authorization: Bearer capability\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()
        client.sendall(headers + body)
        assert entered.wait(3)
        client.close()  # Hermes aborts only its local request; Render keeps accounting.
        old = app.state.store.rows('SELECT message_id,user_id FROM model_requests')[0]
        app.state.store.execute("UPDATE runs SET active_message_id=999,active_user_id='google:bob' WHERE id=?", (run['id'],))
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = app.state.store.rows('SELECT * FROM model_requests')[0]
            if record['status'] == 'completed':
                break
            time.sleep(0.02)
        assert record['status'] == 'completed' and record['cost'] == '0.002'
        assert record['message_id'] == old['message_id'] and record['user_id'] == old['user_id'] == 'google:alice'
    finally:
        release.set()
        if client:
            client.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
