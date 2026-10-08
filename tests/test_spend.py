import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.db import Store
from app.security import digest
from app.spend import UsageCapture, money, stamp, completion_events
from test_workspace import workspace
from test_slack import slack_app
from test_slack import event, signed
from test_slack_chat import start
from scripts.spend_backfill import backfill, connect, load_logs, timestamp


def sign_in(app, client, sub='alice', email='alice@berri.ai'):
    app.state.settings.google_client_id = 'google-client'
    app.state.settings.google_client_secret = 'google-secret'
    app.state.settings.google_admin_emails = 'alice@berri.ai'
    response = JSONResponse({})
    sid = app.state.security.new_session(response, identity={'sub': sub, 'email': email, 'domain': 'berri.ai', 'name': sub.title()})
    client.cookies.set('workspace_session', response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0])
    client.headers['X-CSRF-Token'] = app.state.security.csrf(sid)
    client.headers['Origin'] = app.state.settings.public_url
    assert client.get('/api/session').json()['authenticated']
    return 'google:' + sub


def active(app, user='google:alice', model='openai/gpt-6-astra'):
    run = app.state.store.create_run('Spend test', '', 'modal', [], chat_enabled=True, user_id=user, model=model)
    app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    return app.state.store.run(run['id'])


def test_sso_owner_and_per_turn_actor_are_server_bound_and_persist(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    alice = sign_in(app, client)
    run = client.post('/api/runs', json={'prompt': 'First response'}).json()
    assert run['owner_id'] == alice
    first = app.state.store.claim_message(run['id'])
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    data = {'content': 'Second response', 'client_id': 'a-new-message'}
    assert client.post(f"/api/runs/{run['id']}/messages", json=data).status_code == 202
    assert client.post('/api/runs', json={'prompt': 'Fake user', 'user_id': alice}).status_code == 422
    assert app.state.store.run(run['id'])['active_user_id'] == alice
    assert app.state.store.run(run['id'])['owner_id'] == alice
    app.state.store.finish_message(run['id'], first['id'], 'Done')
    app.state.store.claim_message(run['id'])
    assert app.state.store.run(run['id'])['active_user_id'] == bob
    sign_in(app, client)
    assert client.post(f"/api/runs/{run['id']}/messages", json=data).status_code == 409
    reopened = Store(app.state.settings.data_dir)
    assert reopened.run(run['id'])['owner_id'] == alice
    assert reopened.run(run['id'])['active_user_id'] == bob


def test_spend_is_admin_only_and_mutations_require_csrf(workspace):
    app, client = workspace
    sign_in(app, client)
    assert client.get('/api/admin/spend').status_code == 200
    sign_in(app, client, 'bob', 'bob@berri.ai')
    for url in ['/api/admin/spend', '/api/admin/spend/link-slack']:
        response = client.get(url) if url.endswith('spend') else client.post(url, json={'slack_user_id':'x','google_user_id':'y'})
        assert response.status_code == 403
    client.cookies.clear()
    assert client.get('/api/admin/spend').status_code == 401


def test_broker_records_exact_cost_and_blocks_sandbox_attribution_override(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    app.state.settings.litellm_api_key = 'spend-test-key'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = active(app)
    app.state.store.enqueue_message(run['id'], 'Queued by Bob', 'bobs-next-message', user_id='google:bob')
    captured = []
    def gateway(request):
        data = json.loads(request.content)
        captured.append(data)
        assert 'user' not in data
        assert 'session_id' not in data['metadata']
        assert data['stream'] is False
        assert data['metadata']['moyai_request_id'] == request.headers['x-litellm-call-id']
        assert set(data['metadata']) == {'moyai_request_id'}
        return httpx.Response(200, headers={'x-litellm-response-cost':'0.0123456789', 'x-litellm-call-id':request.headers['x-litellm-call-id']}, json={'id':'response','choices':[], 'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers={'Authorization':'Bearer capability'}, json={'messages':[], 'user':'someone-else','metadata':{'moyai_request_id':'spoof'}})
    assert response.status_code == 200
    row = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert row['user_id'] == 'google:alice' and row['cost'] == '0.0123456789'
    assert row['total_tokens'] == 110 and row['status'] == 'completed'
    report = client.get('/api/admin/spend').json()
    assert report['total']['spend'] == '0.0123456789'
    assert report['priced_requests'] == 1
    assert row['cost_source'] == 'response_header'
    assert 'spend-test-key' not in json.dumps(report)


def test_stream_parser_handles_split_utf8_and_final_usage_without_storing_text():
    raw = ('data: '+json.dumps({'choices':[{'delta':{'content':'hello ö'}}]},ensure_ascii=False)+'\n\ndata: '+json.dumps({'usage':{'total_tokens':50},'x_litellm_response_cost':0.000125})+'\n\ndata: [DONE]\n\n').encode()
    capture = UsageCapture(True)
    for byte in raw:
        capture.feed(bytes([byte]))
    capture.finish()
    assert capture.done and capture.usage['total_tokens'] == 50 and capture.cost == '0.000125'
    assert capture.buffer == b''
    for value in ('NaN','Infinity','-1',True,'garbage'):
        assert money(value) is None
    assert money('0') == '0'


@pytest.mark.parametrize('raw,expected', [
    (b'0.01234567890123456789', '0.01234567890123456789'),
    (b'"0.01234567890123456789"', '0.01234567890123456789'),
    (b'0', '0'), (b'null', None), (b'true', None), (b'-1', None),
    (b'"NaN"', None), (b'"Infinity"', None), (b'"garbage"', None),
    (b'{}', None), (b'[]', None),
])
def test_stream_usage_cost_validation_and_precision(raw: bytes, expected: str | None) -> None:
    capture = UsageCapture(True)
    wire = b'data: {"usage":{"total_tokens":14,"cost":' + raw + b'}}\n\ndata: [DONE]\n\n'
    for byte in wire:
        capture.feed(bytes([byte]))
    capture.finish()
    assert capture.done and capture.cost == expected and capture.usage['total_tokens'] == 14


def test_stream_cost_precedence_and_cumulative_events() -> None:
    capture = UsageCapture(True)
    capture.feed(b'data: {"x_litellm_response_cost":2,"usage":{"x_litellm_response_cost":1}}\n\n')
    assert capture.cost == '1'
    capture.feed(b'data: {"x_litellm_response_cost":2,"usage":{"x_litellm_response_cost":1,"cost":0}}\n\n')
    assert capture.cost == '0'
    for _ in range(2):
        capture.feed(b'data: {"usage":{"cost":0.125}}\n\n')
    capture.feed(b'data: {"cost":99,"usage":{"total_tokens":14}}\n\ndata: [DONE]\n\n')
    capture.finish()
    assert capture.done and capture.cost == '0.125'


def test_broker_buffers_gateway_to_obtain_final_cost_for_streaming_clients(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_key = 'spend-test-key'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = active(app)
    def gateway(request):
        data = json.loads(request.content)
        assert data['stream'] is False and 'stream_options' not in data
        return httpx.Response(200, headers={'x-litellm-response-cost':'0.02'}, json={'id':'reply','choices':[{'index':0,'message':{'role':'assistant','content':'Ready'},'finish_reason':'stop'}], 'usage':{'total_tokens':9}})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    url = f"/broker/{run['id']}/v1/chat/completions"
    response = client.post(url,headers={'Authorization':'Bearer capability'},json={'messages':[], 'stream':True})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:') and '[DONE]' not in line]
    assert frames[0]['choices'][0]['delta']['content'] == 'Ready'
    assert frames[-1]['usage']['total_tokens'] == 9
    assert response.text.endswith('data: [DONE]\n\n')
    row = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert row['cost'] == '0.02' and row['total_tokens'] == 9
    assert app.state.spend.report()['total']['missing_costs'] == 0
    def fail(request):
        return httpx.Response(429)
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(fail), **kw))
    assert client.post(url,headers={'Authorization':'Bearer capability'},json={'messages':[]}).status_code == 502
    assert app.state.store.rows("SELECT * FROM model_requests WHERE status='failed'")


def test_slack_each_sender_is_tracked_and_admin_can_link_to_sso(slack_app):
    app, client, run_id = start(slack_app)
    first = app.state.store.messages(run_id)[0]
    assert first['user_id'].startswith('slack:')
    assert app.state.store.run(run_id)['owner_id'] == first['user_id']
    sign_in(app, client)
    app.state.settings.litellm_api_key = 'spend-test-key'
    app.state.store.claim_message(run_id)
    run = app.state.store.run(run_id)
    request_id = app.state.spend.begin(run, run['model'])
    app.state.store.execute("UPDATE model_requests SET cost='0.5' WHERE id=?", (request_id,))
    assert client.post('/api/admin/spend/link-slack',json={'slack_user_id':first['user_id'],'google_user_id':'google:alice'}).status_code == 200
    report = client.get('/api/admin/spend').json()
    assert report['users'][0]['id'] == 'google:alice' and report['users'][0]['spend'] == '0.5'
    assert app.state.store.rows('SELECT * FROM identity_audit')[0]['actor_id'] == 'google:alice'
    assert app.state.store.rows('SELECT user_id FROM model_requests')[0]['user_id'] == first['user_id']
    assert client.post('/api/admin/spend/link-slack',json={'slack_user_id':'google:alice','google_user_id':'google:alice'}).status_code == 422


def test_time_normalization_uses_utc():
    assert stamp('2026-09-28T22:00:00-07:00') == '2026-09-29T05:00:00+00:00'
    assert stamp('2026-09-29T05:00:00') == stamp('2026-09-29T05:00:00Z')


def test_google_subject_survives_email_changes(workspace):
    app, client = workspace
    sign_in(app, client)
    sign_in(app, client, 'alice', 'alice-renamed@berri.ai')
    users = app.state.store.rows("SELECT * FROM users WHERE kind='google'")
    assert len(users) == 1 and users[0]['id'] == 'google:alice'
    assert users[0]['email'] == 'alice-renamed@berri.ai'


def test_second_slack_sender_keeps_own_attribution(slack_app):
    app, client, run_id = start(slack_app)
    first = app.state.store.messages(run_id)[0]
    payload = event(event_id='SecondSender', text='<@U99999999> Please continue')
    payload['event']['user'] = 'U87654321'
    payload['event']['ts'] = '1790720765.000001'
    source = app.state.store.slack_source(run_id)
    payload['event']['thread_ts'] = source['thread_ts']
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    messages = app.state.store.messages(run_id)
    assert len(messages) == 2
    assert messages[1]['user_id'] == 'slack:T12345678:U87654321'
    assert messages[0]['user_id'] == first['user_id']
    assert app.state.store.run(run_id)['owner_id'] == first['user_id']


def test_key_rotation_preserves_organization_cost_history(workspace):
    app, _ = workspace
    app.state.settings.litellm_api_key = 'first-key'
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    app.state.store.execute("UPDATE model_requests SET cost='2.5' WHERE id=?", (rid,))
    assert app.state.spend.report()['total']['spend'] == '2.5'
    app.state.settings.litellm_api_key = 'replacement-key'
    assert app.state.spend.report()['total']['spend'] == '2.5'
    assert app.state.store.rows('SELECT id FROM model_requests')



def test_unknown_cost_is_visible_but_explicit_zero_is_valid(workspace):
    app, client = workspace
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    assert app.state.spend.report()['total']['pending_costs'] == 1
    app.state.spend.finish(rid, None, 'failed')
    rid2 = app.state.spend.begin(run, run['model'])
    app.state.spend.headers(rid2, httpx.Response(200, headers={'x-litellm-response-cost':'0'}), False)
    app.state.spend.finish(rid2, None, 'completed')
    report = app.state.spend.report()
    assert report['total']['pending_costs'] == 0 and report['total']['missing_costs'] == 1
    assert report['priced_requests'] == 1 and report['total']['spend'] == '0'
    assert client.get('/api/admin/spend?start=2026-01-01&end=2026-12-31').status_code == 422
    assert client.get('/api/admin/spend?start=bad').status_code == 422
    assert client.post('/hooks/litellm/cost',json={}).status_code == 404
    assert client.post('/api/admin/spend/sync').status_code == 404


def test_header_is_authoritative_and_accounting_is_persistent(workspace):
    app, _ = workspace
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    app.state.spend.headers(rid, httpx.Response(200, headers={'x-litellm-response-cost':'0.123456789012345678'}), False)
    capture = UsageCapture(False)
    capture.feed(b'{"usage":{"total_tokens":100,"cost":0.1}}')
    capture.finish()
    app.state.spend.finish(rid, capture, 'interrupted')
    app.state.spend.finish(rid, capture, 'interrupted')
    report = app.state.spend.report()
    assert report['total']['spend'] == '0.123456789012345678'
    assert report['total']['requests'] == 1
    assert report['request_details'][0]['cost_source'] == 'response_header'
    reopened = Store(app.state.settings.data_dir)
    assert reopened.rows('SELECT cost FROM model_requests')[0]['cost'] == report['total']['spend']


def test_missing_header_uses_final_usage_cost_without_float_rounding(workspace):
    app, _ = workspace
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    app.state.spend.headers(rid, httpx.Response(200), False)
    capture = UsageCapture(False)
    capture.feed(b'{"usage":{"total_tokens":100,"x_litellm_response_cost":0.01234567890123456789}}')
    capture.finish()
    app.state.spend.finish(rid, capture, 'completed')
    report = app.state.spend.report()
    assert report['total']['spend'] == '0.01234567890123456789'
    assert report['request_details'][0]['cost_source'] == 'response_usage'


def test_stream_headers_never_count_as_final_prices(workspace):
    app, _ = workspace
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    app.state.spend.headers(rid, httpx.Response(200, headers={'x-litellm-response-cost':'0'}), True)
    app.state.spend.finish(rid, None, 'interrupted')
    assert app.state.spend.report()['total']['missing_costs'] == 1


def test_completion_adapter_keeps_tool_calls_reasoning_and_cached_usage():
    value = {'id':'chat-123', 'model':'test-model', 'created':123,
             'choices':[{'index':0, 'message':{'role':'assistant', 'content':None, 'reasoning_content':'Checking',
              'tool_calls':[{'id':'call_1','type':'function','function':{'name':'read_file','arguments':'{"path":"a.py"}'}},
                            {'id':'call_2','type':'function','function':{'name':'terminal','arguments':'{"command":"pwd"}'}}]}, 'finish_reason':'tool_calls'}],
             'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_tokens_details':{'cached_tokens':50}}}
    events = list(completion_events(value))
    frames = [json.loads(frame[5:]) for frame in events[:-1]]
    delta = frames[0]['choices'][0]['delta']
    assert delta['reasoning_content'] == 'Checking'
    assert [tool['index'] for tool in delta['tool_calls']] == [0,1]
    assert delta['tool_calls'][0]['function']['arguments'] == '{"path":"a.py"}'
    assert frames[1]['choices'][0]['finish_reason'] == 'tool_calls'
    assert frames[2]['usage'] == value['usage']
    assert events[-1] == 'data: [DONE]\n\n'


def test_cost_survives_session_stop_during_inference(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = active(app)
    def gateway(request):
        app.state.store.update_run(run['id'], status='cancelled', token_hash='')
        return httpx.Response(200, headers={'x-litellm-response-cost':'0.2'}, json={'choices':[], 'usage':{'total_tokens':40}})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers={'Authorization':'Bearer capability'}, json={'messages':[], 'stream':True})
    assert response.status_code == 200 and not response.content
    row = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert row['cost'] == '0.2' and row['total_tokens'] == 40
    assert row['user_id'] == 'google:alice'


def test_user_session_model_totals_match_exactly_and_requests_keep_original_date(workspace):
    app, client = workspace
    for user, model, cost in [('alice','openai/gpt-6-astra','0.12'),('bob','anthropic/claude-opus-5-5','0.034'),('alice','anthropic/claude-opus-5-5','0.006')]:
        sign_in(app, client, user, user+'@berri.ai')
        run = active(app, 'google:'+user, model)
        rid = app.state.spend.begin(run, model)
        app.state.store.execute("UPDATE model_requests SET created_at='2026-09-29T23:59:59+00:00' WHERE id=?", (rid,))
        app.state.spend.headers(rid, httpx.Response(200, headers={'x-litellm-response-cost':cost}), False)
        app.state.spend.finish(rid, None, 'completed')
    report = app.state.spend.report(start=datetime(2026,9,29).date(),end=datetime(2026,9,29).date())
    for field in ['users','sessions','models']:
        assert sum(Decimal(row['spend']) for row in report[field]) == Decimal(report['total']['spend']) == Decimal('0.160')
    assert app.state.spend.report(start=datetime(2026,9,30).date(),end=datetime(2026,9,30).date())['total']['spend'] == '0'


def test_usage_capture_normalizes_cache_aliases_without_overwriting_zero() -> None:
    capture = UsageCapture(False)
    capture.consume({'usage': {'prompt_tokens': 125, 'cache_read_input_tokens': 0,
        'cache_creation_input_tokens': None, 'prompt_tokens_details': {'cached_tokens': 100,
            'cache_write_tokens': None, 'cache_creation_tokens': 20,
            'cache_creation_token_details': {'ephemeral_5m_input_tokens': 20}}}})
    assert capture.usage['cache_read_input_tokens'] == 0
    assert capture.usage['cache_creation_input_tokens'] == 20
    assert capture.usage['cache_creation'] == {'ephemeral_5m_input_tokens': 20}
    capture.consume({'usage': {'prompt_tokens_details': 'malformed'}})
    assert 'cache_read_input_tokens' not in capture.usage


def test_usage_capture_preserves_reported_reasoning_zero_and_omits_missing_breakdown() -> None:
    capture = UsageCapture(False)
    capture.consume({'usage': {'reasoning_tokens': 0, 'completion_tokens_details': {'reasoning_tokens': 2}}})
    assert capture.usage['reasoning_tokens'] == 0
    capture.consume({'usage': {'output_tokens_details': {'reasoning_tokens': 0}}})
    assert capture.usage['reasoning_tokens'] == 0
    capture.consume({'usage': {'completion_tokens_details': 'malformed'}})
    assert 'reasoning_tokens' not in capture.usage


@pytest.fixture
def backfill_sample(workspace: tuple[FastAPI, TestClient], tmp_path: Path) -> tuple[FastAPI, Path, dict[str, str]]:
    app, _ = workspace
    app.state.settings.litellm_api_key = 'historical-key'
    run = active(app)
    ids = {name: app.state.spend.begin(run, run['model']) for name in ('recover', 'priced', 'pending')}
    for name, rid in ids.items():
        app.state.spend.headers(rid, httpx.Response(200, headers={'x-litellm-call-id': 'gateway-' + rid}), True)
        if name != 'pending':
            app.state.spend.finish(rid, None, 'completed')
    app.state.store.execute("UPDATE model_requests SET created_at='2026-10-01T00:00:00+00:00'")
    app.state.store.execute("UPDATE model_requests SET cost='0',cost_source='response_header' WHERE id=?", (ids['priced'],))
    logs = tmp_path / 'receipts.json'
    records = [{'request_id': 'provider-' + name, 'litellm_call_id': 'gateway-' + rid,
                'api_key': digest('historical-key'), 'spend': '0.01234567890123456789', 'status': 'success'}
               for name, rid in ids.items()]
    logs.write_text(json.dumps(records).replace('"0.01234567890123456789"', '0.01234567890123456789'))
    app.state.settings.litellm_api_key = 'rotated-key'
    return app, logs, ids


def test_backfill_cli_dry_run_apply_backup_and_replay(backfill_sample: tuple[FastAPI, Path, dict[str, str]], tmp_path: Path) -> None:
    app, logs, ids = backfill_sample
    database = app.state.store.path
    original = app.state.store.rows('SELECT * FROM model_requests ORDER BY id')
    command = [sys.executable, 'scripts/spend_backfill.py', '--db', str(database), '--logs', str(logs),
               '--before', '2026-10-07T00:00:00Z']
    preview = subprocess.run(command, text=True, capture_output=True, check=True, timeout=30)
    report = json.loads(preview.stdout)
    assert report['mode'] == 'dry_run' and report['applied'] == 0 and report['recoverable'] == 1
    assert report['recovered_spend'] == '0.01234567890123456789'
    assert report['skip_counts'] == {'already_priced': 1, 'not_finalized': 1}
    assert digest('historical-key') not in preview.stdout
    assert app.state.store.rows('SELECT * FROM model_requests ORDER BY id') == original
    with connect(database) as read_only:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            read_only.execute("UPDATE model_requests SET cost='999'")
    backup = tmp_path / 'before-backfill.db'
    applied = subprocess.run(command + ['--apply', '--expect-plan', report['plan_sha256'], '--backup', str(backup)],
                             text=True, capture_output=True, check=True, timeout=30)
    assert json.loads(applied.stdout)['applied'] == 1
    assert backup.stat().st_mode & 0o777 == 0o600
    with connect(backup) as saved:
        assert [dict(row) for row in saved.execute('SELECT * FROM model_requests ORDER BY id')] == original
    expected = [{**row, 'cost': '0.01234567890123456789', 'cost_source': 'gateway_backfill'}
                if row['id'] == ids['recover'] else row for row in original]
    assert app.state.store.rows('SELECT * FROM model_requests ORDER BY id') == expected
    totals = app.state.spend.report(start=datetime(2026, 10, 1).date(), end=datetime(2026, 10, 7).date())
    assert totals['total']['spend'] == '0.01234567890123456789' and totals['priced_requests'] == 2
    rerun = json.loads(subprocess.run(command, text=True, capture_output=True, check=True, timeout=30).stdout)
    assert rerun['recoverable'] == 0
    stale = subprocess.run(command + ['--apply', '--expect-plan', report['plan_sha256'], '--backup', str(tmp_path / 'stale.db')],
                           text=True, capture_output=True, timeout=30)
    assert stale.returncode == 1 and not (tmp_path / 'stale.db').exists()
    assert app.state.store.rows('SELECT * FROM model_requests ORDER BY id') == expected


@pytest.mark.parametrize('shape', ['call_id', 'metadata_call_id', 'nested_moyai_id', 'metadata_string', 'legacy_request_id', 'duplicate', 'failure'])
def test_backfill_matches_real_export_id_shapes(backfill_sample: tuple[FastAPI, Path, dict[str, str]], shape: str) -> None:
    app, logs, ids = backfill_sample
    record = json.loads(logs.read_text(), parse_float=str)[0]
    call = record.pop('litellm_call_id')
    if shape == 'metadata_call_id':
        record['metadata'] = {'litellm_call_id': call}
    elif shape in {'nested_moyai_id', 'metadata_string'}:
        metadata = {'spend_logs_metadata': {'moyai_request_id': ids['recover']}}
        record['metadata'] = json.dumps(metadata) if shape == 'metadata_string' else metadata
    elif shape == 'legacy_request_id':
        record['request_id'] = call
    else:
        record['litellm_call_id'] = call
    if shape == 'failure':
        record['status'] = 'failure'
    logs.write_text(json.dumps([record, record] if shape == 'duplicate' else [record]))
    report = backfill(app.state.store.path, [logs], timestamp('2026-10-07T00:00:00Z'))
    assert report['recoverable'] == 1 and report['changes'][0]['id'] == ids['recover']


@pytest.mark.parametrize('scenario,reason', [
    ('wrong_key', 'no_matching_receipt'), ('conflicting_alias', 'ambiguous_receipts'),
    ('unknown_alias', 'conflicting_identifiers'), ('retry', 'multiple_billed_attempts'),
    ('conflicting_duplicate', 'ambiguous_receipts'), ('zero', 'zero_cost_needs_review'),
    ('negative', 'invalid_cost'), ('nan', 'invalid_cost'), ('boolean', 'invalid_cost'),
    ('unknown_status', 'unknown_receipt_status'), ('bad_metadata', 'invalid_metadata'),
    ('new_request', 'outside_cutoff'), ('unfinished', 'not_finalized'),
])
def test_backfill_leaves_uncertain_costs_unknown(backfill_sample: tuple[FastAPI, Path, dict[str, str]], scenario: str, reason: str) -> None:
    app, logs, ids = backfill_sample
    record = json.loads(logs.read_text(), parse_float=str)[0]
    records = [record]
    if scenario == 'wrong_key':
        record['api_key'] = digest('different-key')
    elif scenario in {'conflicting_alias', 'unknown_alias'}:
        record['metadata'] = {'litellm_call_id': ids['pending'] if scenario == 'conflicting_alias' else 'unknown-call'}
    elif scenario == 'retry':
        records.append({**record, 'request_id': 'another-provider-attempt'})
    elif scenario == 'conflicting_duplicate':
        records.append({**record, 'spend': '2.0'})
    elif scenario in {'zero', 'negative', 'nan', 'boolean'}:
        record['spend'] = {'zero': 0, 'negative': -1, 'nan': 'NaN', 'boolean': True}[scenario]
    elif scenario == 'unknown_status':
        record['status'] = None
    elif scenario == 'bad_metadata':
        record['metadata'] = 'broken-json'
    elif scenario == 'new_request':
        app.state.store.execute("UPDATE model_requests SET created_at='2026-10-07T00:00:00+00:00' WHERE id=?", (ids['recover'],))
    elif scenario == 'unfinished':
        app.state.store.execute('UPDATE model_requests SET finished_at=NULL WHERE id=?', (ids['recover'],))
    logs.write_text(json.dumps(records))
    report = backfill(app.state.store.path, [logs], timestamp('2026-10-07T00:00:00Z'))
    assert report['recoverable'] == 0
    assert {'id': ids['recover'], 'reason': reason} in report['skipped']


def test_backfill_apply_is_atomic_and_requires_a_new_backup(backfill_sample: tuple[FastAPI, Path, dict[str, str]], tmp_path: Path) -> None:
    app, logs, ids = backfill_sample
    database, cutoff = app.state.store.path, timestamp('2026-10-07T00:00:00Z')
    app.state.store.execute("UPDATE model_requests SET cost=NULL,status='completed',finished_at='2026-10-01T01:00:00Z'")
    last = sorted(ids.values())[-1]
    with app.state.store.connect() as conn:
        conn.execute(f"CREATE TRIGGER reject_repair BEFORE UPDATE OF cost ON model_requests WHEN OLD.id='{last}' "
                     "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    report = backfill(database, [logs], cutoff)
    assert report['recoverable'] == 3
    backup = tmp_path / 'backup.db'
    with pytest.raises(ValueError, match='requires'):
        backfill(database, [logs], cutoff, apply=True)
    with pytest.raises(sqlite3.IntegrityError, match='fixture failure'):
        backfill(database, [logs], cutoff, apply=True, expected_plan=report['plan_sha256'], backup=backup)
    assert all(row['cost'] is None for row in app.state.store.rows('SELECT cost FROM model_requests'))
    with pytest.raises(FileExistsError):
        backfill(database, [logs], cutoff, apply=True, expected_plan=report['plan_sha256'], backup=backup)
    assert backup.exists()


def test_backfill_rejects_incomplete_capped_or_aggregate_exports(tmp_path: Path) -> None:
    one, two = tmp_path / 'one.json', tmp_path / 'two.json'
    page = {'data': [{'request_id': 'first'}], 'total': 2, 'page': 1, 'page_size': 1,
            'total_pages': 2, 'total_is_capped': False}
    one.write_text(json.dumps(page))
    with pytest.raises(ValueError, match='Missing'):
        load_logs([one])
    two.write_text(json.dumps({**page, 'data': [{'request_id': 'second'}], 'page': 2}))
    assert len(load_logs([one, two])[0]) == 2
    two.write_text(json.dumps({**page, 'page': 2}))
    with pytest.raises(ValueError, match='Overlapping'):
        load_logs([one, two])
    one.write_text(json.dumps({**page, 'total_is_capped': True}))
    with pytest.raises(ValueError, match='uncapped'):
        load_logs([one, two])
    one.write_text('[{"spend": 1}]')
    with pytest.raises(ValueError, match='per-request'):
        load_logs([one])
    with pytest.raises(ValueError, match='timezone'):
        timestamp('2026-10-07')
    missing = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        connect(missing)
    assert not missing.exists()
