"""Real pinned Hermes -> stdio MCP -> Moyai broker, with fixture providers.

Optional because Hermes is built separately inside Modal, not a web dependency.
Set HERMES_TEST_SOURCE and HERMES_TEST_PYTHON to run this integration check.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest

from sandbox.agent import hermes_config
from test_workspace import cloud_capability, workspace  # noqa: F401
from test_spend import sign_in, active


def test_native_tool_search_preserves_broker_scope_and_direct_writes(workspace, tmp_path, monkeypatch):
    source, python = os.environ.get('HERMES_TEST_SOURCE'), os.environ.get('HERMES_TEST_PYTHON')
    if not source or not python:
        pytest.skip('Set HERMES_TEST_SOURCE and HERMES_TEST_PYTHON for the pinned Hermes integration')
    app, client = workspace
    revision = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
    assert revision == app.state.settings.hermes_revision
    run_id, headers = cloud_capability(app, ['linear', 'github', 'slack', 'notion'])
    # Exercise a complete parent catalog without starting Temporal or Modal.
    app.state.settings.temporal_enabled = True
    actor = sign_in(app, client)
    app.state.store.execute('UPDATE runs SET chat_enabled=1 WHERE id=?', (run_id,))
    message, _ = app.state.store.enqueue_message(run_id, 'Keep benchmark summaries short.', 'memory-fixture', user_id=actor)
    app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    provider_calls, broker_calls = [], []
    async def provider(name, arguments):
        provider_calls.append(name)
        return {'result': 'fixture-ok'}
    async def github(run, name, arguments):
        return await provider(name, arguments)
    monkeypatch.setattr(app.state.connectors, 'call', provider)
    monkeypatch.setattr(app.state.connectors.github, 'call', github)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, response):
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            if self.path != '/tools':
                # Hermes may probe the custom model origin's metadata routes.
                self.send_error(404)
                return
            assert self.headers['Authorization'] == headers['Authorization']
            self.reply(client.get(f'/broker/{run_id}/tools', headers=headers))

        def do_POST(self):
            if self.path != '/tools/call':
                self.send_error(404)
                return
            assert self.headers['Authorization'] == headers['Authorization']
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            broker_calls.append(body['name'])
            if body['name'] == 'slack_search' and body['arguments']['query'] == 'revoked':
                response = client.patch('/api/connections/slack/policy', json={'enabled': False, 'read_only': False})
                assert response.status_code == 200
            self.reply(client.post(f'/broker/{run_id}/tools/call', headers=headers, json=body))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    profile = tmp_path / 'hermes'
    profile.mkdir()
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', headers['Authorization'].removeprefix('Bearer '))
    url = f'http://127.0.0.1:{server.server_port}'
    config = hermes_config({'model': 'test-model', 'broker_url': url}, url, tmp_path)
    # Only filesystem/executable locations differ from the sandbox config.
    config['mcp_servers']['workspace'].update(command=python, args=[str(root / 'agent/tools/mcp_bridge.py')])
    (profile / 'config.yaml').write_text(json.dumps(config))
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
    env.update(HERMES_HOME=str(profile), PYTHONPATH=source, HERMES_RUNTIME_DIR=str(tmp_path / 'runtimes'))
    env['MOYAI_TEST_TURN_ID'] = str(message['id'])
    try:
        result = subprocess.run([python, str(root / 'tests/hermes_tool_search_probe.py')],
            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        proof = next(line.removeprefix('TOOL_SEARCH_PROOF ') for line in result.stdout.splitlines()
                     if line.startswith('TOOL_SEARCH_PROOF '))
        print(proof)
        assert {'memory_save', 'memory_search'} <= set(broker_calls)
        assert 'short-benchmark-memory' in app.state.memory.context(app.state.store.run(run_id))
        # New run, same identity: storage outlives a sandbox's tool process.
        next_run = active(app)
        assert 'short-benchmark-memory' not in app.state.memory.context(next_run)
        app.state.memory.call(next_run, 'memory_search', {'turn_id': next_run['active_message_id'], 'query': 'benchmark'})
        assert 'short-benchmark-memory' in app.state.memory.context(next_run)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert provider_calls == ['linear_search', 'github_repositories', 'slack_search', 'linear_comment']
    assert broker_calls == ['memory_save', 'memory_search', *provider_calls[:3], 'slack_search', 'linear_comment']
    assert not app.state.store.approvals(run_id)
