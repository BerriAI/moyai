"""Multi-source admission, provider authentication, and schedule lifecycle contracts."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from temporalio.client import ScheduleAlreadyRunningError

from app.automations import Automations, Definition, Save, Timing, Trigger
from app.automation_events import AutomationEvents
from app.automation_sources import EventTrigger, EVENT_CHOICES, example, normalize
from app.temporal_runtime import TemporalRunManager
from test_workspace import workspace
from test_automations import create
from test_automation_events import SECRET, TEAM, USER, LABEL, complete, signed
from test_slack import slack_app, signed as slack_signed, event as slack_event


def event_trigger(provider, event, **kwargs):
    defaults = {'github':{'repository':'BerriAI/litellm','repository_id':101}, 'gitlab':{'repository':'group/project'},
                'linear':{'team_id':TEAM}, 'slack':{'channel_id':'C12345678','reaction':'eyes'}}
    return EventTrigger(provider=provider,event=event,**(defaults.get(provider,{})|kwargs))


def configure(app, client, triggers, cap=50):
    a = create(client,triggers=triggers,max_runs_per_hour=cap)
    revision = 1
    for provider in dict.fromkeys(t['event']['provider'] for t in triggers if t.get('event') and t['event']['provider']!='slack'):
        r=client.post(f"/api/automations/{a['id']}/webhook",json={'revision':revision,'provider':provider,'secret':SECRET})
        assert r.status_code==200,r.text
        revision=r.json()['revision']
    TemporalRunManager(app.state.store,app.state.settings)
    app.state.settings.temporal_enabled=True
    r=client.post(f"/api/automations/{a['id']}/state",json={'revision':revision,'paused':False})
    assert r.status_code==200,r.text
    return r.json()


def test_defaults_null_cap_and_legacy_upgrade():
    base={'name':'Test','prompt':'Do work'}
    assert Definition(**base).max_runs_per_hour==50
    slack=event_trigger('slack','message.posted').model_dump()
    assert Definition(**base,triggers=[{'event':slack}]).max_runs_per_hour==150
    assert Definition(**base,triggers=[{'event':slack}],max_runs_per_hour=17).max_runs_per_hour==17
    assert Definition(**base,max_runs_per_hour=None).max_runs_per_hour is None
    legacy=Definition(**base,event={'provider':'webhook','max_runs_per_hour':3})
    assert legacy.max_runs_per_hour==3 and legacy.triggers[0].id=='default'
    with pytest.raises(ValueError):Definition(**base,max_runs_per_hour=0)
    with pytest.raises(ValueError):Definition(**base,triggers=[{'id':'same','schedule':{}},{'id':'same','schedule':{}}])
    with pytest.raises(ValueError):Definition(**base,triggers=[{'schedule':{},'event':{'provider':'webhook'}}])


async def test_or_matching_queues_once_and_cap_is_shared_with_manual_and_schedule(workspace, monkeypatch):
    app,client=workspace
    from test_github import select, GitHubAPI
    app.state.connectors.github.save_app({'id': 123, 'pem': 'test', 'owner_id': 44})
    select(app)
    GitHubAPI(app.state.connectors.github, monkeypatch)
    a=configure(app,client,[{'id':'daily','schedule':{}},
        {'id':'first','event':{'provider':'webhook','text_contains':'bug'}},
        {'id':'second','event':{'provider':'webhook'}},
        {'id':'github','event':{'provider':'github','event':'issues','repository':'BerriAI/litellm'}}],cap=1)
    result=client.post(f"/hooks/automations/{a['id']}/webhook",**signed('webhook',{'body':'bug'},delivery='a'))
    assert result.json()['status']=='accepted'
    context=json.loads(app.state.store.rows('SELECT context FROM automation_events')[0]['context'])
    assert context['matched_trigger_ids']==['first','second']
    service=app.state.automations
    await service.events.dispatch()
    run=app.state.store.rows('SELECT * FROM runs')[0]
    complete(app,run['id'])
    assert (await service.launch(a['id'],a['revision'],'tick',trigger_id='daily'))['outcome']=='skipped'
    assert client.post(f"/api/automations/{a['id']}/run",json={'revision':a['revision'],'client_id':'manual-new'}) .json()['outcome']=='skipped'
    github={'action':'opened','repository':{'id':101,'full_name':'BerriAI/litellm'},'issue':{'number':123,'title':'Investigate'}}
    assert client.post(f"/hooks/automations/{a['id']}/github",**signed('github',github)).json()['status']=='accepted'
    await service.events.dispatch()
    assert len(app.state.store.rows('SELECT * FROM runs'))==1
    assert 'hourly' in app.state.store.rows("SELECT detail FROM automation_events WHERE status='pending'")[0]['detail']
    # Limits are independent across automations.
    b=configure(app,client,[{'id':'hook','event':{'provider':'webhook'}}],cap=None)
    for i in range(3):
        assert client.post(f"/hooks/automations/{b['id']}/webhook",**signed('webhook',{'body':'bug'},delivery=f'b{i}')).json()['status']=='accepted'
        await service.events.dispatch()
        newest=app.state.store.rows('SELECT run_id FROM automation_runs WHERE automation_id=? ORDER BY rowid DESC',(b['id'],))[0]['run_id']
        complete(app,newest)
    assert len(app.state.store.rows("SELECT 1 FROM automation_runs WHERE automation_id=? AND outcome='started'",(b['id'],)))==3
    assert len(app.state.store.rows('SELECT * FROM automation_webhooks WHERE automation_id=?',(a['id'],)))==2


@pytest.mark.parametrize('provider,event', [(p,e) for p,choices in EVENT_CHOICES.items() for e,_ in choices])
def test_catalog_examples_and_unrelated_events(provider,event):
    t=event_trigger(provider,event)
    payload=example(t)
    assert normalize(t,payload,event.split('.')[0] if provider=='github' else ''), (provider,event,payload)
    if provider!='webhook':
        assert normalize(t,{},'unrelated') is None


@pytest.mark.parametrize('provider,event,header',[
    ('gitlab','merge_request','X-Gitlab-Token'),('jira','issue.updated','X-Hub-Signature'),
    ('pagerduty','incident.triggered','X-PagerDuty-Signature'),('pylon','issue.created','Authorization'),
    ('webhook','*','X-Webhook-Secret')])
def test_native_signature_formats_reject_changes_and_deduplicate(workspace,provider,event,header):
    app,client=workspace
    t=event_trigger(provider,event)
    a=configure(app,client,[{'id':'native','event':t.model_dump()}])
    payload=example(t)
    raw=json.dumps(payload).encode()
    digest=hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
    auth={'gitlab':SECRET,'jira':'sha256='+digest,'pagerduty':'v1=invalid, v1='+digest,'pylon':'Bearer '+SECRET,'webhook':SECRET}[provider]
    path=f"/hooks/automations/{a['id']}/{provider}"
    assert client.post(path,content=raw,headers={header:'bad'}).status_code==401
    response=client.post(path,content=raw,headers={header:auth})
    assert response.status_code==202,response.text
    assert response.json()['status']=='accepted'
    assert client.post(path,content=raw,headers={header:auth,'X-GitHub-Delivery':'tampered'}).json()['status']=='duplicate'
    if provider in {'jira','pagerduty'}:
        assert client.post(path,content=raw+b' ',headers={header:auth}).status_code==401
    assert len(app.state.store.rows('SELECT * FROM automation_events'))==1


def test_gitlab_standard_webhooks_authenticates_id_and_timestamp(workspace):
    app,client=workspace
    t=event_trigger('gitlab','pipeline')
    a=configure(app,client,[{'id':'pipeline','event':t.model_dump()}])
    key=b'gitlab-standard-webhooks-test-key'
    secret='whsec_'+base64.b64encode(key).decode()
    r=client.post(f"/api/automations/{a['id']}/webhook",json={'revision':a['revision'],'provider':'gitlab','secret':secret})
    revision=r.json()['revision']
    client.post(f"/api/automations/{a['id']}/state",json={'revision':revision,'paused':False})
    raw=json.dumps(example(t)).encode();timestamp=str(int(time.time()));delivery='gl-123'
    digest=base64.b64encode(hmac.new(key,f'{delivery}.{timestamp}.'.encode()+raw,hashlib.sha256).digest()).decode()
    headers={'webhook-id':delivery,'webhook-timestamp':timestamp,'webhook-signature':'v1,old v1,'+digest}
    path=f"/hooks/automations/{a['id']}/gitlab"
    assert client.post(path,content=raw,headers=headers).json()['status']=='accepted'
    assert client.post(path,content=raw,headers=headers).json()['status']=='duplicate'
    assert client.post(path,content=raw,headers=headers|{'webhook-id':'tampered'}).status_code==401
    assert client.post(path,content=raw,headers=headers|{'webhook-timestamp':'1'}).status_code==401


def test_generic_any_payload_regex_and_explicit_delivery_id(workspace):
    app,client=workspace
    a=configure(app,client,[{'id':'regex','event':{'provider':'webhook','payload_pattern':'"priority":\\s*"high"'}}])
    path=f"/hooks/automations/{a['id']}/webhook"
    headers={'Authorization':'Bearer '+SECRET,'X-Moyai-Event-Id':'event-1'}
    assert client.post(path,json={'priority':'high','build':123},headers=headers).json()['status']=='accepted'
    assert client.post(path,json={'priority':'high','build':123},headers=headers).json()['status']=='duplicate'
    assert client.post(path,json={'priority':'high','build':123},headers=headers|{'X-Moyai-Event-Id':'event-2'}).json()['status']=='accepted'
    assert client.post(path,json={'priority':'low'},headers=headers|{'X-Moyai-Event-Id':'event-3'}).json()['status']=='ignored'
    with pytest.raises(ValueError):EventTrigger(provider='webhook',payload_pattern='(?=unsafe)')


def test_old_encrypted_webhook_migrates_without_resetting_definitions(workspace):
    app,client=workspace
    a=create(client,event={'provider':'webhook','event':'ready'})
    service=app.state.automations
    # Recreate the pre-multiple-provider schema and stored definition.
    service.store.execute('DROP TABLE automation_webhooks')
    service.store.execute('CREATE TABLE automation_webhooks(automation_id TEXT PRIMARY KEY,encrypted TEXT NOT NULL)')
    encrypted=service.security.encrypt(SECRET)
    service.store.execute('INSERT INTO automation_webhooks VALUES(?,?)',(a['id'],encrypted))
    service.store.execute('UPDATE automations SET definition=? WHERE id=?',(json.dumps({'name':'Legacy','prompt':'Do work','event':{'provider':'webhook','event':'ready','max_runs_per_hour':5}}),a['id']))
    migrated=AutomationEvents(service)
    keys=service.store.rows('SELECT * FROM automation_webhooks')
    assert len(keys)==1 and keys[0]['provider']=='webhook' and keys[0]['encrypted']==encrypted
    assert migrated.ready(service.row(a['id']))
    AutomationEvents(service)  # Idempotent on later restarts.
    assert len(service.store.rows('SELECT * FROM automation_webhooks'))==1


def test_slack_reactions_threads_bot_filters_and_own_bot_guard(slack_app):
    app,client,runs,messages=slack_app
    service=app.state.automations
    owner=app.state.store.identity({'method':'password','role':'admin'})
    a=service.save(Save(definition=Definition(name='Slack triage',prompt='Read context',mode='demo',triggers=[
        {'id':'react','event':event_trigger('slack','reaction.added')},
        {'id':'threads','event':event_trigger('slack','message.posted',include_thread_replies=True,sender_type='human',text_contains='triage')},
        {'id':'bots','event':event_trigger('slack','message.posted',sender_type='bot',text_contains='failure')} ])),owner)
    service.store.execute('UPDATE automations SET paused=0 WHERE id=?',(a['id'],))
    reaction=slack_event('React1',type='reaction_added',reaction='eyes',item={'type':'message','channel':'C12345678','ts':'1790719000.123456'})
    assert client.post('/hooks/slack/events',**slack_signed(reaction)).status_code==200
    assert client.post('/hooks/slack/events',**slack_signed(reaction)).status_code==200
    assert client.post('/hooks/slack/events',**slack_signed(slack_event('Thread1',type='message',text='triage',thread_ts='1790718000.123456'))).status_code==200
    assert client.post('/hooks/slack/events',**slack_signed(slack_event('Bot1',type='message',bot_id='B12345678',text='failure'))).status_code==200
    for payload in [slack_event('Own1',type='message',user='U99999999',text='triage'),reaction|{'event_id':'Extern1','is_ext_shared_channel':True},reaction|{'event_id':'WrongTeam','team_id':'T87654321'}]:
        assert client.post('/hooks/slack/events',**slack_signed(payload)).status_code==200
    assert len(service.store.rows('SELECT * FROM automation_events'))==3
    assert not runs and not messages


def test_provider_filters_match_real_changes_not_unrelated_updates():
    linear=event_trigger('linear','issue.status_changed',status=USER)
    payload={'type':'Issue','action':'update','data':{'id':'I1','teamId':TEAM,'stateId':USER},'updatedFrom':{'stateId':LABEL}}
    assert normalize(linear,payload)
    assert normalize(linear,payload|{'updatedFrom':{'title':'old'}}) is None
    jira=event_trigger('jira','issue.assigned',project='ENG',assignee_id='alice')
    payload={'webhookEvent':'jira:issue_updated','issue':{'key':'ENG-42','fields':{'project':{'key':'ENG'},'assignee':{'accountId':'alice'}}},'changelog':{'items':[{'field':'assignee','fromString':'Bob','toString':'Alice','to':'alice'}]}}
    assert normalize(jira,payload)
    assert normalize(jira,payload|{'changelog':{'items':[{'field':'summary','fromString':'Old','toString':'New'}]}}) is None
    gitlab=event_trigger('gitlab','note.merge_request')
    payload={'object_kind':'note','project':{'path_with_namespace':'group/project'},'object_attributes':{'id':123,'noteable_type':'MergeRequest','note':'Please fix'}}
    assert normalize(gitlab,payload)
    assert normalize(gitlab,payload|{'object_attributes':payload['object_attributes']|{'noteable_type':'Issue'}}) is None
    pd=event_trigger('pagerduty','incident.triggered',service_id='P123',urgency='high')
    assert normalize(pd,{'event':{'id':'event1','event_type':'incident.triggered','data':{'id':'incident1','service':{'id':'P123'},'urgency':'high'}}})
    assert normalize(pd,{'event':{'event_type':'incident.triggered','data':{'service':{'id':'WRONG'},'urgency':'high'}}}) is None


@pytest.mark.parametrize('cron',['* * * * *','*/15 8-18 * * 1-5','5,35 9 * 1,12 0'])
def test_cron_specs(cron):
    assert Timing(frequency='cron',cron=cron,timezone='UTC').spec().cron_expressions==[cron]


@pytest.mark.parametrize('cron',['* * *','60 * * * *','*/0 * * * *','0 24 * * *','0 9 * * 8','0 9 * * mon','0 9 20-10 * *'])
def test_invalid_cron_rejected(cron):
    with pytest.raises(ValueError):Timing(frequency='cron',cron=cron)


async def test_schedule_bindings_removed_and_one_time_cannot_replay(workspace,monkeypatch):
    app,client=workspace
    future=(datetime.now(timezone.utc)+timedelta(hours=2)).replace(microsecond=0).isoformat()
    a=create(client,triggers=[{'id':'daily','schedule':{'frequency':'daily'}},{'id':'once','schedule':{'frequency':'once','run_at':future}}])
    service=app.state.automations
    class Remote:
        schedules={}
        async def create_schedule(self,key,schedule,**kwargs):
            if key in self.schedules:raise ScheduleAlreadyRunningError()
            self.schedules[key]=schedule
        def get_schedule_handle(self,key):
            remote=self
            class Handle:
                async def update(self,fn,**kwargs):remote.schedules[key]=fn(None).schedule
                async def delete(self,**kwargs):remote.schedules.pop(key)
            return Handle()
    remote=Remote()
    await service.sync(remote)
    assert len(remote.schedules)==2
    once=remote.schedules['moyai-automation-'+a['id']+'-once']
    assert once.state.remaining_actions==1 and once.state.limited_actions
    service.store.execute('UPDATE automations SET paused=0 WHERE id=?',(a['id'],))
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    first=await service.launch(a['id'],1,'once-workflow-1',trigger_id='once')
    complete(app,first['run_id'])
    assert await service.launch(a['id'],1,'once-workflow-2',trigger_id='once')==first
    assert 'once' in service.public(service.row(a['id']),a['owner_id'])['completed_triggers']
    definition=a['definition']|{'triggers':[a['definition']['triggers'][1]]}
    assert client.put('/api/automations/'+a['id'],json={'revision':1,'definition':definition}).status_code==200
    await service.sync(remote)
    assert len(remote.schedules)==1
    once=next(iter(remote.schedules.values()))
    assert once.state.paused and once.state.remaining_actions==0
    assert len(service.store.rows('SELECT * FROM runs'))==1


def test_one_time_needs_future_timezone_and_old_delivery_does_not_consume_new_trigger(workspace,monkeypatch):
    app,client=workspace
    for stamp in ['2020-01-01T00:00:00+00:00','2030-01-01T00:00:00']:
        r=client.post('/api/automations',json={'definition':{'name':'Once','prompt':'Do work','triggers':[{'schedule':{'frequency':'once','run_at':stamp}}]}})
        assert r.status_code==422
    future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    a=create(client,triggers=[{'id':'once','schedule':{'frequency':'once','run_at':future}}])
    service=app.state.automations
    service.store.execute('UPDATE automations SET revision=2,paused=0 WHERE id=?',(a['id'],))
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert asyncio.run(service.launch(a['id'],1,'stale-once',trigger_id='once'))['outcome']=='skipped'
    assert asyncio.run(service.launch(a['id'],2,'current-once',trigger_id='once'))['outcome']=='started'


def test_ci_check_filters_use_native_check_run_fields():
    trigger=event_trigger('github','check_run',conclusion='failure',branch='main',text_contains='test failed')
    payload={'action':'completed','repository':{'id':101,'full_name':'BerriAI/litellm'},'check_run':{'id':42,'name':'CI',
        'conclusion':'failure','status':'completed','check_suite':{'head_branch':'main'},'output':{'summary':'test failed'}}}
    assert normalize(trigger,payload,'check_run')
    assert normalize(trigger,example(trigger),'check_run')
    assert normalize(trigger,payload|{'check_run':payload['check_run']|{'conclusion':'success'}},'check_run') is None
