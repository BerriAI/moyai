import json
from test_workspace import workspace
from test_spend import sign_in


def test_side_chat_is_durable_isolated_and_attributed_to_its_requester(workspace, monkeypatch):
    app, client = workspace
    store = app.state.store
    submitted = []
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: submitted.append(run['id']))
    parent = store.create_run('Build the checkout flow', '', 'demo', [], chat_enabled=True)
    message = store.claim_message(parent['id'])
    store.finish_message(parent['id'], message['id'], 'Use address validation before checkout.', 'completed')
    store.update_run(parent['id'], status='running', sandbox_id='original-sandbox')
    before = store.messages(parent['id'])
    sign_in(app, client, 'bob', 'bob@berri.ai')
    body = {'prompt': 'Explain the validation choices', 'mode': 'demo', 'side_chat_of': parent['id'], 'client_id': 'side-chat-once'}
    response = client.post('/api/runs', json=body)
    assert response.status_code == 201
    side = response.json()
    assert side['id'] != parent['id'] and side['side_chat_of'] == parent['id']
    assert side['owner_id'] == 'google:bob' and side['parent_run_id'] == ''
    assert side['snapshot_id'] == side['sandbox_id'] == ''
    assert 'token_hash' not in side
    assert 'side_chat_context' not in side
    stored = store.run(side['id'])
    context = json.loads(stored['side_chat_context'])
    assert context['task'] == parent['prompt']
    assert any('address validation' in m['content'] for m in context['conversation'])
    assert app.state.manager.spec(stored)['side_chat_context'] == stored['side_chat_context']
    assert store.messages(parent['id']) == before
    assert store.run(parent['id'])['status'] == 'running'
    assert store.run(parent['id'])['sandbox_id'] == 'original-sandbox'
    assert set(submitted) == {side['id']}
    assert client.post('/api/runs', json=body).json()['id'] == side['id']
    assert len(client.get('/api/runs/'+parent['id']+'/side-chats').json()) == 1
    assert client.post('/api/runs/'+side['id']+'/messages', json={'content':'Clarify required fields', 'client_id':'side-followup-one'}).status_code == 202
    assert store.messages(parent['id']) == before
    assert store.messages(side['id'])[-1]['user_id'] == 'google:bob'


def test_side_chat_security_and_retry_scope(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    first = app.state.store.create_run('First task', '', 'demo', [], chat_enabled=True)
    second = app.state.store.create_run('Second task', '', 'demo', [], chat_enabled=True)
    body = {'prompt':'A side question', 'mode':'demo', 'client_id':'retry-side-one', 'side_chat_of':first['id']}
    assert client.post('/api/runs', json=body, headers={'X-CSRF-Token':'bad'}).status_code == 403
    assert client.post('/api/runs', json={**body, 'side_chat_of':'a'*32}).status_code == 404
    assert client.post('/api/runs', json={**body, 'side_chat_of':'../../no'}).status_code == 422
    assert client.post('/api/runs', json={**body, 'chat_enabled':False}).status_code == 422
    assert client.post('/api/runs', json=body).status_code == 201
    assert client.post('/api/runs', json={**body,'side_chat_of':second['id']}).status_code == 409
    assert client.post('/api/runs', json={**body,'prompt':'Different text'}).status_code == 409
    client.cookies.clear()
    assert client.get('/api/runs/'+first['id']+'/side-chats').status_code == 401


def test_side_context_is_bounded_and_never_a_live_link(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    store=app.state.store
    parent=store.create_run('Original task', '', 'demo', [], chat_enabled=True)
    for i in range(35):
        store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant',?,'completed','2026-10-02')", (parent['id'],str(i)+'🗿'*5000))
    side=client.post('/api/runs', json={'prompt':'Explain', 'mode':'demo','side_chat_of':parent['id']}).json()
    context=store.run(side['id'])['side_chat_context']
    assert len(context)<=48000 and json.loads(context)['conversation']
    store.update_run(parent['id'],summary='Changed after the side conversation started')
    assert store.run(side['id'])['side_chat_context']==context
    assert 'side_chat_context' not in client.get('/api/runs/'+side['id']).json()
    assert all('side_chat_context' not in r for r in client.get('/api/runs').json())


def test_escaped_snapshot_text_still_obeys_size_limit(workspace, monkeypatch):
    app,client=workspace
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    parent=app.state.store.create_run('\x00'*16000,'','demo',[],chat_enabled=True)
    app.state.store.update_run(parent['id'],summary='\x00'*16000)
    side=client.post('/api/runs',json={'prompt':'Explain','side_chat_of':parent['id']}).json()
    context=app.state.store.run(side['id'])['side_chat_context']
    assert len(context)<=48000
    assert json.loads(context)['task']
