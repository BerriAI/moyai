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


def test_labeled_token_request_round_trip(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    fields = [{'name': 'SERVICE_TOKEN', 'label': 'Access token', 'secret': True, 'required': True}]
    args = CredentialRequest(provider='generic', name='service', reason='Read service logs', request_key='token', input_fields=fields)
    vault = app.state.credentials
    request = vault.request(run, args)
    assert vault.request(run, args)['request_id'] == request['request_id']
    assert client.get('/api/runs/'+run['id']).json()['credential_requests'][0]['input_fields'] == fields
    changed = args.model_copy(update={'input_fields': []})
    with pytest.raises(ValueError, match='original credential inputs'):
        vault.request(run, changed)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/credentials/requests/'+request['request_id'], json={
        'decision': 'provide', 'generation': 0, 'scope': 'personal', 'lifetime': 'session',
        'value': json.dumps({'SERVICE_TOKEN': KEY})})
    assert response.status_code == 200
    secret = app.state.store.rows('SELECT * FROM provider_secrets')[0]
    assert json.loads(app.state.security.decrypt(secret['encrypted'])) == {'SERVICE_TOKEN': KEY}
    assert KEY not in client.get('/api/runs/'+run['id']).text


@pytest.mark.parametrize('fields', [
    [{'name': 'PATH', 'label': 'Token'}],
    [{'name': 'SERVICE_TOKEN', 'label': 'Token', 'value': 'must-not-accept-values'}],
    [{'name': 'SERVICE_TOKEN', 'label': 'Token'}]*2,
])
def test_credential_input_metadata_is_validated(fields):
    with pytest.raises(ValueError):
        CredentialRequest(provider='generic', name='service', reason='Read logs', request_key='token', input_fields=fields)


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
    assert client.post(path,json={'scope':'personal','value':KEY}).status_code==409
    app.state.store.update_run(run['id'],status='waiting_credential')
    assert client.post(path,json={'decision':'decline'}).status_code==200
    resolution=app.state.credentials.resolution(run['id'],request['request_id'])
    assert resolution['status']=='declined' and 'credentials_http_request' not in resolution['instructions']
    assert not app.state.store.rows('SELECT * FROM provider_secrets')


@pytest.mark.parametrize('scope',[None,''])
def test_new_keys_require_explicit_scope_in_library_and_request_form(workspace,scope):
    app,client=workspace
    sign_in(app,client)
    body={'provider':'fireworks','label':'New key','value':KEY,'client_id':'scope-choice-test'}
    if scope is not None:
        body['scope']=scope
    response=client.post('/api/credentials/secrets',json=body)
    assert response.status_code==422 and KEY not in response.text
    request=requested(app,active(app))
    resolution={'value':KEY}
    if scope is not None:
        resolution['scope']=scope
    response=client.post('/api/credentials/requests/'+request['request_id'],json=resolution)
    assert response.status_code==422 and KEY not in response.text
    assert not app.state.store.rows('SELECT * FROM provider_secrets')
    assert app.state.credentials.row(request['request_id'])['status']=='pending'


def test_saved_key_selection_keeps_scope_without_new_scope_choice(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    request=requested(app,active(app))
    saved=save(client,'organization').json()['id']
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    response=client.post('/api/credentials/requests/'+request['request_id'],json={'secret_id':saved})
    assert response.status_code==200
    assert len(app.state.store.rows('SELECT * FROM provider_secrets'))==1
    assert app.state.store.rows('SELECT scope FROM provider_secrets')[0]['scope']=='organization'


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


def access_request(vault,run,*,format='env',key='cluster'):
    return vault.request(run,CredentialRequest(provider='generic',name='production cluster',format=format,
        env_var='KUBECONFIG' if format=='file' else '',reason='Investigate the outage',request_key=key))


def generic_value(format):
    return 'apiVersion: v1\nusers:\n- token: private-test-token\n' if format=='file' else json.dumps({'AWS_ACCESS_KEY_ID':'synthetic-access','AWS_SECRET_ACCESS_KEY':'synthetic-secret'})


async def test_generic_setup_guidance_migrates_enriches_and_survives_renewal(workspace,monkeypatch):
    from app.credentials import ReportFailure
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    vault=app.state.credentials
    original=access_request(vault,run)
    with app.state.store.connect() as conn:
        for field in ('setup_url','setup_instructions'):
            conn.execute('ALTER TABLE credential_requests DROP COLUMN '+field)
    for _ in range(2):
        vault=Credentials(app.state.store,vault.security,vault.settings,vault.manager,vault.checkpoints)
    assert vault.resolution(run['id'],original['request_id'])['setup_url']==''
    body={'provider':'generic','name':'production cluster','reason':'Investigate the outage','request_key':'cluster',
          'setup_url':'https://docs.aws.amazon.com/singlesignon/latest/userguide/howtogetcredentials.html',
          'setup_instructions':'Open the AWS access portal and select the permitted account and role.'}
    result=await vault.call(run,'credentials_request',body)
    assert result['request_id']==original['request_id']
    assert result['setup_url']==body['setup_url'] and result['setup_instructions']==body['setup_instructions']
    pending=client.get('/api/runs/'+run['id']).json()['credential_requests'][0]
    assert pending['setup_url']==result['setup_url'] and pending['setup_instructions']==result['setup_instructions']
    assert access_request(vault,run)['setup_url']==body['setup_url'], 'An older retry must retain guidance'
    with pytest.raises(ValueError,match='original setup guidance'):
        await vault.call(run,'credentials_request',{**body,'setup_url':'https://example.com/different'})
    other=await vault.call(run,'credentials_request',{**body,'name':'different service','request_key':'other',
                                               'setup_url':'https://example.com/another'})
    assert other['request_id']==result['request_id'] and other['setup_url']==body['setup_url']
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert client.post('/api/credentials/requests/'+result['request_id'],json={
        'scope':'personal','lifetime':'persistent','value':generic_value('env')}).status_code==200
    assert vault.resolution(run['id'],result['request_id'])['setup_url']==body['setup_url']
    renewed=vault.report_failure(run,ReportFailure(request_id=result['request_id'],revision=1,failure='invalid'))
    assert renewed['status']=='pending' and renewed['setup_url']==body['setup_url']
    assert renewed['setup_instructions']==body['setup_instructions']


async def test_setup_urls_are_https_without_login_details_and_provider_destinations_stay_fixed(workspace):
    body={'provider':'generic','name':'aws','reason':'Investigate the outage','request_key':'setup'}
    for url in ['/', '#', '/#run=one', 'javascript:alert(1)', 'http://example.com',
                'https://user:password@example.com', 'https://example.com/white space', 'https://example.com\\path']:
        with pytest.raises(ValueError):
            CredentialRequest(**body,setup_url=url)
    assert CredentialRequest(**body,setup_instructions='Ask the service administrator.').setup_url==''
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    result=await app.state.credentials.call(run,'credentials_request',{'provider':'fireworks','reason':'Run benchmark',
        'request_key':'inference','setup_url':'https://example.com/unrelated'})
    assert result['setup_url']=='https://app.fireworks.ai/settings/users/api-keys'


@pytest.mark.parametrize('scope',['personal','organization'])
@pytest.mark.parametrize('lifetime',['session','persistent'])
@pytest.mark.parametrize('format',['env','file'])
def test_generic_sharing_and_lifetime_apply_to_inventory_reuse_and_materialization(workspace,monkeypatch,scope,lifetime,format):
    from app.credentials import ListCredentials, Materialize
    app,client=workspace
    sign_in(app,client)
    vault=app.state.credentials
    run=active(app)
    request=access_request(vault,run,format=format)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    value=generic_value(format)
    response=client.post('/api/credentials/requests/'+request['request_id'],json={
        'scope':scope,'lifetime':lifetime,'value':value,'generation':0})
    assert response.status_code==200
    row=vault.row(request['request_id'])
    secret=app.state.store.rows('SELECT * FROM provider_secrets')[0]
    assert secret['scope']==scope and secret['lifetime']==lifetime
    assert secret['root_id']==(run['id'] if lifetime=='session' else '')
    assert value not in str(secret)
    result=vault.materialize(run,Materialize(request_ids=[row['id']]))
    assert result['bindings'][0]['value']==value and result['bindings'][0]['revision']==1
    assert value not in json.dumps(vault.inventory(run,ListCredentials()))
    for actor,same_root,allowed in [('google:alice',False,lifetime=='persistent'),
                                   ('google:bob',True,scope=='organization'),
                                   ('google:bob',False,scope=='organization' and lifetime=='persistent')]:
        other=active(app,actor)
        if same_root:
            app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(run['id'],other['id']))
            other=app.state.store.run(other['id'])
        listing=vault.inventory(other,ListCredentials())['credentials']
        assert bool(listing)==allowed
        assert (access_request(vault,other,format=format)['status']=='provided')==allowed
        # A credential request is run-bound even when its secret is shared.
        with pytest.raises(HTTPException):
            vault.materialize(other,Materialize(request_ids=[row['id']]))


def test_generic_input_is_explicit_and_runtime_names_and_secret_errors_are_safe(workspace):
    app,client=workspace
    sign_in(app,client)
    body={'provider':'generic','name':'cloud','label':'Cloud','scope':'personal','lifetime':'persistent',
          'format':'env','value':generic_value('env'),'client_id':'generic-input'}
    invalid=[{key:value for key,value in body.items() if key!='lifetime'},
             {**body,'value':json.dumps({'WORKSPACE_RUN_TOKEN':'private-secret'})},
             {**body,'value':json.dumps({'LD_PRELOAD':'private-secret'})},
             {**body,'value':json.dumps({'AWS_SECRET_ACCESS_KEY':{'nested':'private-secret'}})},
             {**body,'value':'private-secret'},
             {**body,'format':'file','env_var':'HOME','value':'private-secret'},
             {**body,'format':'file','env_var':'KUBECONFIG','value':'private-secret\0'}]
    for data in invalid:
        response=client.post('/api/credentials/secrets',json=data)
        assert response.status_code==422 and 'private-secret' not in response.text
    request=access_request(app.state.credentials,active(app))
    for choices in [{'scope':'personal'}, {'scope':'session','lifetime':'session'},
                    {'scope':'session','lifetime':'persistent'},
                    {'scope':'personal','lifetime':'persistent','expires_at':'not-a-date'}]:
        response=client.post('/api/credentials/requests/'+request['request_id'],json={**choices,'value':generic_value('env')})
        assert response.status_code==422 and 'synthetic-secret' not in response.text
        assert app.state.credentials.row(request['request_id'])['status']=='pending'
    assert not app.state.store.rows('SELECT * FROM provider_secrets')


def test_rotation_and_renewal_reject_stale_failure_and_stale_forms(workspace,monkeypatch):
    from app.credentials import Materialize, ReportFailure
    app,client=workspace
    sign_in(app,client)
    vault=app.state.credentials
    run=active(app)
    request=access_request(vault,run)
    endpoint='/api/credentials/requests/'+request['request_id']
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    body={'scope':'personal','lifetime':'persistent','value':generic_value('env'),'generation':0}
    assert client.post(endpoint,json=body).status_code==200
    args=Materialize(request_ids=[request['request_id']])
    first=vault.materialize(run,args)['bindings'][0]
    secret_id=vault.row(request['request_id'])['secret_id']
    edit='/api/credentials/secrets/'+secret_id
    assert client.patch(edit,json={'revision':1,'value':json.dumps({'AWS_SECRET_ACCESS_KEY':'replacement-one'})}).status_code==200
    report=lambda revision,failure:vault.report_failure(run,ReportFailure(request_id=request['request_id'],revision=revision,failure=failure))
    assert report(first['revision'],'expired')['status']=='stale'
    second=vault.materialize(run,args)['bindings'][0]
    assert second['revision']>first['revision']
    pending=report(second['revision'],'invalid')
    assert pending['moyai_wait_credential']==request['request_id'] and pending['generation']==1
    assert client.post(endpoint,json=body).status_code==409
    replacement={**body,'generation':1,'value':json.dumps({'AWS_SECRET_ACCESS_KEY':'replacement-two'})}
    assert client.post(endpoint,json=replacement).status_code==200
    assert client.post(endpoint,json=replacement).status_code==200
    third=vault.materialize(run,args)['bindings'][0]
    assert third['revision']>second['revision']
    assert report(second['revision'],'invalid')['status']=='stale'
    assert vault.materialize(run,args)['status']=='ready'
    assert len(app.state.store.rows('SELECT * FROM provider_secrets'))==2
    assert client.patch(edit,json={'revision':1,'label':'stale overwrite'}).status_code==409


def test_expiry_of_previous_turn_handle_reopens_current_turn_and_preserves_progress(workspace,monkeypatch):
    from app.credentials import Materialize
    app,client=workspace
    sign_in(app,client)
    vault=app.state.credentials
    run=active(app)
    request=access_request(vault,run,format='file')
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    endpoint='/api/credentials/requests/'+request['request_id']
    assert client.post(endpoint,json={'scope':'organization','lifetime':'persistent','value':generic_value('file')}).status_code==200
    secret=vault.row(request['request_id'])['secret_id']
    assert client.patch('/api/credentials/secrets/'+secret,json={'revision':1,'expires_at':'2000-01-01T00:00:00Z'}).status_code==200
    app.state.store.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at,user_id) VALUES(?,'user','Continue outage investigation','running','next',?,'google:alice')",(run['id'],now()))
    message=app.state.store.rows('SELECT MAX(id) AS id FROM messages WHERE run_id=?',(run['id'],))[0]['id']
    app.state.store.execute('UPDATE runs SET active_message_id=? WHERE id=?',(message,run['id']))
    run=app.state.store.run(run['id'])
    pending=vault.materialize(run,Materialize(request_ids=[request['request_id']]))
    assert pending['status']=='pending' and pending['failure']=='expired'
    assert vault.row(request['request_id'])['message_id']==message
    assert vault.resolution(run['id'],request['request_id'])['status']=='pending'
    assert len(vault.pending(run,'google:alice',True))==1
    assert client.get('/api/credentials').json()['secrets'][0]['status']=='expired'


def test_metadata_edits_keep_values_and_enforce_personal_org_and_session_bounds(workspace,monkeypatch):
    from app.credentials import Materialize
    app,client=workspace
    sign_in(app,client)
    vault=app.state.credentials
    run=active(app)
    request=access_request(vault,run)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    assert client.post('/api/credentials/requests/'+request['request_id'],json={
        'scope':'personal','lifetime':'persistent','value':generic_value('env')}).status_code==200
    identity=vault.row(request['request_id'])['secret_id']
    endpoint='/api/credentials/secrets/'+identity
    assert client.patch(endpoint,json={'revision':1,'lifetime':'session'}).status_code==422
    saved=client.patch(endpoint,json={'revision':1,'scope':'organization','lifetime':'session','root_id':run['id']})
    assert saved.status_code==200 and saved.json()['revision']==2
    assert 'value' not in saved.text and 'encrypted' not in saved.text
    assert vault.materialize(run,Materialize(request_ids=[request['request_id']]))['bindings'][0]['value']==generic_value('env')
    sign_in(app,client,'bob','bob@berri.ai')
    assert client.patch(endpoint,json={'revision':2,'label':'Not authorized'}).status_code==404
    sign_in(app,client)
    assert client.patch(endpoint,json={'revision':2,'lifetime':'persistent'}).json()['root_id']==''
    assert client.patch(endpoint,json={'revision':3,'value':''}).status_code==422
    assert client.patch(endpoint,json={'revision':3,'expires_at':None}).status_code==422
    for invalid in [{'scope':'session','value':generic_value('env')},
                    {'expires_at':'not-a-date','value':generic_value('env')}]:
        rejected=client.patch(endpoint,json={'revision':3,**invalid})
        assert rejected.status_code==422 and 'synthetic-secret' not in rejected.text


def test_legacy_session_scope_migrates_once_and_remains_root_bound(workspace):
    app,client=workspace
    sign_in(app,client)
    identity=save(client).json()['id']
    run=active(app)
    app.state.store.execute("UPDATE provider_secrets SET scope='session',root_id=? WHERE id=?",(run['id'],identity))
    with app.state.store.connect() as conn:
        for field in ('name','format','env_var','lifetime','expires_at','invalid_reason','revision'):
            conn.execute('ALTER TABLE provider_secrets DROP COLUMN '+field)
        for field in ('name','format','env_var','generation','failure','revision','secret_revision'):
            conn.execute('ALTER TABLE credential_requests DROP COLUMN '+field)
    for attempt in range(2):
        vault=Credentials(app.state.store,app.state.security,app.state.settings,app.state.manager,SimpleNamespace(flush=None))
        secret=app.state.store.rows('SELECT * FROM provider_secrets WHERE id=?',(identity,))[0]
        assert secret['scope']=='personal' and secret['lifetime']=='session'
        assert vault.permitted(secret,run,'google:alice')
        assert not vault.permitted(secret,active(app),'google:alice')


def test_permission_recovery_pauses_only_this_request_and_keeps_shared_credentials_usable(workspace,monkeypatch):
    from app.credentials import Materialize, ReportFailure
    app,client=workspace
    sign_in(app,client)
    vault=app.state.credentials
    run=active(app)
    request=access_request(vault,run)
    monkeypatch.setattr(app.state.manager,'submit',lambda run:None)
    endpoint='/api/credentials/requests/'+request['request_id']
    assert client.post(endpoint,json={'scope':'organization','lifetime':'persistent','value':generic_value('env')}).status_code==200
    first=vault.materialize(run,Materialize(request_ids=[request['request_id']]))['bindings'][0]
    saved=vault.row(request['request_id'])['secret_id']
    other=active(app,'google:bob')
    other_request=access_request(vault,other)
    failed=vault.report_failure(run,ReportFailure(request_id=request['request_id'],revision=first['revision'],failure='permission'))
    assert failed['status']=='pending' and failed['failure']=='permission'
    assert app.state.store.rows('SELECT invalid_reason FROM provider_secrets')[0]['invalid_reason']==''
    assert vault.materialize(other,Materialize(request_ids=[other_request['request_id']]))['status']=='ready'
    # The service administrator can grant permissions without replacing its key.
    assert client.post(endpoint,json={'secret_id':saved,'generation':1}).status_code==200
    assert vault.materialize(run,Materialize(request_ids=[request['request_id']]))['status']=='ready'
