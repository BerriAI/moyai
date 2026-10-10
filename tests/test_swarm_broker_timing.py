"""Broker admission enforces current swarm timing without an execution worker."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from app.security import digest
from sandbox.broker_transport import CONTENT_TYPE, seal
from test_context_gateway import native_exchange
from test_swarm_time_controls import mission, roster, start_swarm, swarm_api


def activate(store, run_id):
    message = store.claim_message(run_id)
    assert message
    token = 'capability-' + run_id
    store.update_run(run_id, status='running', token_hash=digest(token))
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def broker_family(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    child = roster(app, root)[0]['id']
    store = app.state.store
    nested = store.create_run('A nested teammate task.', '', 'modal', [], chat_enabled=True,
                              user_id='google:alice', model=store.run(root)['model'])['id']
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (child, nested))
    runs = {'root': root, 'child': child, 'grandchild': nested}
    headers = {label: activate(store, run_id) for label, run_id in runs.items()}
    return app, client, root, runs, headers


def expire_without_worker(app, root):
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    app.state.store.execute('UPDATE swarm_missions SET ends_at=? WHERE run_id=?', (deadline, root))
    assert mission(app, root)['status'] == 'active'


@pytest.mark.parametrize('member', ['root', 'child', 'grandchild'])
def test_elapsed_root_window_denies_new_broker_work_without_worker_or_token_mutation(broker_family, member, monkeypatch):
    app, client, root, runs, headers = broker_family
    run_id = runs[member]
    tokens = {label: app.state.store.run(identity)['token_hash'] for label, identity in runs.items()}
    expire_without_worker(app, root)
    before = mission(app, root)

    def unexpected_provider(*args, **kwargs):
        pytest.fail('Expired swarm reached provider network construction')
    monkeypatch.setattr('app.main.httpx.AsyncClient', unexpected_provider)
    requests = [
        ('GET', '/v1/models', None),
        ('GET', '/tools', None),
        ('POST', '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'Do new work'}]}),
        ('POST', '/tools/call', {'name': 'github_create_issue', 'arguments': {}}),
        ('POST', '/context/compact', {}),
        ('POST', '/context/maintenance', {}),
        ('POST', '/v1/responses', {}),
        ('POST', '/v1/messages', {}),
        ('POST', '/goal', {'action': 'review'}),
    ]
    for method, suffix, body in requests:
        response = client.request(method, f'/broker/{run_id}{suffix}', headers=headers[member],
                                  **({'json': body} if body is not None else {}))
        assert response.status_code == 401, response.text
        assert 'Swarm time window ended' in response.json()['detail']
    assert mission(app, root) == before
    assert app.state.store.rows('SELECT id FROM model_requests') == []
    for label, identity in runs.items():
        assert app.state.store.run(identity)['status'] == 'running'
        assert app.state.store.run(identity)['token_hash'] == tokens[label]


@pytest.mark.parametrize('member', ['root', 'child', 'grandchild'])
def test_live_time_extension_immediately_reauthorizes_existing_capability(broker_family, member):
    app, client, root, runs, headers = broker_family
    run_id = runs[member]
    url = f'/broker/{run_id}/v1/models'
    assert client.get(url, headers=headers[member]).status_code == 200
    token = app.state.store.run(run_id)['token_hash']
    expire_without_worker(app, root)
    assert client.get(url, headers=headers[member]).status_code == 401
    response = client.patch(f'/api/runs/{root}/swarm/time', json={'budget_seconds': 2 * 3600})
    assert response.status_code == 200, response.text
    assert client.get(url, headers=headers[member]).status_code == 200
    assert app.state.store.run(run_id)['token_hash'] == token
    assert app.state.store.run(run_id)['status'] == 'running'


@pytest.mark.parametrize('status', ['paused', 'stopped', 'blocked', 'expired'])
@pytest.mark.parametrize('member', ['root', 'grandchild'])
def test_inactive_root_blocks_descendant_capabilities_even_with_time_remaining(broker_family, status, member):
    app, client, root, runs, headers = broker_family
    app.state.store.execute('UPDATE swarm_missions SET status=? WHERE run_id=?', (status, root))
    response = client.get(f"/broker/{runs[member]}/tools", headers=headers[member])
    assert response.status_code == 401
    assert 'Swarm is on hold' in response.json()['detail']


@pytest.mark.parametrize('member', ['root', 'child', 'grandchild'])
def test_until_stopped_policy_admits_root_and_nested_members_without_native_deadline(broker_family, member):
    app, client, root, runs, headers = broker_family
    response = client.patch(f'/api/runs/{root}/swarm/time', json={'budget_seconds': 0})
    assert response.status_code == 200, response.text
    assert mission(app, root)['ends_at'] == ''
    assert client.get(f"/broker/{runs[member]}/v1/models", headers=headers[member]).status_code == 200
    assert client.get(f"/broker/{runs[member]}/tools", headers=headers[member]).status_code == 200


def test_invalid_capability_is_rejected_before_swarm_policy_lookup(broker_family, monkeypatch):
    app, client, root, runs, _ = broker_family
    expire_without_worker(app, root)
    def unexpected_policy(*args, **kwargs):
        pytest.fail('Unauthenticated request reached swarm ancestry lookup')
    monkeypatch.setattr('app.swarms.execution_policy', unexpected_policy)
    response = client.get(f"/broker/{runs['grandchild']}/tools", headers={'Authorization': 'Bearer wrong'})
    assert response.status_code == 401
    assert response.json()['detail'] == 'Run capability expired or invalid.'


def test_ordinary_root_broker_requests_do_not_gain_swarm_policy_queries(swarm_api, monkeypatch):
    app, client = swarm_api
    store = app.state.store
    run = store.create_run('An ordinary standalone session.', '', 'modal', [], chat_enabled=True,
                           user_id='google:alice', model=app.state.settings.agent_model)
    headers = activate(store, run['id'])
    assert not store.run(run['id']).get('swarm')
    def unexpected_policy(*args, **kwargs):
        pytest.fail('Ordinary root request gained a swarm ancestry lookup')
    monkeypatch.setattr('app.swarms.execution_policy', unexpected_policy)
    assert client.get(f"/broker/{run['id']}/v1/models", headers=headers).status_code == 200
    assert client.get(f"/broker/{run['id']}/tools", headers=headers).status_code == 200


@pytest.mark.parametrize('member', ['root', 'grandchild'])
def test_expired_window_preserves_completed_tool_receipt_and_native_checkpoint(broker_family, member):
    app, client, root, runs, headers = broker_family
    run_id = runs[member]
    token = headers[member]['Authorization'].removeprefix('Bearer ')
    journal_url = f'/broker/{run_id}/context/journal'
    imported = client.post(journal_url, headers=headers[member], json={
        'action': 'import', 'manifest': {'id': 'initial-work', 'count': 0},
        'entries': [], 'complete': True,
    })
    assert imported.status_code == 200, imported.text
    assert native_exchange(client, run_id, 'begin', token=token).status_code == 200
    expire_without_worker(app, root)
    before = mission(app, root)
    receipt = {'id': 'finished-tool-receipt', 'message': {
        'role': 'tool', 'tool_call_id': 'already-admitted-operation',
        'content': 'The already-admitted operation succeeded. Do not repeat it.',
    }}
    saved = client.post(journal_url, headers=headers[member], json={'action': 'append', 'entries': [receipt]})
    assert saved.status_code == 200, saved.text
    retried = client.post(journal_url, headers=headers[member], json={'action': 'append', 'entries': [receipt]})
    assert retried.status_code == 200 and retried.json() == saved.json()
    assert app.state.store.rows('SELECT item_id FROM session_items WHERE run_id=?', (run_id,)) == [
        {'item_id': 'finished-tool-receipt'},
    ]
    checkpoint = native_exchange(client, run_id, 'commit', token=token)
    assert checkpoint.status_code == 200 and checkpoint.json()['saved'], checkpoint.text
    native = app.state.store.rows('SELECT encrypted FROM native_sessions WHERE run_id=?', (run_id,))[0]
    state = json.loads(app.state.security.decrypt(native['encrypted']))
    assert state['state']['transcript'] == 'private-native-marker'
    assert mission(app, root) == before
    assert client.post(f'/broker/{run_id}/context/compact', headers=headers[member], json={}).status_code == 401
    assert client.post(f'/broker/{run_id}/v1/chat/completions', headers=headers[member], json={}).status_code == 401
    assert app.state.store.rows('SELECT id FROM model_requests') == []


def test_expired_window_can_acknowledge_delivered_steering_but_cannot_deliver_new_work(broker_family):
    app, client, root, runs, headers = broker_family
    store = app.state.store
    followup, _ = store.enqueue_message(root, 'Focus on the verified findings.', 'already-delivered-steering',
                                       user_id='google:alice', send_now=True)
    url = f'/broker/{root}/control'
    delivery = client.post(url, headers=headers['root'], json={'version': 2, 'applied': []})
    assert delivery.status_code == 200, delivery.text
    assert delivery.json()['input']['id'] == followup['id']
    expire_without_worker(app, root)
    before = mission(app, root)
    receipt = client.post(url, headers=headers['root'], json={
        'version': 2, 'receipt_only': True, 'applied': [followup['id']],
    })
    assert receipt.status_code == 200 and receipt.json()['steer_message_id'] is None
    assert 'input' not in receipt.json()
    assert store.rows('SELECT status FROM messages WHERE id=?', (followup['id'],)) == [{'status': 'injected'}]
    for body in ({'version': 2, 'applied': []}, {'version': 1}):
        rejected = client.post(url, headers=headers['root'], json=body)
        assert rejected.status_code == 401, rejected.text
    assert mission(app, root) == before


@pytest.mark.parametrize('action_route', ['/agent-runtime', '/tools/call'])
def test_expired_window_saves_multipart_native_checkpoint_without_admitting_new_actions(broker_family, action_route):
    app, client, root, _, headers = broker_family
    token = headers['root']['Authorization'].removeprefix('Bearer ')
    assert native_exchange(client, root, 'begin', token=token).status_code == 200
    expire_without_worker(app, root)
    body = {'action': 'commit', 'lease': 'a' * 32, 'compatibility': 'b' * 64,
            'checkpoint': {'epoch': 'c' * 32, 'seq': 7},
            'state': {'transcript': 'already-admitted-native-state'}}
    raw = json.dumps(body).encode()
    digest = hashlib.sha256(raw).hexdigest()
    part = {'route': '/context/native', 'digest': digest, 'part': 0,
            'data': base64.b64encode(raw).decode()}
    uploaded = client.post(f'/broker/{root}/payloads', headers=headers['root'], json=part)
    assert uploaded.status_code == 200, uploaded.text
    manifest = {'moyai_payload': {'digest': digest, 'parts': 1}}
    saved = client.post(f'/broker/{root}/context/native',
                        headers={**headers['root'], 'Content-Type': CONTENT_TYPE},
                        content=seal(token, '/context/native', json.dumps(manifest).encode()))
    assert saved.status_code == 200 and saved.json()['saved'], saved.text
    computer = client.post(f'/broker/{root}/payloads', headers=headers['root'],
                           json={**part, 'route': action_route})
    assert computer.status_code == 401
    assert app.state.store.rows('SELECT DISTINCT route FROM broker_payload_parts') == [
        {'route': '/context/native'},
    ]
    assert client.post(f'/broker/{root}{action_route}', headers=headers['root'], json=manifest).status_code == 401
    assert client.post(f'/broker/{root}/context/compact', headers=headers['root'], json={}).status_code == 401
    assert client.post(f'/broker/{root}/context/maintenance', headers=headers['root'], json={}).status_code == 401


def test_parent_agent_guidance_cannot_extend_the_human_work_window(broker_family):
    app, client, root, runs, headers = broker_family
    before = mission(app, root)
    child = app.state.store.run(runs['child'])
    response = client.post(f'/broker/{root}/tools/call', headers=headers['root'], json={
        'name': 'agents_send', 'arguments': {'group_id': child['agent_group_id'],
            'child_id': child['id'], 'request_key': 'parent-guidance', 'message': 'add three days'},
    })
    assert response.status_code == 200, response.text
    saved = app.state.store.rows('SELECT input_kind,content FROM messages WHERE id=?',
                                 (response.json()['message_id'],))[0]
    assert saved == {'input_kind': 'delegation', 'content': 'add three days'}
    assert mission(app, root) == before


@pytest.mark.parametrize('fence', ['capability', 'stopped'])
def test_state_only_exception_keeps_original_capability_and_stop_fences(broker_family, fence):
    app, client, root, runs, headers = broker_family
    token = headers['root']['Authorization'].removeprefix('Bearer ')
    assert native_exchange(client, root, 'begin', token=token).status_code == 200
    expire_without_worker(app, root)
    if fence == 'capability':
        app.state.store.update_run(root, token_hash=digest('replacement-capability'))
    else:
        app.state.store.update_run(root, status='stopping')
    commit = native_exchange(client, root, 'commit', token=token)
    assert commit.status_code == 401
    journal = client.post(f'/broker/{root}/context/journal', headers=headers['root'], json={'action': 'state'})
    assert journal.status_code == 401
    receipt = client.post(f'/broker/{root}/control', headers=headers['root'], json={
        'version': 2, 'receipt_only': True, 'applied': [],
    })
    assert receipt.status_code == 401
    native = app.state.store.rows('SELECT encrypted FROM native_sessions WHERE run_id=?', (root,))[0]
    assert native['encrypted'] == ''
