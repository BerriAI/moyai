"""Active-turn guidance must stay in one task and survive duplicate delivery/recovery."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db import Store
from app.temporal_runtime import TemporalRunManager
from sandbox.broker_relay import BrokerRelay
from sandbox.broker_transport import unseal
from test_durable import durable, drive  # noqa: F401
from app.message_queue import MessageQueue
from app.security import digest
from sandbox.continuation import ActiveTurnSteering
from test_message_queue import queue, change  # noqa: F401
from test_workspace import workspace  # noqa: F401
from test_attachments import upload


async def checkpoint_wait(durable, phase):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='checkpointed')
    state = manager.state(run_id)
    await manager.cleanup(state, run_id)
    state.update(phase=phase, sandbox_id='', segment=1, cursor=0)
    state.pop('result', None)
    if phase == 'waiting_children':
        state['wait_group'] = 'existing-workers'
        handed_off = False
        def handoff(*args):
            nonlocal handed_off
            handed_off = True
            return True
        manager.coordinator = SimpleNamespace(results=lambda *a: {
            'group_id':'existing-workers', 'settled':handed_off, 'children':[{'summary':'Worker results'}]},
            pending_group=lambda *a: None if handed_off else 'existing-workers',
            group=lambda *a: None, settled=lambda *a: True, handoff=handoff,
            cancel_children=AsyncMock())
    else:
        state['wait_credential'] = 'existing-key-request'
        manager.credentials = SimpleNamespace(resolution=lambda *a: {'status':'pending'})
    manager.save(run_id, state)
    manager.store.update_run(run_id, status=phase, pending_result='', summary='', token_hash='')
    return state['message_id']


@pytest.mark.parametrize('phase', ['waiting_children', 'waiting_credential'])
async def test_checkpointed_steering_resumes_same_turn_and_survives_worker_restarts(durable, phase):
    manager, cloud, run_id = durable
    original = await checkpoint_wait(durable, phase)
    published = manager.store.messages(run_id)[1]
    assert published['role'] == 'assistant' and published['status'] == 'saving'
    assert published['response_to_id'] == original
    target, _ = manager.store.enqueue_message(run_id, 'What is the status? Continue the task.', 'status-during-wait')
    manager.message_queue.change(run_id, target['id'], '', False, 0, 'steer')
    await manager.advance(run_id)
    state = manager.state(run_id)
    assert state['phase'] == 'provision' and state['message_id'] == original
    assert state['snapshot_id'] == 'im-1' and state['segment'] == 1
    assert len(cloud.launches) == 1
    # Lose the worker before restoring the machine: pending delivery is durable.
    successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    successor.coordinator, successor.credentials = manager.coordinator, manager.credentials
    await drive(successor, run_id, phase='monitor')
    assert cloud.machines[1].spec['continuation'] is True
    if phase == 'waiting_children':
        assert cloud.machines[1].spec['agent_results']['settled'] is False
    else:
        assert cloud.machines[1].spec['credential_resolution']['status'] == 'pending'
    packet = successor.message_queue.live_control(run_id, original, [])
    assert packet['input']['id'] == target['id']
    assert packet['input']['content'].startswith('What is the status?')
    assert successor.store.run(run_id)['active_message_id'] == original
    successor.message_queue.live_control(run_id, original, [target['id']])
    # Lose the worker after native delivery too. Worker results still need a
    # handoff and another coordinator segment before the task can finish.
    last = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    last.coordinator, last.credentials = manager.coordinator, manager.credentials
    await drive(last, run_id)
    messages = last.store.messages(run_id)
    assert [m['id'] for m in messages] == [original, published['id'], target['id']]
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert all(m['status'] == 'completed' for m in messages)
    assert messages[2]['steering_parent_id'] == original
    assert messages[1]['content'] == 'Saved answer'
    assert messages[1]['response_to_id'] == original
    assert messages[1]['created_at'] == published['created_at']
    expected_segments = 3 if phase == 'waiting_children' else 2
    assert len(cloud.launches) == len(cloud.machines) == expected_segments
    assert len(last.store.rows("SELECT id FROM events WHERE run_id=? AND message='Response started'", (run_id,))) == 1
    if manager.coordinator:
        manager.coordinator.cancel_children.assert_not_awaited()


async def test_checkpointed_steering_waits_for_capacity_without_acknowledging_or_finishing(durable):
    manager, cloud, run_id = durable
    original = await checkpoint_wait(durable, 'waiting_children')
    published = manager.store.messages(run_id)[1]
    assert published['role'] == 'assistant' and published['response_to_id'] == original
    target, _ = manager.store.enqueue_message(run_id, 'Status?', 'waiting-capacity')
    manager.message_queue.change(run_id, target['id'], '', False, 0, 'steer')
    manager.make_capacity = AsyncMock(return_value=False)
    assert await manager.advance(run_id) == 'capacity'
    assert manager.state(run_id)['phase'] == 'waiting_children'
    messages = manager.store.messages(run_id)
    assert [m['id'] for m in messages] == [original, published['id'], target['id']]
    assert [m['status'] for m in messages] == ['running', 'saving', 'queued']
    assert messages[1] == published
    assert messages[2]['started_at'] == ''
    assert manager.store.run(run_id)['active_message_id'] == original
    assert manager.store.claim_message(run_id) is None
    assert len(cloud.launches) == 1
    manager.make_capacity = AsyncMock(return_value=True)
    await manager.advance(run_id)
    assert manager.state(run_id)['message_id'] == original
    assert manager.state(run_id)['phase'] == 'provision'


def test_only_checkpointed_control_can_offer_a_waiting_input(queue):
    app, client, run_id, first, target, user = queue
    change(client, run_id, target, 'steer')
    app.state.store.update_run(run_id, status='waiting_children')
    control = MessageQueue(app.state.store)
    assert 'input' not in control.live_control(run_id, first, [])
    assert control.live_control(run_id, first, [], checkpointed=True)['input']['id'] == target
    assert app.state.store.messages(run_id)[1]['status'] == 'queued'


def control(app, client, run_id, applied=()):
    app.state.store.update_run(run_id, token_hash=digest('active-turn-capability'))
    return client.post(f'/broker/{run_id}/control', headers={'Authorization': 'Bearer active-turn-capability'},
                       json={'version': 2, 'applied': list(applied)})


def test_native_receipt_keeps_root_turn_user_model_and_has_only_one_final_answer(queue):
    app, client, run_id, first, message, user = queue
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run_id,))
    assert change(client, run_id, message, 'steer').status_code == 200
    packet = control(app, client, run_id).json()
    assert packet['input']['id'] == message and packet['input']['content'] == 'Queued request'
    assert control(app, client, run_id).json() == packet  # Lost poll response: stable delivery.
    assert change(client, run_id, message, 'delete', 1).status_code == 409
    original = app.state.store.run(run_id)
    assert control(app, client, run_id, [message]).json()['steer_message_id'] is None
    assert control(app, client, run_id, [message]).status_code == 200
    run = app.state.store.run(run_id)
    assert run['active_message_id'] == first and run['active_user_id'] == user
    assert run['active_model'] == original['active_model']
    assert not app.state.store.has_queued_messages(run_id)
    assert [m['status'] for m in app.state.store.messages(run_id)] == ['running', 'injected']
    assert len(app.state.store.rows("SELECT * FROM events WHERE run_id=? AND message='Your message is guiding the current task.'", (run_id,))) == 1
    app.state.store.finish_message(run_id, first, 'Original objective completed with the correction')
    messages = app.state.store.messages(run_id)
    assert [m['role'] for m in messages] == ['user', 'user', 'assistant']
    assert messages[1]['steering_parent_id'] == first
    assert all(m['status'] == 'completed' for m in messages)
    assert not app.state.store.claim_message(run_id)


def test_native_guidance_never_changes_personal_scope_or_model(queue):
    app, client, run_id, first, message, user = queue
    q = MessageQueue(app.state.store)
    for field, value in [('user_id', 'someone-else'), ('model', 'other-model')]:
        with app.state.store.connect() as conn:
            conn.execute(f'UPDATE messages SET {field}=? WHERE id=?', (value, message))
            conn.execute('UPDATE runs SET steer_message_id=? WHERE id=?', (message, run_id))
        packet = q.live_control(run_id, first, [])
        assert packet == {'steer_message_id': message, 'handoff': True}
        assert app.state.store.messages(run_id)[1]['steering_parent_id'] is None
        app.state.store.execute('UPDATE messages SET user_id=? WHERE id=?', (user, message))


def test_unaccepted_racing_input_returns_to_queue_when_turn_finishes(queue):
    app, client, run_id, first, message, user = queue
    q = MessageQueue(app.state.store)
    change(client, run_id, message, 'steer')
    assert q.live_control(run_id, first, [])['input']['id'] == message
    app.state.store.finish_message(run_id, first, 'Finished before the redirect arrived')
    assert app.state.store.messages(run_id)[-1]['steering_parent_id'] is None
    assert app.state.store.claim_message(run_id)['id'] == message


def test_failed_or_stopped_turn_retains_guidance_without_replaying_it(queue):
    app, client, run_id, first, message, user = queue
    q = MessageQueue(app.state.store)
    change(client, run_id, message, 'steer')
    q.live_control(run_id, first, [])
    q.acknowledge(run_id, first, [message])
    app.state.store.finish_message(run_id, first, 'Stopped safely', 'cancelled')
    assert [m['status'] for m in app.state.store.messages(run_id)] == ['cancelled'] * 3
    assert not app.state.store.has_queued_messages(run_id)


def test_only_offered_current_turn_receipts_are_accepted(queue):
    app, client, run_id, first, message, user = queue
    q = MessageQueue(app.state.store)
    q.acknowledge(run_id, first, [message])
    assert app.state.store.messages(run_id)[1]['status'] == 'queued'
    change(client, run_id, message, 'steer');q.live_control(run_id, first, [])
    q.acknowledge(run_id, first + 999, [message])
    assert app.state.store.messages(run_id)[1]['status'] == 'queued'


def test_steering_attachment_download_and_image_context_do_not_expose_other_queued_files(queue):
    app, client, run_id, first, earlier, user = queue
    image = upload(client).json()
    target = client.post(f'/api/runs/{run_id}/messages', json={'content':'Use this screenshot', 'client_id':'steering-image', 'attachment_ids':[image['id']], 'send_now':True}).json()['id']
    q = MessageQueue(app.state.store)
    run = app.state.store.run(run_id)
    packet = q.live_control(run_id, first, [])['input']
    assert packet['attachments'][0]['id'] == image['id']
    assert image['id'] in packet['content']
    assert app.state.store.attachments.broker_file(run, image['id']).status_code == 200
    text = [{'role':'user','content':packet['content']}]
    assert isinstance(app.state.store.attachments.with_images(run,text)[0]['content'],str)
    q.acknowledge(run_id, first, [target])
    # upload()'s fixture may be a text file; use the preview record to prove image gating.
    app.state.store.execute('UPDATE attachments SET preview=? WHERE id=?', (b'image-preview',image['id']))
    assert app.state.store.attachments.with_images(run,text)[0]['content'][1]['type'] == 'image_url'


def test_runtime_native_redirect_is_idempotent_and_accepts_successive_corrections():
    packet = {'input': {'id': 1, 'content':'Keep the task; also check caching'}}
    posted, delivered = [], []
    def poll(body):
        posted.append(body)
        return packet
    steer = ActiveTurnSteering(SimpleNamespace(control=poll))
    agent = SimpleNamespace(redirect=lambda text: delivered.append(text) or True, steer=lambda text: delivered.append(text) or True)
    steer._poll(agent, boundary=False)
    steer._poll(agent, boundary=False)
    assert len(delivered) == 1 and posted[-1]['applied'] == [1]
    assert not steer.requested  # No end-of-turn interruption.
    with steer.model_wait() as generation:
        packet['input'] = {'id':2,'content':'Use a smaller sample'}
        steer._poll(agent, boundary=False)
        assert steer.cancelled(generation)
    with steer.model_wait() as generation:
        assert not steer.cancelled(generation)
    assert steer.receipts() == [1,2]


def test_native_monitor_polls_during_tools_and_handoff_waits_for_safe_boundary():
    target = {'input': {'id':1,'content':'Report progress, then continue'}}
    delivered = threading.Event()
    steer = ActiveTurnSteering(SimpleNamespace(control=lambda body: target))
    agent = SimpleNamespace(redirect=lambda text: delivered.set() or True, interrupt=lambda: None)
    steer.listen(agent)
    try:
        assert delivered.wait(2)  # Native redirect can yield supported tools.
    finally:
        steer.close()
    other = ActiveTurnSteering(SimpleNamespace(control=lambda body: {'steer_message_id':8}))
    other._poll(agent,boundary=False)
    assert not other.requested
    other.step(agent)
    assert other.requested


async def test_temporal_restart_reconciles_final_receipt_without_starting_a_new_turn(durable):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='monitor')
    root = manager.state(run_id)['message_id']
    active = manager.store.run(run_id)
    message,_ = manager.store.enqueue_message(run_id, 'Also verify caching', 'native-correction', user_id=active['active_user_id'], model=active['active_model'], send_now=True)
    packet = manager.message_queue.live_control(run_id, root, [])
    assert packet['input']['id'] == message['id']
    # Native redirect succeeded, but its HTTP acknowledgment was lost during redeploy.
    cloud.machines[0].operations[cloud.launches[0]] = {'kind':'final','message':'Finished original objective and caching','completed':True,'steering_applied':[message['id']]}
    successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    await drive(successor, run_id, phase='idle')
    assert len(cloud.launches) == 1 and cloud.snapshots == 1
    transcript = successor.store.messages(run_id)
    assert [m['role'] for m in transcript] == ['user','user','assistant']
    assert all(m['status'] == 'completed' for m in transcript)
    assert transcript[1]['steering_parent_id'] == root
    assert not successor.store.has_queued_messages(run_id)


@pytest.mark.parametrize('old_status', [200, 502])
def test_superseded_relay_reply_cannot_erase_or_replace_current_generation_error(old_status):
    entered, release = threading.Event(), threading.Event()
    received = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(unseal('token', self.path, self.rfile.read(int(self.headers['Content-Length']))))
            received.append(body)
            if body['attempt'] == 'old':
                entered.set()
                assert release.wait(5)
                status, message = old_status, 'obsolete result'
            else:
                status, message = 502, 'current failure'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'message': message}}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token').start()
    relay.steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: {'input': {'id':7,'content':'Correction'}}))
    def request(attempt):
        try:
            return httpx.post(relay.url + '/v1/chat/completions', json={'attempt':attempt}, headers={'Authorization':'Bearer token'}, timeout=5)
        except httpx.RemoteProtocolError:
            return None  # The cancelled request no longer has a consumer.
    try:
        with ThreadPoolExecutor() as pool:
            old = pool.submit(request, 'old')
            assert entered.wait(3)
            relay.steering._poll(SimpleNamespace(redirect=lambda text: True), boundary=False)
            response = request('new')
            assert response.status_code == 502 and relay.last_error == 'current failure'
            release.set()
            old.result(timeout=3)
            assert relay.last_error == 'current failure'
            assert [body['steering_applied'] for body in received] == [[], [7]]
    finally:
        release.set()
        relay.close()
        server.shutdown()
        server.server_close()


def test_background_handoff_is_not_reported_as_command_completion():
    from sandbox.activity import ActivityReporter
    events = []
    activity = ActivityReporter(lambda kind, message, data: events.append(data))
    activity.start('one', 'terminal', {'command':'test-command'})
    activity.complete('one', 'terminal', {}, json.dumps({'status':'yielded_to_background','exit_code':None,'session_id':'process-one'}))
    assert events[-1]['phase'] == 'backgrounded'
    assert 'exit_code' not in events[-1]
