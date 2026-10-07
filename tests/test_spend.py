import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi.responses import JSONResponse

from app.db import Store
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
    capture.feed(b'{"usage":{"total_tokens":100,"x_litellm_response_cost":0.1}}')
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
