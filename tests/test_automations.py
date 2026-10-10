import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy

from app.automations import Automations, Definition, Save, Timing
from test_workspace import workspace, cloud_capability
from test_spend import sign_in


def create(client, **definition):
    response = client.post('/api/automations', json={'definition': {'name':'Check tickets', 'prompt':'Read my tickets and prepare a fix', 'mode':'demo', **definition}})
    assert response.status_code == 201, response.text
    return response.json()


def test_drafts_require_explicit_enable_and_keep_identity(workspace, monkeypatch):
    app, client = workspace
    owner = sign_in(app, client)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    automation = create(client)
    assert automation['paused'] and automation['owner_id'] == owner
    assert client.post(f"/api/automations/{automation['id']}/state",json={'revision':1,'paused':False}).status_code == 409
    path=f"/api/automations/{automation['id']}/run"
    first=client.post(path,json={'revision':1,'client_id':'one-occurrence'}).json()
    again=client.post(path,json={'revision':1,'client_id':'one-occurrence'}).json()
    assert again==first and first['outcome']=='started'
    run=app.state.store.run(first['run_id'])
    assert run['owner_id']==run['active_user_id']==owner
    assert app.state.store.messages(run['id'])[0]['user_id']==owner
    assert run['chat_enabled'] and run['agent_label']=='Automation · Check tickets'
    blocked=client.post(path,json={'revision':1,'client_id':'another-occurrence'}).json()
    assert blocked['outcome']=='started' and blocked['run_id'] != run['id']
    app.state.store.update_run(run['id'],status='idle')
    # Pending input on an earlier session does not block an independent occurrence.
    assert client.post(path,json={'revision':1,'client_id':'third-occurrence'}).json()['outcome']=='started'
    message=app.state.store.claim_message(run['id'])
    app.state.store.finish_message(run['id'],message['id'],'Finished with PR link','completed')
    app.state.store.update_run(run['id'],status='idle',summary='Finished with PR link')
    next_run=client.post(path,json={'revision':1,'client_id':'fourth-occurrence'}).json()
    assert next_run['run_id'] != run['id']
    assert 'Finished with PR link' in app.state.store.run(next_run['run_id'])['prompt']
    app.state.session_lifecycle.request_delete(run['id'], owner, False)
    history = {row['run_id']: row for row in client.get('/api/automations').json()['automations'][0]['history']}
    assert history[run['id']]['status'] == 'deleting' and history[run['id']]['outcome'] == 'started'


def test_ownership_csrf_revision_and_admin_pause(workspace, monkeypatch):
    app, client=workspace
    owner=sign_in(app,client)
    a=create(client)
    path='/api/automations/'+a['id']
    assert client.post(path+'/run',json={'revision':1,'client_id':'one-request'},headers={'X-CSRF-Token':'wrong'}).status_code==403
    sign_in(app,client,'bob','bob@berri.ai')  # helper makes Alice the administrator
    app.state.settings.google_admin_emails='bob@berri.ai'
    assert client.put(path,json={'revision':1,'definition':a['definition']}).status_code==403
    assert client.post(path+'/run',json={'revision':1,'client_id':'one-request'}).status_code==403
    assert client.post(path+'/state',json={'revision':1,'paused':False}).status_code==403
    assert client.post(path+'/state',json={'revision':1,'paused':True}).status_code==200
    sign_in(app,client)
    assert client.put(path,json={'revision':1,'definition':a['definition']}).status_code==409
    edit=client.put(path,json={'revision':2,'definition':a['definition']})
    assert edit.status_code==200 and edit.json()['paused']
    client.cookies.clear()
    assert client.get('/api/automations').status_code==401


def test_timing_and_repository_validation(workspace):
    app,client=workspace
    for timing in [{'timezone':'../../bad'}, {'time':'25:17'}, {'frequency':'every-second'}, {'weekday':7}]:
        assert client.post('/api/automations',json={'definition':{'name':'Job','prompt':'Do work','timing':timing}}).status_code==422
    assert client.post('/api/automations',json={'definition':{'name':'Job','prompt':'Do work','repo_url':'https://evil.example/repo'}}).status_code==422
    assert client.post('/api/automations',json={'definition':{'name':'Job','prompt':'Do work','owner_id':'someone'}}).status_code==422
    a=create(client,timing={'frequency':'weekdays','time':'09:30','timezone':'America/Los_Angeles'})
    schedule=app.state.automations.schedule(app.state.automations.row(a['id']))
    assert schedule.policy.overlap==ScheduleOverlapPolicy.ALLOW_ALL
    assert schedule.policy.catchup_window==timedelta(minutes=15)
    assert schedule.spec.time_zone_name=='America/Los_Angeles'
    assert schedule.spec.calendars[0].day_of_week[0].start==1
    assert schedule.spec.calendars[0].day_of_week[0].end==5
    assert schedule.state.paused
    assert len(schedule.action.args)==2 and a['definition']['prompt'] not in str(schedule.action.args)


async def test_outbox_retries_and_updates_schedule_without_losing_saved_change(workspace):
    app,client=workspace
    a=create(client)
    service=app.state.automations
    class Remote:
        failed=True
        existing=False
        values=[]
        async def create_schedule(self,identity,schedule,**kwargs):
            if self.failed: raise ConnectionError('sensitive provider diagnostics')
            if self.existing: raise ScheduleAlreadyRunningError()
            self.existing=True;self.values.append(schedule)
        def get_schedule_handle(self,identity):return self
        async def update(self,fn,**kwargs):self.values.append(fn(None).schedule)
    remote=Remote()
    await service.sync(remote)
    assert service.row(a['id'])['synced_revision']==0
    assert 'sensitive' not in service.row(a['id'])['sync_error']
    remote.failed=False;service.next_sync=0
    await service.sync(remote)
    assert service.row(a['id'])['synced_revision']==1
    service.store.execute('UPDATE automations SET revision=2,paused=0 WHERE id=?',(a['id'],));service.next_sync=0
    await service.sync(remote)
    assert service.row(a['id'])['synced_revision']==2 and not remote.values[-1].state.paused


async def test_stale_schedule_and_downtime_do_not_launch_old_work(workspace, monkeypatch):
    app,client=workspace
    a=create(client)
    service=app.state.automations
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert (await service.launch(a['id'],1,'paused-occurrence'))['outcome']=='skipped'
    service.store.execute('UPDATE automations SET paused=0,revision=2 WHERE id=?',(a['id'],))
    assert (await service.launch(a['id'],1,'old-version'))['outcome']=='skipped'
    expired=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    assert (await service.launch(a['id'],2,'late',expired))['outcome']=='skipped'
    from app.temporal_runtime import TemporalRunManager
    TemporalRunManager(app.state.store, app.state.settings)
    app.state.settings.temporal_enabled=True
    # The run and wake are committed together even if dispatch has not happened.
    result=await service.launch(a['id'],2,'on-time')
    assert result['run_id'] and service.store.rows('SELECT * FROM durable_sessions WHERE run_id=?',(result['run_id'],))
    assert (await service.launch(a['id'],2,'on-time'))==result
    assert len(service.store.rows('SELECT * FROM runs'))==1


async def test_retry_flushes_receipt_after_checkpoint_failure(workspace,monkeypatch):
    app,client=workspace
    # Keep unrelated Slack flushes from consuming the injected failure.
    client.portal.call(app.state.slack.chat.shutdown)
    a=create(client)
    service=app.state.automations
    from app.temporal_runtime import TemporalRunManager
    TemporalRunManager(app.state.store, app.state.settings)
    app.state.settings.temporal_enabled=True
    calls=0
    async def flush():
        nonlocal calls
        calls+=1
        if calls==1:raise ConnectionError('checkpoint temporarily unavailable')
    monkeypatch.setattr(service.checkpoints,'flush',flush)
    with pytest.raises(ConnectionError):await service.launch(a['id'],1,'retry',manual=True)
    first=service.store.rows('SELECT * FROM runs')[0]
    result=await service.launch(a['id'],1,'retry',manual=True)
    assert result['run_id']==first['id'] and calls==2
    assert len(service.store.rows('SELECT * FROM runs'))==1


def test_ticket_claims_survive_failed_runs_and_cannot_cross_automations(workspace,monkeypatch):
    app,client=workspace
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    a=create(client)
    run_id=client.post(f"/api/automations/{a['id']}/run",json={'revision':1,'client_id':'claim-first'}).json()['run_id']
    run=app.state.store.run(run_id)
    service=app.state.automations
    assert service.claim(run,{'item_key':'LIT-123'})['claimed']
    assert service.claim(run,{'item_key':'lit-123'})['claimed']
    app.state.store.update_run(run_id,status='failed')
    app.state.store.execute("UPDATE messages SET status='failed' WHERE run_id=?",(run_id,))
    second_id=client.post(f"/api/automations/{a['id']}/run",json={'revision':1,'client_id':'claim-second'}).json()['run_id']
    assert not service.claim(app.state.store.run(second_id),{'item_key':'LIT-123'})['claimed']
    unrelated=app.state.store.create_run('Other task','','demo',[])
    assert not service.tools(unrelated)
    with pytest.raises(HTTPException):service.claim(unrelated,{'item_key':'LIT-123'})


def test_missing_connection_blocks_launch_and_shows_reason(workspace):
    app,client=workspace
    a=create(client,mode='modal',plugins=['linear','github'])
    result=client.post(f"/api/automations/{a['id']}/run",json={'revision':1,'client_id':'missing-setup'}).json()
    assert result['outcome']=='blocked'
    history=client.get('/api/automations').json()['automations'][0]['history']
    assert 'Cloud setup is incomplete' in history[0]['detail']
    assert not app.state.store.rows('SELECT * FROM runs')


def test_my_linear_issues_uses_session_identity_not_connection_owner(workspace,monkeypatch):
    app,client=workspace
    user=sign_in(app,client,'tin','tin@berri.ai')
    app.state.store.identity({'method':'google','identity':{'sub':'tin','email':'tin@berri.ai','name':'Tin'}})
    run_id,headers=cloud_capability(app,['linear'])
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?',(user,user,run_id))
    sent=[]
    async def request(method,url,**kwargs):
        sent.append(kwargs['json']);return {'data':{'issues':{'nodes':[{'identifier':'LIT-123'}]}}}
    monkeypatch.setattr(app.state.connectors,'request',request)
    result=client.post('/broker/'+run_id+'/tools/call',headers=headers,json={'name':'linear_my_issues','arguments':{}})
    assert result.status_code==200 and result.json()['issues']['nodes'][0]['identifier']=='LIT-123'
    assert sent[0]['variables']=={'email':'tin@berri.ai'}
    assert 'assignee' in sent[0]['query'] and 'completed' in sent[0]['query']
    assert client.post('/broker/'+run_id+'/tools/call',headers=headers,json={'name':'linear_my_issues','arguments':{'email':'someone@berri.ai'}}).status_code==422


def test_concurrent_occurrences_admit_independent_sessions(workspace,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app,client=workspace
    a=create(client)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    def launch(i):return asyncio.run(app.state.automations.launch(a['id'],1,'concurrent-'+str(i),manual=True))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(launch,range(20)))
    assert all(r['outcome']=='started' for r in results)
    assert len({r['run_id'] for r in results})==20
    assert len(app.state.store.rows('SELECT * FROM runs'))==20
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries=list(pool.map(launch,range(20)))
    assert retries==results
    assert len(app.state.store.rows('SELECT * FROM runs'))==20


def test_owner_access_is_rechecked_at_execution(workspace):
    app,client=workspace
    sign_in(app,client)
    a=create(client)
    app.state.settings.google_allowed_domains='different.example'
    result=asyncio.run(app.state.automations.launch(a['id'],1,'owner-revoked',manual=True))
    assert result['outcome']=='blocked'
    assert not app.state.store.rows('SELECT * FROM runs')


@pytest.mark.parametrize('depth', [1, 3])
def test_active_child_does_not_block_independent_occurrences(workspace,monkeypatch,depth):
    app,client=workspace
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    a=create(client)
    root=client.post(f"/api/automations/{a['id']}/run",json={'revision':1,'client_id':'root-with-children'}).json()['run_id']
    app.state.store.update_run(root,status='idle')
    app.state.store.execute("UPDATE messages SET status='completed' WHERE run_id=?",(root,))
    parent = root
    for _ in range(depth):
        child=app.state.store.create_run('Work still running','','demo',[])
        app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(parent,child['id']))
        app.state.store.update_run(parent,status='completed')
        parent = child['id']
    assert not app.state.automations.finished(root)
    assert client.post(f"/api/automations/{a['id']}/run",json={'revision':1,'client_id':'overlap-with-children'}).json()['outcome']=='started'
    app.state.store.update_run(child['id'],status='completed')
    assert app.state.automations.finished(root)


def test_builder_metadata_and_queue_setting_round_trip(workspace):
    app, client = workspace
    a = create(client, metadata={'team': 'engineering', 'owner': '<script>demo</script>', 'notes': 'x' * 501}, queue_events=False)
    assert a['definition']['metadata']['team'] == 'engineering'
    assert a['definition']['queue_events'] is False
    notes = '界' * 16370 + '\nreview notes!'
    assert len(notes) == 16384
    saved = client.put('/api/automations/' + a['id'], json={
        'revision': a['revision'], 'definition': {**a['definition'], 'metadata': {'team': 'platform', 'notes': notes}, 'queue_events': True}})
    assert saved.status_code == 200
    current = client.get('/api/automations').json()['automations'][0]
    assert current['paused'] and current['revision'] == 2
    assert current['definition']['metadata'] == {'team': 'platform', 'notes': notes}
    assert current['definition']['queue_events'] is True
    for metadata in ({' ': 'empty'}, {'a' * 81: 'long key'}, {'team': 'x' * 16385}, {str(i): '' for i in range(21)}):
        result = client.post('/api/automations', json={'definition': {**a['definition'], 'metadata': metadata}})
        assert result.status_code == 422


def test_metadata_schema_advertises_entry_limits():
    schema = Definition.model_json_schema()['properties']['metadata']
    assert schema['maxProperties'] == 20
    assert schema['propertyNames'] == {'minLength': 1, 'maxLength': 80}
    assert schema['additionalProperties'] == {'type': 'string', 'maxLength': 16384}


def test_library_templates_are_valid_editable_workflows(workspace):
    app, client = workspace
    templates = client.get('/api/automations').json()['templates']
    assert {t['id'] for t in templates} == {'linear-pr', 'weekly-digest', 'ci-failure', 'daily-triage'}
    for template in templates:
        trigger = {'event': {**template['event'], 'repository': 'example/project'}} if template.get('event') else {'schedule': template.get('schedule', {})}
        d = Definition(name=template['name'], prompt=template['prompt'], plugins=template['plugins'], triggers=[trigger])
        assert not d.queue_events and not d.metadata
