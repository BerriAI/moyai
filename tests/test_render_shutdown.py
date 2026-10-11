"""Exercise real Uvicorn sockets and POSIX signals without production services."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest
import uvicorn

from render_start import RenderAPIServer


PROBE = r'''
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import uvicorn
from render_start import RenderAPIServer

events = Path(sys.argv[1])
def record(event, **values):
    with events.open('a') as output:
        output.write(json.dumps(dict(event=event, at=time.monotonic(), **values)) + '\n')

@asynccontextmanager
async def lifespan(app):
    record('owner_acquired')
    try:
        yield
    finally:
        record('owner_released')

app = FastAPI(lifespan=lifespan)
@app.get('/health')
async def health():
    record('request')
    return {'status': 'ok'}

@app.get('/stream')
async def stream(seconds: float = 1):
    async def body():
        record('stream_started')
        yield b'start\n'
        await asyncio.sleep(seconds)
        record('stream_finished')
        yield b'finished\n'
    return StreamingResponse(body())

class ProbeServer(RenderAPIServer):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        record('ready', port=self.servers[0].sockets[0].getsockname()[1])
    def handle_exit(self, sig, frame):
        super().handle_exit(sig, frame)
        record('signal', sig=sig, grace=self.listener_grace_deadline,
               drain=self.http_drain_deadline)

config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='critical',
                        timeout_graceful_shutdown=float(sys.argv[3]))
ProbeServer(config, listener_grace_seconds=float(sys.argv[2])).run()
'''


class RunningAPI:
    def __init__(self, directory, grace=1.5, drain=2):
        self.events_path = directory / 'events.jsonl'
        self.output = (directory / 'server.log').open('w+')
        self.process = subprocess.Popen(
            [sys.executable, '-u', '-c', PROBE, str(self.events_path), str(grace), str(drain)],
            cwd=Path(__file__).resolve().parents[1], stdout=self.output, stderr=subprocess.STDOUT,
        )
        self.port = self.wait_event('ready')['port']

    def events(self, name):
        lines = self.events_path.read_text().splitlines() if self.events_path.exists() else []
        return [event for line in lines if (event := json.loads(line))['event'] == name]

    def wait_event(self, name, count=1, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            events = self.events(name)
            if len(events) >= count:
                return events[count - 1]
            if self.process.poll() is not None:
                break
            time.sleep(0.01)
        self.output.seek(0)
        pytest.fail(f'Missing {name}: {self.output.read()}')

    def request(self, path='/health'):
        with urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5) as response:
            return response.read()

    def signal(self, sig):
        count = len(self.events('signal')) + 1
        self.process.send_signal(sig)
        return self.wait_event('signal', count)

    def wait_listener_closed(self, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1):
                    pass
            except OSError:
                return time.monotonic()
            time.sleep(0.01)
        pytest.fail('Listener did not close')

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.output.close()


@pytest.fixture
def api_server(tmp_path):
    instances = []
    def start(**kwargs):
        directory = tmp_path / str(len(instances))
        directory.mkdir()
        server = RunningAPI(directory, **kwargs)
        instances.append(server)
        return server
    yield start
    for server in instances:
        server.close()


@pytest.mark.skipif(os.name == 'nt', reason='Render uses POSIX signals')
def test_sigterm_keeps_accepting_then_drains_stream_before_lifespan_cleanup(api_server):
    server = api_server()
    first_signal = server.signal(signal.SIGTERM)
    assert json.loads(server.request()) == {'status': 'ok'}
    # Admit a real streaming response after SIGTERM. It outlasts the listener
    # grace and must finish before the application's ownership is released.
    with ThreadPoolExecutor() as pool:
        stream = pool.submit(server.request, '/stream?seconds=2')
        server.wait_event('stream_started')
        time.sleep(0.2)
        repeated = server.signal(signal.SIGTERM)
        assert repeated['grace'] == first_signal['grace']
        assert repeated['drain'] == first_signal['drain']
        assert json.loads(server.request()) == {'status': 'ok'}
        closed_at = server.wait_listener_closed()
        assert first_signal['grace'] <= closed_at < first_signal['grace'] + 0.5
        assert not server.events('owner_released')
        assert stream.result(timeout=5) == b'start\nfinished\n'
    released = server.wait_event('owner_released')
    assert released['at'] >= server.events('stream_finished')[0]['at']
    server.process.wait(timeout=5)


@pytest.mark.skipif(os.name == 'nt', reason='Render uses POSIX signals')
@pytest.mark.parametrize('first_signal', [signal.SIGINT, signal.SIGTERM])
def test_sigint_bypasses_listener_grace_including_force_exit(api_server, first_signal):
    server = api_server(grace=10)
    server.signal(first_signal)
    if first_signal == signal.SIGTERM:
        server.signal(signal.SIGINT)
    assert server.wait_listener_closed(timeout=1)
    server.process.wait(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='Render uses POSIX signals')
def test_stream_exceeding_finite_drain_is_cancelled_and_cleanup_runs(api_server):
    server = api_server(grace=0.2, drain=0.2)
    with ThreadPoolExecutor() as pool:
        stream = pool.submit(server.request, '/stream?seconds=10')
        server.wait_event('stream_started')
        first_signal = server.signal(signal.SIGTERM)
        server.process.wait(timeout=2)
        with pytest.raises(Exception):
            stream.result(timeout=1)
    assert not server.events('stream_finished')
    assert server.events('owner_released')[0]['at'] < first_signal['at'] + 1.5


def test_signal_before_started_does_not_defer_shutdown():
    server = RenderAPIServer(uvicorn.Config('unused:app'))
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit
    assert server.listener_grace_deadline is None
    assert server.http_drain_deadline is None


def test_startup_failure_does_not_wait_for_listener_grace(tmp_path):
    probe = PROBE.replace("record('owner_acquired')", "raise RuntimeError('startup failed')")
    result = subprocess.run(
        [sys.executable, '-u', '-c', probe, str(tmp_path / 'events.jsonl'), '30', '240'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 3
    assert not (tmp_path / 'events.jsonl').exists()


async def test_late_event_loop_does_not_extend_http_drain_budget(monkeypatch):
    import render_start
    server = RenderAPIServer(uvicorn.Config('unused:app', timeout_graceful_shutdown=240))
    server.started = True
    monkeypatch.setattr(render_start.time, 'monotonic', lambda: 100)
    server.handle_exit(signal.SIGTERM, None)
    assert server.listener_grace_deadline == 130
    assert server.http_drain_deadline == 370
    monkeypatch.setattr(render_start.time, 'monotonic', lambda: 150)
    calls = []
    async def shutdown(self, sockets=None):
        calls.append(self.config.timeout_graceful_shutdown)
    monkeypatch.setattr(uvicorn.Server, 'shutdown', shutdown)
    await server.shutdown()
    assert calls == [220]
