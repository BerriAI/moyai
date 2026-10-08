import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.main import create_app
from app.security import digest
from app.spend import UsageCapture, money, stamp, completion_events
from test_workspace import workspace
from test_slack import slack_app
from test_slack import event, signed
from test_slack_chat import start


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


def recovery_request(app: FastAPI, *, status: str = 'interrupted') -> dict[str, object]:
    app.state.settings.litellm_api_base = 'https://gateway.example/v1/'
    app.state.settings.litellm_api_key = 'recovery-fixture-key'
    run = active(app)
    request_id = app.state.spend.begin(run, run['model'])
    if status != 'pending':
        capture = UsageCapture(False)
        capture.consume({'usage': {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}})
        app.state.spend.finish(request_id, capture, status)
    return app.state.store.rows('SELECT * FROM model_requests WHERE id=?', (request_id,))[0]


def recovery_receipt(row: dict[str, object], **changes: object) -> dict[str, object]:
    return {'request_id': 'provider-' + str(row['id']), 'litellm_call_id': row['id'],
            'api_key': row['key_hash'], 'spend': '0.01234567890123456789', 'status': 'success',
            'metadata': {'litellm_call_id': row['id'],
                         'spend_logs_metadata': {'moyai_request_id': row['id']}}, **changes}


def recovery_page(rows: list[dict[str, object]], **changes: object) -> dict[str, object]:
    return {'data': rows, 'total': len(rows), 'page': 1, 'page_size': 1000,
            'total_pages': 1 if rows else 0, 'total_is_capped': False, **changes}


@pytest.mark.parametrize('receipt_status', ['success', 'failure'])
async def test_cost_recovery_is_exact_persistent_and_never_overwrites_attribution(
        workspace: tuple[FastAPI, TestClient], receipt_status: str) -> None:
    app, _ = workspace
    row = recovery_request(app)
    observed: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET' and str(request.url).startswith('https://gateway.example/spend/logs/v2?')
        assert request.headers['authorization'] == 'Bearer recovery-fixture-key'
        assert request.url.params['request_id'] == row['id']
        assert request.url.params['api_key'] == row['key_hash']
        for field in ('start_date', 'end_date'):
            datetime.strptime(request.url.params[field], '%Y-%m-%d %H:%M:%S')
        observed.append(request.method)
        item = recovery_receipt(row, status=receipt_status)
        if receipt_status == 'failure':
            item['metadata'] = {'litellm_call_id': row['id'], 'spend_logs_metadata': None}
        return httpx.Response(200, json=recovery_page([item]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        await app.state.spend.recovery.process(client, row)
        await app.state.spend.recovery.process(client, row)
    settled = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert settled['cost'] == '0.01234567890123456789'
    assert settled['cost_receipt_id'] == 'provider-' + str(row['id'])
    for field in ('user_id', 'run_id', 'message_id', 'model', 'created_at', 'status', 'total_tokens'):
        assert settled[field] == row[field]
    app.state.spend.headers(row['id'], httpx.Response(200, headers={'x-litellm-response-cost': '0'}), True)
    app.state.spend.headers(row['id'], httpx.Response(200, headers={'x-litellm-response-cost': '999'}), False)
    capture = UsageCapture(False)
    capture.consume({'usage': {'cost': 999}})
    app.state.spend.finish(row['id'], capture, 'interrupted')
    report = app.state.spend.report()
    assert report['total']['spend'] == settled['cost'] and report['priced_requests'] == 1
    assert report['request_details'][0]['cost_status'] == 'settled'
    assert report['request_details'][0]['cost_source'] == settled['cost_source']
    assert Store(app.state.settings.data_dir).rows('SELECT cost FROM model_requests')[0]['cost'] == settled['cost']
    assert observed and set(observed) == {'GET'}


@pytest.mark.parametrize('change', ['key', 'gateway', 'legacy'])
async def test_cost_recovery_never_queries_a_different_scope(
        workspace: tuple[FastAPI, TestClient], change: str) -> None:
    app, _ = workspace
    row = recovery_request(app)
    if change == 'key':
        app.state.settings.litellm_api_key = 'replacement-fixture-key'
    elif change == 'gateway':
        app.state.settings.litellm_api_base = 'https://other-gateway.example/v1'
    else:
        app.state.store.execute("UPDATE model_requests SET gateway_scope='' WHERE id=?", (row['id'],))
        row['gateway_scope'] = ''

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail('An unavailable original credential or gateway must not trigger lookup')

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        await app.state.spend.recovery.process(client, row)
    assert app.state.store.rows('SELECT cost FROM model_requests')[0]['cost'] is None
    assert app.state.spend.report()['request_details'][0]['cost_status'] == 'unresolved'


@pytest.mark.parametrize('change', ['key', 'gateway'])
async def test_cost_recovery_rechecks_scope_after_http_await(
        workspace: tuple[FastAPI, TestClient], change: str) -> None:
    app, _ = workspace
    row = recovery_request(app)

    async def gateway(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        if change == 'key':
            app.state.settings.litellm_api_key = 'replacement-fixture-key'
        else:
            app.state.settings.litellm_api_base = 'https://other-gateway.example/v1'
        return httpx.Response(200, json=recovery_page([recovery_receipt(row)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        await app.state.spend.recovery.process(client, row)
    assert app.state.store.rows('SELECT cost FROM model_requests')[0]['cost'] is None
    assert app.state.spend.report()['request_details'][0]['cost_status'] == 'unresolved'


@pytest.mark.parametrize('case', [
    'multiple', 'incomplete', 'capped', 'conflicting', 'wrong_key', 'wrong_call',
    'zero', 'negative', 'nonfinite', 'boolean', 'missing_cost', 'unknown_status',
])
async def test_cost_recovery_rejects_unsafe_receipts(
        workspace: tuple[FastAPI, TestClient], case: str) -> None:
    app, _ = workspace
    row = recovery_request(app)
    item = recovery_receipt(row)
    page = recovery_page([item])
    if case == 'multiple':
        page = recovery_page([item, recovery_receipt(row, request_id='another-provider-attempt')])
    elif case == 'incomplete':
        page.update(total=2, page_size=1, total_pages=2)
    elif case == 'capped':
        page['total_is_capped'] = True
    elif case == 'conflicting':
        item['metadata'] = {'spend_logs_metadata': {'moyai_request_id': 'another-moyai-request'}}
    elif case == 'wrong_key':
        item['api_key'] = digest('foreign-fixture-key')
    elif case == 'wrong_call':
        item['litellm_call_id'] = 'unrelated-call'
    elif case == 'unknown_status':
        item['status'] = 'pending'
    else:
        item['spend'] = {'zero': 0, 'negative': -1, 'nonfinite': 'Infinity',
                         'boolean': True, 'missing_cost': None}[case]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=page))) as client:
        await app.state.spend.recovery.process(client, row)
    saved = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert saved['cost'] is None and saved['cost_receipt_id'] == ''
    assert saved['cost_recovery_error'] and saved['cost_recovery_attempts'] == 1
    assert datetime.fromisoformat(saved['cost_next_attempt_at']) > datetime.now(timezone.utc)
    public = app.state.spend.report()['request_details'][0]
    assert public['cost_status'] == 'unresolved'
    assert 'key_hash' not in public and 'gateway_scope' not in public


async def test_cost_recovery_receipt_cannot_price_two_requests(
        workspace: tuple[FastAPI, TestClient]) -> None:
    app, _ = workspace
    first, second = recovery_request(app), recovery_request(app)
    rows = {row['id']: row for row in (first, second)}

    def gateway(request: httpx.Request) -> httpx.Response:
        item = recovery_receipt(rows[request.url.params['request_id']], request_id='one-provider-receipt')
        return httpx.Response(200, json=recovery_page([item]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        await app.state.spend.recovery.process(client, first)
        await app.state.spend.recovery.process(client, second)
    saved = {row['id']: row for row in app.state.store.rows('SELECT * FROM model_requests')}
    assert saved[first['id']]['cost'] == '0.01234567890123456789'
    assert saved[second['id']]['cost'] is None and saved[second['id']]['cost_recovery_error']
    assert app.state.spend.report()['priced_requests'] == 1


async def test_cost_recovery_retries_delayed_receipts_without_querying_inflight_requests(
        workspace: tuple[FastAPI, TestClient]) -> None:
    app, _ = workspace
    row = recovery_request(app)
    inflight = recovery_request(app, status='pending')
    calls: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params['request_id'])
        return httpx.Response(200, json=recovery_page([] if len(calls) == 1 else [recovery_receipt(row)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        await app.state.spend.recovery.process(client, row)
        waiting = app.state.store.rows('SELECT * FROM model_requests WHERE id=?', (row['id'],))[0]
        assert waiting['cost'] is None and waiting['cost_recovery_attempts'] == 1
        assert datetime.fromisoformat(waiting['cost_next_attempt_at']) > datetime.now(timezone.utc)
        report = app.state.spend.report()
        assert report['total']['pending_costs'] == 2 and report['total']['missing_costs'] == 0
        await app.state.spend.recovery.poll(client)
        assert calls == [row['id']]
        app.state.store.execute("UPDATE model_requests SET cost_next_attempt_at='2000-01-01T00:00:00+00:00'")
        await app.state.spend.recovery.poll(client)
    assert calls == [row['id'], row['id']] and inflight['id'] not in calls
    assert app.state.store.rows('SELECT cost FROM model_requests WHERE id=?', (row['id'],))[0]['cost'] is not None


async def test_cost_recovery_marks_interrupted_requests_before_session_recovery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, litellm_api_key='',
                             modal_token_id='', modal_token_secret='', session_titles_enabled=False))
    prior = recovery_request(app, status='pending')
    app.state.settings.litellm_api_key = ''
    newly_started: list[str] = []

    async def recover() -> None:
        row = app.state.store.rows('SELECT * FROM model_requests WHERE id=?', (prior['id'],))[0]
        assert row['status'] == 'interrupted' and row['cost'] is None
        # A resumed session can submit fresh inference as soon as recovery starts.
        newly_started.append(app.state.spend.begin(app.state.store.run(prior['run_id']), 'test-model'))

    async def idle() -> None:
        return None

    monkeypatch.setattr(app.state.manager, 'recover', recover)
    monkeypatch.setattr(app.state.spend.recovery, 'watch', idle)
    for owner in (app.state.identities, app.state.environments, app.state.automations,
                  app.state.tracing, app.state.spend.infrastructure, app.state.session_titles):
        monkeypatch.setattr(owner, 'start', lambda: None)
    monkeypatch.setattr(app.state.slack, 'recover', lambda: None)
    async with app.router.lifespan_context(app):
        row = app.state.store.rows('SELECT status,cost FROM model_requests WHERE id=?', (newly_started[0],))[0]
        assert row == {'status': 'pending', 'cost': None}


@pytest.mark.parametrize('fault', [401, 403, 429, 302, 'timeout', 'malformed', 'oversized', 'nested'])
async def test_cost_recovery_faults_remain_unknown_and_retry_without_leaking_diagnostics(
        workspace: tuple[FastAPI, TestClient], fault: int | str) -> None:
    app, _ = workspace
    row = recovery_request(app)
    calls: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if fault == 'timeout':
            raise httpx.ReadTimeout('private upstream detail')
        if fault == 'malformed':
            return httpx.Response(200, content=b'private malformed payload')
        if fault == 'oversized':
            return httpx.Response(200, content=b' ' * (256 * 1024 + 1))
        if fault == 'nested':
            return httpx.Response(200, content=b'[' * 2000 + b']' * 2000)
        return httpx.Response(fault, headers={'Location': 'https://unexpected.example'}, text='private upstream detail')

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway), follow_redirects=True) as client:
        await app.state.spend.recovery.process(client, row)
    result = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert result['cost'] is None and result['cost_recovery_attempts'] == 1
    assert result['cost_recovery_error'] and 'private' not in json.dumps(result)
    assert len(calls) == 1 and calls[0].startswith('https://gateway.example/')


async def test_cost_recovery_checkpoint_survives_a_new_app_and_migration_is_idempotent(
        workspace: tuple[FastAPI, TestClient], tmp_path: Path) -> None:
    from app.persistence import Checkpoints, restore_checkpoint
    app, _ = workspace
    row = recovery_request(app)
    settings = app.state.settings.model_copy(update={'checkpoint_dir': tmp_path / 'saved'})

    async def commit() -> None:
        return None

    await Checkpoints(app.state.store, settings, commit=commit).flush()
    restored_settings = settings.model_copy(update={
        'data_dir': tmp_path / 'restored',
        'encryption_key': (settings.data_dir / 'encryption.key').read_text().strip(),
    })
    restore_checkpoint(restored_settings)
    # Construction runs schema upgrades on the restored database twice.
    restored_settings.checkpoint_dir = None
    create_app(restored_settings)
    restored = create_app(restored_settings)
    copied = restored.state.store.rows('SELECT * FROM model_requests')[0]
    assert copied == row

    def gateway(request: httpx.Request) -> httpx.Response:
        raw = json.dumps(recovery_page([recovery_receipt(row)]))
        # The gateway emits a JSON number, including digits beyond binary float precision.
        return httpx.Response(200, content=raw.replace('"0.01234567890123456789"', '0.01234567890123456789'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        await restored.state.spend.recovery.process(client, copied)
    assert restored.state.spend.report()['total']['spend'] == '0.01234567890123456789'
    assert app.state.spend.report()['total']['spend'] == '0'


async def test_cost_recovery_disk_wait_does_not_block_streaming_loop(
        workspace: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    app, _ = workspace
    row = recovery_request(app)
    release = threading.Event()
    settle = app.state.spend.settle_receipt

    def slow_settle(*args: str) -> bool:
        # Only the event loop can release this simulated disk wait.
        assert release.wait(2), 'Recovery blocked the event loop'
        return settle(*args)

    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=recovery_page([recovery_receipt(row)]))

    monkeypatch.setattr(app.state.spend, 'settle_receipt', slow_settle)
    timer = asyncio.get_running_loop().call_later(0.05, release.set)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
            await app.state.spend.recovery.process(client, row)
    finally:
        timer.cancel()
        release.set()
    assert app.state.store.rows('SELECT cost FROM model_requests')[0]['cost'] == '0.01234567890123456789'


async def test_cost_recovery_shutdown_joins_an_outstanding_database_write() -> None:
    import threading
    from app.spend_recovery import database
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write() -> None:
        started.set()
        assert release.wait(2)
        finished.set()

    task = asyncio.create_task(database(write))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.parametrize('wire, completed', [
    (b'{}', True), (b'{"error":null}', True),
    (b'{"error":{"message":"failed"}}', False), (b'{"error":{}}', False),
    (b'{"error":[]}', False), (b'{"error":false}', False),
    (b'{"error":0}', False), (b'{"error":""}', False),
    (b'null', False), (b'[]', False), (b'not-json', False), (b'\xff', False),
])
def test_nonstream_capture_distinguishes_null_from_nonnull_error(wire, completed):
    capture = UsageCapture(False)
    capture.feed(wire)
    capture.finish()
    assert capture.done is completed
    assert capture.buffer == b''


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('fields', [{}, {'error': None}])
def test_chat_null_error_preserves_success_and_accounting(workspace, monkeypatch, stream, fields):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = active(app)
    value = {'id': 'chat-success', 'choices': [{'index': 0, 'finish_reason': 'stop',
             'message': {'role': 'assistant', 'content': 'ACCOUNTING_OK'}}],
             'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3},
             **fields}
    def upstream(request):
        assert json.loads(request.content)['stream'] is False
        return httpx.Response(200, json=value, headers={'x-litellm-response-cost': '0.0123456789'})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions",
        headers={'Authorization': 'Bearer capability'}, json={'messages': [], 'stream': stream})
    assert response.status_code == 200
    if stream:
        assert 'ACCOUNTING_OK' in response.text and 'data: [DONE]' in response.text
    else:
        assert response.json() == value
    reopened = Store(app.state.settings.data_dir)
    rows = reopened.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))
    assert len(rows) == 1
    assert rows[0]['status'] == 'completed' and rows[0]['finished_at']
    assert rows[0]['total_tokens'] == 3 and rows[0]['cost'] == '0.0123456789'
