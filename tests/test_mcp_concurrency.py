"""Exercise the real stdio bridge with blocked HTTP reads and ordered actions."""
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Event, Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sandbox import mcp_bridge


def message(identity, name):
    return {'jsonrpc': '2.0', 'id': identity, 'method': 'tools/call',
            'params': {'name': name, 'arguments': {}}}


def test_slow_github_http_does_not_block_skills_memory_or_ping_and_eof_drains():
    started, release = Event(), Event()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            assert self.headers['Authorization'] == 'Bearer fixture-capability'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            name = body['name']
            calls.append(name)
            if name == 'github_repositories':
                started.set()
                assert release.wait(10)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'name': name, 'payload': 'x' * 32768}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    script = Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py'
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={'PATH': os.environ['PATH'],
        'WORKSPACE_BROKER_URL': f'http://127.0.0.1:{server.server_port}', 'WORKSPACE_RUN_TOKEN': 'fixture-capability'})
    replies = Queue()
    reader = Thread(target=lambda: [replies.put(json.loads(line)) for line in process.stdout], daemon=True)
    reader.start()
    try:
        process.stdin.write(json.dumps(message('slow', 'github_repositories')) + '\n')
        process.stdin.flush()
        assert started.wait(5)
        requests = [message('skills', 'skills_search'), message('memory', 'memory_search'),
                    {'id': 'ping', 'method': 'ping'}, {'method': 'notifications/initialized'}]
        process.stdin.write('\n'.join(json.dumps(row) for row in requests) + '\n')
        process.stdin.close()
        # Slow request cannot finish until we release it, so no latency threshold
        # or scheduler timing assumption is needed to prove the overlap.
        fast = [replies.get(timeout=5) for _ in range(3)]
        assert {row['id'] for row in fast} == {'skills', 'memory', 'ping'}
        for row in fast:
            assert 'error' not in row
            if row['id'] != 'ping':
                data = json.loads(row['result']['content'][0]['text'])
                assert data['name'] == row['id'] + '_search'
                assert data['payload'] == 'x' * 32768
        assert process.poll() is None  # EOF waits for the accepted GitHub request.
        release.set()
        slow = replies.get(timeout=5)
        assert slow['id'] == 'slow' and not slow['result']['isError']
        assert process.wait(timeout=5) == 0, process.stderr.read()
        reader.join(timeout=2)
        assert replies.empty()
        assert calls == ['github_repositories', 'skills_search', 'memory_search']
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_stateful_tools_remain_fifo_while_reads_overlap(monkeypatch, capsys):
    started, release = Event(), Event()
    names = ['skills_search', 'memory_search', 'skills_save', 'memory_save', 'browser_open',
             'browser_click', 'github_checkout', 'github_create_pull_request', 'credentials_run', 'new_tool']
    events = []
    def call(params, catalog):
        name = params['name']
        events.append(('start', name))
        if name == names[0]:
            started.set()
            assert release.wait(5)
        if name == 'github_repositories':
            assert started.wait(5)
            # No later context/browser/write action can overtake the first.
            assert events == [('start', names[0]), ('start', 'github_repositories')]
            release.set()
        events.append(('end', name))
        return {'isError': False, 'content': []}
    def lines():
        yield json.dumps(message(0, names[0]))
        assert started.wait(5)
        for i, name in enumerate(names[1:], 1):
            yield json.dumps(message(i, name))
        yield json.dumps(message('read', 'github_repositories'))
    monkeypatch.setattr(mcp_bridge, 'call_tool', call)
    monkeypatch.setattr(sys, 'stdin', lines())
    mcp_bridge.serve()
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(replies) == len(names) + 1 and all('error' not in row for row in replies)
    assert [event for event in events if event[1] != 'github_repositories'] == [
        (phase, name) for name in names for phase in ('start', 'end')]


def test_read_capacity_is_bounded_and_busy_requests_never_run(monkeypatch, capsys):
    started, release, free_lane = Event(), Event(), Event()
    entered, active = [], 0
    lock = Lock()
    def call(params, catalog):
        nonlocal active
        name = params['name']
        if name == 'memory_search':
            free_lane.set()
            return {'isError': False}
        with lock:
            active += 1
            entered.append(name)
            assert active <= 2
            if active == 2:
                started.set()
        assert release.wait(5)
        with lock:
            active -= 1
        return {'isError': False}
    def lines():
        for i in range(2):
            yield json.dumps(message(i, 'github_repositories'))
        assert started.wait(5)
        for i in range(2, 5):
            yield json.dumps(message(i, 'github_repositories'))
        yield json.dumps(message('context', 'memory_search'))
        assert free_lane.wait(5)
        release.set()
    monkeypatch.setattr(mcp_bridge, 'READ_WORKERS', 2)
    monkeypatch.setattr(mcp_bridge, 'MAX_PENDING_PER_LANE', 3)
    monkeypatch.setattr(mcp_bridge, 'call_tool', call)
    monkeypatch.setattr(sys, 'stdin', lines())
    mcp_bridge.serve()
    replies = {row['id']: row for row in map(json.loads, capsys.readouterr().out.splitlines())}
    assert len(replies) == 6
    assert len(entered) == 3
    for i in (3, 4):
        assert replies[i]['result']['isError']
        assert 'not started' in replies[i]['result']['content'][0]['text']
    assert not replies['context']['result']['isError']


def test_concurrent_allowlist_excludes_local_and_context_side_effects():
    from app.connectors import RETRY_SAFE_READS
    assert mcp_bridge.CONCURRENT_READS <= RETRY_SAFE_READS
    assert 'github_checkout' not in mcp_bridge.CONCURRENT_READS
    assert not any(name.startswith(('browser_', 'skills_', 'memory_', 'credentials_'))
                   for name in mcp_bridge.CONCURRENT_READS)
