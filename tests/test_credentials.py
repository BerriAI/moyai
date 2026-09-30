import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.credentials import CredentialRequest, Credentials, Invoke, Resolve, SaveSecret
from app.db import Store, now
from app.security import Security
from app.temporal_runtime import TemporalRunManager
from sandbox.continuation import AgentWait
from test_durable import durable, drive
from test_spend import active, sign_in
from test_workspace import workspace

KEY = 'test-provider-secret-438912'


def requested(app, run, key='bench'):
    return app.state.credentials.request(run,CredentialRequest(provider='fireworks',reason='Benchmark the requested models',request_key=key))


def save(client,scope='personal',provider='fireworks',suffix='1'):
    return client.post('/api/credentials/secrets',json={'provider':provider,'label':'Benchmark key','scope':scope,'value':KEY,'client_id':'save-test-'+suffix})


def attach(manager):
    manager.settings.temporal_enabled=True
    manager.credentials=Credentials(manager.store,Security(manager.settings),manager.settings,manager,SimpleNamespace(flush=None))
    return manager.credentials


def test_encrypted_storage_metadata_only_and_identity_bound_permissions(workspace):
    app,client=workspace
    sign_in(app,client)
    secret_id=save(client).json()['id']
    org_id=save(client,'organization',suffix='org').json()['id']
    assert save(client).json()['id']==secret_id
    rows=app.state.store.rows('SELECT * FROM provider_secrets')
    assert len(rows)==2 and all(KEY not in str(r) for r in rows)
    assert app.state.security.decrypt(rows[0]['encrypted'])==KEY
    listing=client.get('/api/credentials').text
    assert KEY not in listing and 'encrypted' not in listing and 'owner_id' not in listing
    assert len(client.get('/api/credentials').json()['secrets'])==2
    sign_in(app,client,'bob','bob@berri.ai')
    assert [s['id'] for s in client.get('/api/credentials').json()['secrets']]==[org_id]
    assert save(client,'organization',suffix='forbidden').status_code==403
    assert client.delete('/api/credentials/secrets/'+secret_id).status_code==404
    assert client.delete('/api/credentials/secrets/'+org_id).status_code==404
    bad=client.post('/api/credentials/secrets',json={'value':KEY,'scope':'broken'})
    assert bad.status_code==422 and KEY not in bad.text
    client.headers['X-CSRF-Token']='invalid'
    assert save(client,suffix='csrf').status_code==403
    client.cookies.clear()
    assert client.get('/api/credentials').status_code==401


def test_pending_request_secure_resolution_is_idempotent_and_wakes(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    request=requested(app,run)
    assert request['status']=='pending' and request['moyai_wait_credential']==request['request_id']
    assert requested(app,run)['request_id']==request['request_id']
    other=app.state.credentials.request(run,CredentialRequest(provider='groq',reason='Another provider',request_key='other'))
    assert other['request_id']==request['request_id']
    pending=client.get('/api/runs/'+run['id']).json()['credential_requests'][0]
    assert pending['can_personal'] and pending['can_organization']
    woken=[]
    monkeypatch.setattr(app.state.manager,'submit',lambda run:woken.append(run['id']))
    path='/api/credentials/requests/'+request['request_id']
    data={'scope':'personal','value':KEY}
    assert client.post(path,json=data).status_code==200
    assert client.post(path,json=data).status_code==200
    assert woken==[run['id'],run['id']]
    assert len(app.state.store.rows('SELECT * FROM provider_secrets'))==1
    assert requested(app,run)['status']=='provided'
    assert KEY not in client.get('/api/runs/'+run['id']).text
    assert KEY not in json.dumps(app.state.store.events(run['id']))
    assert not client.get('/api/runs/'+run['id']).json()['credential_requests']


def test_other_members_cannot_supply_personal_keys_or_reuse_foreign_handles(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    request=requested(app,run)
    sign_in(app,client,'bob','bob@berri.ai')
    save(client,suffix='bob')
    assert client.post('/api/credentials/requests/'+request['request_id'],json={'scope':'personal','value':KEY}).status_code==403
    row=client.get('/api/runs/'+run['id']).json()['credential_requests'][0]
    assert not row['can_personal'] and not row['can_organization']
    sign_in(app,client)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert client.post('/api/credentials/requests/'+request['request_id'],json={'scope':'session','value':KEY}).status_code==200
    headers={'Authorization':'Bearer capability'}
    args={'request_id':request['request_id'],'method':'GET','path':'/models'}
    endpoint='/broker/'+run['id']+'/credentials/invoke'
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?',('google:bob',run['id']))
    assert client.post(endpoint,json=args,headers=headers).status_code==403
    bob=active(app,'google:bob')
    assert client.post('/broker/'+bob['id']+'/credentials/invoke',json=args,headers=headers).status_code==403
    # Session-only keys cannot migrate to another session of the same owner.
    alice2=active(app)
    assert requested(app,alice2)['status']=='pending'
    child=active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(run['id'],child['id']))
    assert requested(app,app.state.store.run(child['id']))['status']=='provided'


def test_admin_cannot_attach_their_personal_key_to_someone_elses_request(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    mine=save(client).json()['id']
    bob=active(app,'google:bob')
    request=requested(app,bob)
    endpoint='/api/credentials/requests/'+request['request_id']
    assert client.post(endpoint,json={'scope':'personal','value':KEY}).status_code==403
    assert client.post(endpoint,json={'secret_id':mine}).status_code==403
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert client.post(endpoint,json={'scope':'organization','value':KEY}).status_code==200


def test_fixed_provider_proxy_auth_redaction_routes_no_redirects_and_revocation(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    secret=save(client).json()['id']
    run=active(app)
    request=requested(app,run)
    calls=[]
    response_status=200
    def upstream(req):
        calls.append(req)
        assert str(req.url)=='https://api.fireworks.ai/inference/v1/chat/completions'
        assert req.headers['Authorization']=='Bearer '+KEY
        escaped_key = ''.join('\\u%04x' % ord(c) for c in KEY)
        raw = json.dumps({'choices':[{'message':{'content':KEY}}],KEY:KEY}).replace(KEY,escaped_key)
        return httpx.Response(response_status,headers={'location':'https://untrusted.example/key'},content=raw)
    actual=httpx.AsyncClient
    def client_factory(**kw):
        assert kw['follow_redirects'] is False
        return actual(transport=httpx.MockTransport(upstream),**kw)
    monkeypatch.setattr('app.credentials.httpx.AsyncClient',client_factory)
    args={'request_id':request['request_id'],'path':'/chat/completions','body':{'model':'test-model','messages':[]}}
    endpoint='/broker/'+run['id']+'/credentials/invoke'
    headers={'Authorization':'Bearer capability'}
    result=client.post(endpoint,json=args,headers=headers)
    assert result.status_code==200 and KEY not in result.text and '[credential redacted]' in result.text
    for path in ['https://evil.example','//evil.example','/../keys','/models?api_key=x','/keys','/chat/completions/../keys']:
        assert client.post(endpoint,json={**args,'path':path},headers=headers).status_code==422
    assert client.post(endpoint,json={**args,'body':{'stream':True}},headers=headers).status_code==422
    assert len(calls)==1
    response_status=302
    result=client.post(endpoint,json=args,headers=headers)
    assert result.status_code==502 and KEY not in result.text and len(calls)==2
    response_status=401
    result=client.post(endpoint,json=args,headers=headers)
    assert result.status_code==401 and KEY not in result.text
    assert client.delete('/api/credentials/secrets/'+secret).status_code==200
    assert client.post(endpoint,json=args,headers=headers).status_code==403
    assert len(calls)==3
    reopened=requested(app,run)
    assert reopened['status']=='pending' and reopened['request_id']==request['request_id']
    assert not app.state.store.rows('SELECT * FROM model_requests')  # Not Moyai gateway spend.


def test_accounting_links_cannot_grant_personal_credentials(workspace):
    app,client=workspace
    sign_in(app,client)
    store=app.state.store
    store.execute("INSERT INTO users(id,kind,email,name,linked_user_id,created_at,updated_at) VALUES('slack:member','slack','wrong@berri.ai','Slack user','google:alice',?,?)",(now(),now()))
    vault=app.state.credentials
    assert not vault.same_requester('google:alice','slack:member')
    store.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=? WHERE id='slack:member'",(now(),))
    assert vault.same_requester('google:alice','slack:member')
    store.execute("UPDATE users SET profile_conflict=1 WHERE id='slack:member'")
    assert not vault.same_requester('google:alice','slack:member')
    store.execute("UPDATE users SET profile_conflict=0,profile_checked_at=? WHERE id='slack:member'",((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),))
    assert not vault.same_requester('google:alice','slack:member')


def test_decline_and_stopped_request_do_not_save_secrets(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    request=requested(app,run)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    path='/api/credentials/requests/'+request['request_id']
    app.state.store.update_run(run['id'],status='stopping')
    assert client.post(path,json={'value':KEY}).status_code==409
    app.state.store.update_run(run['id'],status='waiting_credential')
    assert client.post(path,json={'decision':'decline'}).status_code==200
    resolution=app.state.credentials.resolution(run['id'],request['request_id'])
    assert resolution['status']=='declined' and 'credentials_http_request' not in resolution['instructions']
    assert not app.state.store.rows('SELECT * FROM provider_secrets')


async def test_wait_releases_modal_survives_restart_and_resumes_same_message(durable):
    manager,cloud,run_id=durable
    vault=attach(manager)
    manager.store.execute("UPDATE messages SET user_id='google:alice' WHERE run_id=?",(run_id,))
    await drive(manager,run_id,phase='monitor')
    request=vault.request(manager.store.run(run_id),CredentialRequest(provider='fireworks',reason='Benchmark models',request_key='bench'))
    state=manager.state(run_id)
    result={'message':'Key needed','continuation':True,'completed':False,'wait_credential':request['request_id']}
    state.update(phase='save',exit_code=0,result=result)
    manager.store.update_run(run_id,pending_result=json.dumps(result))
    manager.save(run_id,state)
    await drive(manager,run_id,phase='waiting_credential')
    assert not cloud.machines[0].alive and manager.has_capacity()
    assert manager.store.run(run_id)['token_hash']==''
    assert await manager.advance(run_id) is False
    original_message=state['message_id']
    manager=cloud.attach(TemporalRunManager(Store(manager.settings.data_dir),manager.settings))
    vault=attach(manager)
    assert await manager.advance(run_id) is False
    vault.resolve(request['request_id'],Resolve(scope='personal',value=SecretStr(KEY)),'google:alice',False)
    manager.submit(manager.store.run(run_id))
    await drive(manager,run_id)
    assert len(cloud.machines)==2 and cloud.machines[1].spec['continuation']
    assert cloud.machines[1].spec['credential_resolution']['status']=='provided'
    assert KEY not in json.dumps(cloud.machines[1].spec)+json.dumps(manager.state(run_id))
    assert manager.state(run_id)['message_id']==original_message
    assert len([m for m in manager.store.messages(run_id) if m['role']=='assistant'])==1


def test_pause_happens_only_after_complete_credential_tool_round():
    relay=SimpleNamespace(wait_group='',wait_credential='a'*32)
    waiting=AgentWait(relay)
    agent=SimpleNamespace(interrupt=lambda:None)
    waiting.step(agent)
    assert waiting.credential=='a'*32 and waiting.requested
    result={'interrupted':True,'messages':[{'role':'assistant','tool_calls':[{'id':'key-request'}]}]}
    assert not waiting.can_continue(result)
    result['messages'].append({'role':'tool','tool_call_id':'key-request','content':'pending'})
    assert waiting.can_continue(result)


async def test_early_decline_before_checkpoint_is_seen_after_pause(durable):
    manager,cloud,run_id=durable
    vault=attach(manager)
    manager.store.execute("UPDATE messages SET user_id='google:alice' WHERE run_id=?",(run_id,))
    await drive(manager,run_id,phase='monitor')
    request=vault.request(manager.store.run(run_id),CredentialRequest(provider='openai',reason='Run evaluation',request_key='key'))
    vault.resolve(request['request_id'],Resolve(decision='decline'),'google:alice',False)
    state=manager.state(run_id)
    result={'message':'Key requested','continuation':True,'completed':False,'wait_credential':request['request_id']}
    state.update(phase='save',exit_code=0,result=result)
    manager.store.update_run(run_id,pending_result=json.dumps(result))
    manager.save(run_id,state)
    await drive(manager,run_id)
    assert cloud.machines[1].spec['credential_resolution']['status']=='declined'
    assert not manager.store.rows('SELECT * FROM provider_secrets')


def test_credential_tools_require_durable_identity_and_do_not_return_values(workspace):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    headers={'Authorization':'Bearer capability'}
    endpoint='/broker/'+run['id']
    body={'name':'credentials_request','arguments':{'provider':'fireworks','reason':'Run benchmark','request_key':'key'}}
    assert client.post(endpoint+'/tools/call',json=body,headers=headers).status_code==403
    app.state.settings.temporal_enabled=True
    assert 'credentials_request' in [t['name'] for t in client.get(endpoint+'/tools',headers=headers).json()]
    result=client.post(endpoint+'/tools/call',json=body,headers=headers)
    assert result.status_code==200 and result.json()['status']=='pending'
    bad=client.post(endpoint+'/tools/call',json={**body,'arguments':{**body['arguments'],'value':KEY}},headers=headers)
    assert 'Invalid' in bad.text and KEY not in bad.text
    app.state.store.update_run(run['id'],token_hash='')
    assert client.post(endpoint+'/tools/call',json=body,headers=headers).status_code==401
