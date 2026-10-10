"""Production-derived skill rejections through the real app, relay and MCP bridge."""
import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import HTTPException

from app.security import digest
from test_broker_transport import diagnostic_relay
from test_skill_saving import call, form
from test_spend import active, sign_in
from test_workspace import workspace


@pytest.fixture
def skill_relay(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = call(client, run, **form(files=[{'path': 'checks.md', 'content': 'private-skill-marker'}])).json()
    assert saved['saved']
    app.state.store.update_run(run['id'], token_hash=digest('private-capability'))
    receipts = []

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def forward(self):
            content = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            response = client.request(self.command, '/broker/' + run['id'] + self.path,
                content=content, headers={key: self.headers[key] for key in ('Authorization', 'Content-Type')
                                         if key in self.headers})
            if self.path == '/tools/call':
                receipts.append({'status': response.status_code, 'body': response.json()})
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = forward
        do_POST = forward

    with diagnostic_relay(Edge) as (relay, transport, diagnostics):
        assert transport.get('/tools').status_code == 200
        yield app, client, run, relay, transport, diagnostics, receipts


def mcp_calls(relay, calls):
    messages = [{'jsonrpc': '2.0', 'id': index, 'method': 'tools/call', 'params': {
        'name': name, 'arguments': arguments}} for index, (name, arguments) in enumerate(calls, 1)]
    script = Path(__file__).resolve().parents[1] / 'sandbox' / 'mcp_bridge.py'
    result = subprocess.run([sys.executable, str(script)],
        input='\n'.join(json.dumps(message) for message in messages) + '\n',
        text=True, capture_output=True, timeout=15,
        env={'PATH': os.environ['PATH'], 'WORKSPACE_BROKER_URL': relay.url,
             'WORKSPACE_RUN_TOKEN': 'private-capability'})
    assert result.returncode == 0, result.stderr
    assert 'private-skill-marker' not in result.stdout + result.stderr
    return [json.loads(line)['result'] for line in result.stdout.splitlines()]


REJECTIONS = [('load-missing', 404), ('search-turn', 409),
              ('search-keywords', 422), ('save-permission', 403), ('save-revision', 409),
              ('read-missing', 404), ('read-offset', 422)]
EXPLANATIONS = {'load-missing': 'unavailable',
                'search-turn': 'Read the current skill context', 'search-keywords': 'specific skill keywords',
                'save-permission': 'Only an administrator', 'save-revision': 'expected_revision=1',
                'read-missing': 'Supporting file not found', 'read-offset': 'past the end of this file'}


@pytest.mark.parametrize('case,status', REJECTIONS, ids=[case for case, _ in REJECTIONS])
@pytest.mark.parametrize('prior_failure', [False, True], ids=['clean', 'existing-failure'])
def test_rejected_skill_allows_correction_without_clearing_real_failure(skill_relay, case, status,
                                                                      prior_failure, record_property):
    app, client, run, relay, transport, diagnostics, receipts = skill_relay
    valid = ('skills_load', {'name': 'personal:team-review'})
    if case == 'load-missing':
        invalid = ('skills_load', {'name': 'personal:missing'})
    elif case.startswith('search-'):
        args = {'query': 'review', 'turn_id': run['active_message_id']}
        invalid = ('skills_search', {**args, **({'turn_id': args['turn_id'] + 1}
                   if case == 'search-turn' else {'query': 'the and for'})})
        valid = ('skills_search', args)
    elif case == 'save-permission':
        sign_in(app, client, 'member', 'member@berri.ai')
        app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:member', run['id']))
        invalid = ('skills_save', form(scope='organization', request_id='member-save'))
        valid = ('skills_save', form(request_id='member-save'))
    elif case == 'save-revision':
        invalid = ('skills_save', form(expected_revision=2, request_id='update-skill'))
        valid = ('skills_save', form(expected_revision=1, request_id='update-skill'))
    else:
        args = {'name': 'personal:team-review', 'path': 'checks.md'}
        invalid = ('skills_read_file', {**args, **({'path': 'missing.md'}
                   if case == 'read-missing' else {'offset': 1000})})
        valid = ('skills_read_file', args)

    if prior_failure:
        app.state.store.update_run(run['id'], token_hash='')
        assert transport.post('/tools/call', json={'name': valid[0], 'arguments': valid[1]}).status_code == 401
        app.state.store.update_run(run['id'], token_hash=digest('private-capability'))
    previous_failure = relay.last_failure
    receipts.clear()
    rejected, recovered = mcp_calls(relay, [invalid, valid])
    loaded = len(app.state.store.rows('SELECT * FROM skill_uses WHERE run_id=?', (run['id'],)))
    actionable = EXPLANATIONS[case] in rejected['content'][0]['text']
    record_property('replay', json.dumps({'category': 'existing-failure' if prior_failure else 'recoverable',
        'case': case, 'expected_rejection': status, 'response_status': receipts[0]['status'],
        'rejected_is_error': rejected['isError'], 'corrected_is_error': recovered['isError'],
        'continuation_blocked': bool(relay.last_error), 'uncertain_tool': relay.uncertain_tool,
        'loaded_skills': loaded, 'upstream_calls': len(receipts), 'actionable_error': actionable}))

    assert rejected['isError'] and not recovered['isError']
    assert len(receipts) == 2  # Neither the rejection nor the write was retried.
    assert loaded == (0 if case.startswith(('save-', 'search-')) else 1)
    if case == 'save-permission':
        assert not app.state.store.rows("SELECT * FROM skills WHERE scope='organization'")
    if case == 'save-revision':
        assert app.state.store.rows('SELECT revision FROM skills')[0]['revision'] == 2
        assert len(app.state.store.rows('SELECT * FROM skill_saves')) == 2
    assert receipts[0]['status'] == 200
    assert receipts[0]['body']['status_code'] == status
    assert actionable
    assert relay.last_failure == previous_failure
    assert bool(relay.last_error) == relay.uncertain_tool == prior_failure
    assert len(diagnostics) == int(prior_failure)


@pytest.mark.parametrize('case,status', [('capability', 401), ('skill-auth', 401), ('skill-server', 500),
                                       ('checkpoint', 503), ('checkpoint-rejection', 503)])
def test_skill_auth_and_persistence_failures_remain_fatal(skill_relay, monkeypatch, case, status,
                                                       record_property):
    app, client, run, relay, transport, diagnostics, receipts = skill_relay
    with monkeypatch.context() as patch:
        if case == 'capability':
            app.state.store.update_run(run['id'], token_hash='')
        elif case.startswith('skill-'):
            def unavailable(*args):
                raise HTTPException(status, 'Skill service unavailable.')
            patch.setattr(app.state.skills, 'call', unavailable)
        else:
            async def flush():
                raise HTTPException(status, 'Checkpoint unavailable.')
            patch.setattr(app.state.memory.checkpoints, 'flush', flush)
        name = 'personal:missing' if case == 'checkpoint-rejection' else 'personal:team-review'
        rejected, = mcp_calls(relay, [('skills_load', {'name': name})])
    record_property('replay', json.dumps({'category': 'critical', 'case': case,
        'expected_status': status, 'response_status': receipts[0]['status'],
        'rejected_is_error': rejected['isError'], 'continuation_blocked': bool(relay.last_error),
        'uncertain_tool': relay.uncertain_tool, 'upstream_calls': len(receipts)}))
    assert receipts[0]['status'] == status
    assert 'status_code' not in receipts[0]['body']
    assert rejected['isError'] and relay.last_error and relay.uncertain_tool
    assert len(receipts) == len(diagnostics) == 1
