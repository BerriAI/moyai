import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.db import Store
from app.message_queue import MessageQueue
from app.security import digest
from sandbox.continuation import AgentSteer
from test_attachments import upload
from test_durable import durable, drive  # noqa: F401
from test_slack import slack_app  # noqa: F401
from test_slack_web_mirror import mirror, start, web, drain  # noqa: F401
from test_spend import sign_in
from test_workspace import workspace  # noqa: F401


@pytest.fixture
def queue(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    user = sign_in(app, client)
    run_id = client.post('/api/runs', json={'prompt':'Initial request'}).json()['id']
    first = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    message = client.post(f'/api/runs/{run_id}/messages', json={'content':'Queued request', 'client_id':'queue-message'}).json()
    return app, client, run_id, first['id'], message['id'], user


def change(client, run_id, message_id, action, revision=0, **kwargs):
    return client.patch(f'/api/runs/{run_id}/messages/{message_id}', json={'action':action, 'revision':revision, **kwargs})


def test_edit_and_delete_are_revision_checked_and_preserve_receipts(queue):
    app, client, run_id, first, message, user = queue
    assert change(client,run_id,message,'edit',content='Updated request').json()['revision'] == 1
    assert change(client,run_id,message,'edit',content='Stale edit').status_code == 409
    assert change(client,run_id,message,'delete').status_code == 409
    assert change(client,run_id,message,'delete',1).json()['status'] == 'deleted'
    assert all(row['id'] != message for row in client.get('/api/runs/'+run_id).json()['messages'])
    assert app.state.store.rows('SELECT status FROM messages WHERE id=?',(message,))[0]['status'] == 'deleted'
    retry = client.post(f'/api/runs/{run_id}/messages',json={'content':'Updated request','client_id':'queue-message'})
    assert retry.json()['created'] is False and retry.json()['status']=='deleted'
    assert not app.state.store.has_queued_messages(run_id)
    assert app.state.store.run(run_id)['status']=='running'


def test_members_only_edit_their_own_pending_messages_and_started_messages_are_immutable(queue):
    app,client,run_id,first,message,user=queue
    sign_in(app,client,'bob','bob@berri.ai')
    for action in ('edit','delete','steer'):
        assert change(client,run_id,message,action,content='replacement').status_code==403
    own=client.post(f'/api/runs/{run_id}/messages',json={'content':'Bob queued','client_id':'bob-pending'}).json()['id']
    assert change(client,run_id,own,'edit',content='Bob changed').status_code==200
    assert change(client,run_id,first,'delete').status_code==403
    sign_in(app,client)
    assert change(client,run_id,first,'delete').status_code==409
    assert change(client,run_id,message,'edit',content='   ').status_code==422
    assert client.patch(f'/api/runs/{run_id}/messages/{message}',json={'action':'delete','revision':0},headers={'X-CSRF-Token':''}).status_code==403


def test_cancelled_unstarted_messages_keep_chronological_position(tmp_path):
    store=Store(tmp_path);run=store.create_run('First','','demo',[],chat_enabled=True,user_id='owner')
    first=store.claim_message(run['id'])
    store.enqueue_message(run['id'],'Cancelled before start','cancel-me',user_id='owner')
    store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'",(run['id'],))
    store.execute("UPDATE messages SET status='completed' WHERE id=?",(first['id'],))
    store.execute("UPDATE runs SET status='idle' WHERE id=?",(run['id'],))
    store.enqueue_message(run['id'],'Later request','later',user_id='owner')
    store.enqueue_message(run['id'],'Still queued','pending',user_id='owner')
    later=store.claim_message(run['id'])
    order=[m['content'] for m in store.messages(run['id'])]
    assert later['content']=='Later request' and first['content']=='First'
    assert order==['First','Cancelled before start','Later request','Still queued']


def test_edit_race_with_claim_never_changes_inflight_prompt(tmp_path):
    store=Store(tmp_path);run=store.create_run('Queued old text','','demo',[],chat_enabled=True,user_id='owner')
    message=store.messages(run['id'])[0];queue=MessageQueue(store)
    def edit():
        try: return queue.change(run['id'],message['id'],'owner',False,0,'edit','Updated text')
        except HTTPException as exc: assert exc.status_code==409;return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        edited=pool.submit(edit);claimed=pool.submit(store.claim_message,run['id'])
    claimed=claimed.result();edited=edited.result()
    assert claimed['content']==('Updated text' if edited else 'Queued old text')
    assert store.messages(run['id'])[0]['content']==claimed['content']


def test_steering_locks_only_at_safe_boundary_and_reorders_without_losing_earlier_queue(queue):
    app,client,run_id,first,earlier,user=queue
    target=client.post(f'/api/runs/{run_id}/messages',json={'content':'Urgent change','client_id':'steer-later','send_now':True}).json()['id']
    control=MessageQueue(app.state.store)
    assert app.state.store.run(run_id)['steer_message_id']==target
    assert change(client,run_id,target,'edit',0,content='Urgent edited').status_code==200
    assert control.accept_steer(run_id,first)==target
    assert change(client,run_id,target,'delete',1).status_code==409
    assert change(client,run_id,earlier,'steer').status_code==409
    before=len(app.state.store.messages(run_id))
    assert client.post(f'/api/runs/{run_id}/messages',json={'content':'Another urgent request','client_id':'other-urgent','send_now':True}).status_code==409
    assert len(app.state.store.messages(run_id))==before
    app.state.store.finish_message(run_id,first,'Paused','steered')
    assert app.state.store.claim_message(run_id)['id']==target
    assert app.state.store.run(run_id)['active_user_id']==user
    app.state.store.finish_message(run_id,target,'Urgent answer')
    assert app.state.store.claim_message(run_id)['id']==earlier
    transcript=app.state.store.messages(run_id)
    assert [m['content'] for m in transcript]==['Initial request','Urgent edited','Urgent answer','Queued request']
    context=app.state.manager.spec({**app.state.store.run(run_id),'message_id':earlier})['history_fallback']
    assert any(m['content']=='Urgent answer' for m in context)
    assert all(m['content']!='Queued request' for m in context)


def test_queued_or_deleted_attachments_never_enter_another_turn(queue):
    app,client,run_id,first,earlier,user=queue
    file=upload(client).json()
    target=client.post(f'/api/runs/{run_id}/messages',json={'content':'Read this later','client_id':'queue-file','attachment_ids':[file['id']]}).json()['id']
    assert not app.state.store.attachments.for_run(run_id,first)
    assert len(app.state.store.attachments.for_run(run_id,target))==1
    assert change(client,run_id,target,'edit',content='Read this updated request').status_code==200
    assert client.get('/api/runs/'+run_id).json()['messages'][-1]['attachments'][0]['id']==file['id']
    assert change(client,run_id,target,'delete',1).status_code==200
    assert not app.state.store.attachments.for_run(run_id,target)


def test_control_requires_the_current_sandbox_capability_and_locks_only_requested_messages(queue):
    app,client,run_id,first,message,user=queue
    app.state.store.execute("UPDATE runs SET mode='modal',token_hash=? WHERE id=?",(digest('test-current-capability'),run_id))
    url=f'/broker/{run_id}/control'
    assert client.post(url,json={}).status_code==401
    headers={'Authorization':'Bearer test-current-capability'}
    assert client.post(url,json={},headers=headers).json()['steer_message_id'] is None
    assert change(client,run_id,message,'steer').status_code==200
    assert client.post(url,json={},headers=headers).json()['steer_message_id']==message
    assert change(client,run_id,message,'edit',1,content='Too late').status_code==409
    app.state.store.update_run(run_id,token_hash='')
    assert client.post(url,json={},headers=headers).status_code==401


def test_steer_callback_waits_for_complete_tools_before_authorizing_checkpoint():
    calls=[];steer=AgentSteer(SimpleNamespace(control=lambda:{'steer_message_id':42}))
    steer.step(SimpleNamespace(interrupt=lambda:calls.append('interrupt')))
    assert calls==['interrupt'] and steer.message_id==42
    pending={'interrupted':True,'messages':[{'role':'assistant','tool_calls':[{'id':'write'}]}]}
    assert not steer.can_continue(pending)
    pending['messages'].append({'role':'tool','tool_call_id':'write'})
    assert steer.can_continue(pending)


async def test_durable_steering_checkpoints_then_switches_user_and_model_after_restart(durable):
    manager,cloud,run_id=durable
    await drive(manager,run_id,phase='monitor');old=manager.state(run_id)['message_id']
    later,_=manager.store.enqueue_message(run_id,'Other queued','other',user_id='other')
    target,_=manager.store.enqueue_message(run_id,'Steer me','urgent',model='new-model',user_id='next-user')
    manager.message_queue.change(run_id,target['id'],'next-user',False,0,'steer')
    assert manager.message_queue.accept_steer(run_id,old)==target['id']
    machine=cloud.machines[0];key=cloud.launches[0]
    machine.operations[key]={'kind':'final','message':'Pausing','completed':False,'steer_message_id':target['id']}
    await drive(manager,run_id,phase='checkpointed')
    assert cloud.snapshots==1
    from app.temporal_runtime import TemporalRunManager
    successor=cloud.attach(TemporalRunManager(Store(manager.settings.data_dir),manager.settings))
    successor.coordinator=SimpleNamespace(cancel_children=AsyncMock())
    await drive(successor,run_id,phase='idle')
    successor.coordinator.cancel_children.assert_not_awaited()
    assert successor.store.rows('SELECT status FROM messages WHERE id=?',(old,))[0]['status']=='steered'
    assert successor.store.rows('SELECT status FROM messages WHERE id=?',(later['id'],))[0]['status']=='queued'
    # Admission changes the requester/model only after the old checkpoint is durable.
    await successor.advance(run_id)
    row=successor.store.run(run_id)
    assert row['active_message_id']==target['id'] and row['active_user_id']=='next-user' and row['active_model']=='new-model'


async def test_failed_steering_save_never_starts_queued_message(durable):
    manager,cloud,run_id=durable
    await drive(manager,run_id,phase='monitor');old=manager.state(run_id)['message_id']
    target,_=manager.store.enqueue_message(run_id,'Urgent','urgent-request')
    manager.message_queue.change(run_id,target['id'],'',False,0,'steer');manager.message_queue.accept_steer(run_id,old)
    cloud.machines[0].operations[cloud.launches[0]]={'kind':'final','message':'Pausing','completed':False,'steer_message_id':target['id']}
    cloud.save_failures=3
    for _ in range(20):
        try: await manager.advance(run_id)
        except TimeoutError: pass
        if manager.state(run_id)['phase']=='idle': break
    assert len(cloud.launches)==1
    assert manager.store.rows('SELECT status FROM messages WHERE id=?',(target['id'],))[0]['status']=='cancelled'


@pytest.mark.parametrize('phase',['waiting_children','waiting_credential'])
@pytest.mark.parametrize('scope', [{'user_id':'teammate'}, {'model':'other-model'}])
async def test_cross_scope_checkpointed_steering_keeps_the_saved_handoff(durable,phase,scope):
    manager,cloud,run_id=durable
    await drive(manager,run_id,phase='checkpointed')
    state=manager.state(run_id);old=state['message_id']
    await manager.cleanup(state, run_id)
    state.update(phase=phase,sandbox_id='');manager.save(run_id,state)
    manager.store.update_run(run_id,status=phase)
    target,_=manager.store.enqueue_message(run_id,'Continue with this instead','steer-wait',**scope)
    manager.message_queue.change(run_id,target['id'],scope.get('user_id',''),False,0,'steer')
    await drive(manager,run_id,phase='idle')
    assert manager.store.rows('SELECT status FROM messages WHERE id=?',(old,))[0]['status']=='steered'
    assert len(cloud.launches)==1 and cloud.snapshots==1
    assert not [m for m in manager.store.messages(run_id) if m['role']=='assistant']
    assert manager.store.has_queued_messages(run_id)


def test_slack_edits_and_deletes_retire_pending_inputs_and_correct_delivered_inputs(mirror):
    app,client,run_id=start(mirror)
    target=web(app,client,run_id).json()['id']
    assert change(client,run_id,target,'edit',content='New queued version').status_code==200
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE dedupe_key=?",(f'input:{target}:0',))[0]['status']=='skipped'
    drain(app)
    assert any('New queued version' in post.get('text','') for post in mirror[3])
    assert not any('Continue from web' in post.get('text','') for post in mirror[3])
    assert change(client,run_id,target,'delete',1).status_code==200
    drain(app)
    assert any('removed in Moyai' in post.get('text','') for post in mirror[3])
