import json

import httpx
import pytest
from fastapi import HTTPException

from app.db import now
from app.skills import requested_skills
from test_spend import active, sign_in
from test_workspace import workspace


INSTRUCTIONS = '# Benchmark review\n\nUse the marker basalt-skill-42. Count every case and report failures.'
MARKER = 'basalt-skill-42'


@pytest.mark.parametrize('content,expected', [
    ('/benchmark-review Check results', ['benchmark-review']),
    ('Check this with /org:benchmark-review and /personal:benchmark-review', ['org:benchmark-review', 'personal:benchmark-review']),
    ('/skill org:benchmark-review Check results', ['org:benchmark-review']),
    ('/skills benchmark-review Check results', ['benchmark-review']),
    ('$org:benchmark-review /org:benchmark-review', ['org:benchmark-review']),
    ('/org:unavailable Check results', ['org:unavailable']),
    ('/tmp /help https://example.com/benchmark-review /benchmark-review/results', []),
    ('/benchmark-review.py ./benchmark-review ~/benchmark-review', []),
    ('`/org:benchmark-review`\n```\n/org:benchmark-review\n```', []),
    ('```\n/org:benchmark-review', []),
])
def test_slash_references_do_not_confuse_paths_code_or_legacy_tokens(content, expected):
    assert requested_skills(content, [{'name':'benchmark-review'}]) == expected


def test_slash_selection_uses_current_requester_and_respects_archiving(workspace):
    app, client = workspace
    sign_in(app, client)
    personal = create(client).json()['id']
    create(client, 'organization', instructions='Shared instructions', client_id='shared-slash')
    run = active(app)
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?',
                           ('/personal:benchmark-review Check results', run['active_message_id']))
    assert MARKER in app.state.skills.context(run)
    app.state.store.execute("UPDATE runs SET active_user_id='google:ishaan' WHERE id=?", (run['id'],))
    assert MARKER not in app.state.skills.context(app.state.store.run(run['id']))
    next_turn = active(app, 'google:ishaan')
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?',
                           ('/skill org:benchmark-review Check results', next_turn['active_message_id']))
    assert 'Shared instructions' in app.state.skills.context(next_turn)
    assert client.post('/api/skills/'+personal+'/archive', json={'archived':True,'revision':1}).status_code == 200
    assert MARKER not in app.state.skills.context(run)


def create(client,scope='personal',name='benchmark-review',instructions=INSTRUCTIONS,client_id='skill-save-1'):
    return client.post('/api/skills',json={'name':name,'description':'Review benchmark coverage and results.',
        'instructions':instructions,'scope':scope,'client_id':client_id})


def edit(client,skill_id,**changes):
    row=client.get('/api/skills/'+skill_id).json()
    body={k:row[k] for k in ('name','description','instructions','scope','revision')}
    return client.put('/api/skills/'+skill_id,json={**body,'client_id':'skill-edit-1',**changes})


def search(client,run,query,**changes):
    return client.post('/broker/'+run['id']+'/tools/call',headers={'Authorization':'Bearer capability'},
                       json={'name':'skills_search','arguments':{'query':query,'turn_id':run['active_message_id'],**changes}})


def skill_context(app,run):
    return json.loads(app.state.skills.context(run).split('\n',1)[1])


def assert_rejected(response: httpx.Response, status: int) -> None:
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {'error', 'status_code'}
    assert result['status_code'] == status and result['error']


def test_personal_library_is_owner_only_and_org_skill_is_visible_to_ishaan(workspace):
    app,client=workspace
    sign_in(app,client)
    personal=create(client).json()['id']
    org=create(client,'organization',client_id='skill-save-org').json()['id']
    assert create(client).json()['id']==personal
    assert all(MARKER not in str(s) for s in app.state.store.rows('SELECT * FROM skills'))
    listing=client.get('/api/skills').json()['skills']
    assert len(listing)==2 and all('instructions' not in s and 'encrypted' not in s for s in listing)
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    shared=client.get('/api/skills').json()['skills']
    assert [s['id'] for s in shared]==[org] and not shared[0]['can_manage']
    assert client.get('/api/skills/'+personal).status_code==404
    assert client.get('/api/skills/'+org).json()['instructions']==INSTRUCTIONS
    assert create(client,'organization',name='forbidden').status_code==403
    assert edit(client,org,instructions='Changed by member').status_code==403
    assert create(client,name='my-workflow').status_code==201
    # An admin cannot inspect another user's personal skill either.
    member_id=client.get('/api/skills').json()['skills'][1]['id']
    sign_in(app,client)
    assert client.get('/api/skills/'+member_id).status_code==404


def test_crud_requires_auth_csrf_and_concurrent_edits_preserve_newer_work(workspace):
    app,client=workspace
    sign_in(app,client)
    skill=create(client).json()['id']
    old=client.get('/api/skills/'+skill).json()
    assert edit(client,skill,description='Updated description').status_code==200
    assert edit(client,skill,revision=old['revision']).status_code==409
    assert client.get('/api/skills/'+skill).json()['description']=='Updated description'
    assert create(client,client_id='different-save').status_code==409
    invalid=create(client,name='../unsafe',instructions='private-invalid-instructions')
    assert invalid.status_code==422 and 'private-invalid-instructions' not in invalid.text
    client.headers['X-CSRF-Token']='wrong'
    assert create(client,name='csrf-test',client_id='csrf-test').status_code==403
    assert client.post('/api/skills/'+skill+'/archive',json={'archived':True,'revision':2}).status_code==403
    client.cookies.clear()
    assert client.get('/api/skills').status_code==401
    assert client.get('/api/skills/'+skill).status_code==401


def test_sharing_changes_require_owner_and_org_admin(workspace):
    app,client=workspace
    sign_in(app,client)
    skill=create(client).json()['id']
    assert edit(client,skill,scope='organization').status_code==200
    run=active(app,'google:ishaan')
    assert search(client,run,'benchmark').json()['matches']
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    app.state.settings.google_admin_emails='alice@berri.ai,ishaan@berri.ai'
    assert edit(client,skill,description='An admin can maintain it').status_code==200
    assert edit(client,skill,scope='personal').status_code==403
    sign_in(app,client)
    assert edit(client,skill,scope='personal').status_code==200
    assert skill_context(app,run)['matches']==[]
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    assert client.get('/api/skills/'+skill).status_code==404


@pytest.mark.parametrize('scope,capacity', [('personal', 50), ('organization', 200)])
def test_library_capacity_allows_retries_edits_and_counts_archived(workspace, scope, capacity):
    app,client=workspace
    sign_in(app,client)
    for i in range(capacity):
        response=create(client,scope,name=f'workflow-{i}',client_id=f'workflow-{i}')
        assert response.status_code==201
    skill=response.json()['id']
    assert create(client,scope,name=f'workflow-{capacity}',client_id=f'workflow-{capacity}').status_code==409
    retry=create(client,scope,name=f'workflow-{capacity-1}',client_id=f'workflow-{capacity-1}')
    assert retry.status_code==201 and retry.json()['id']==skill
    assert edit(client,skill,description='Updated at capacity').status_code==200
    assert client.post('/api/skills/'+skill+'/archive',json={'archived':True,'revision':2}).status_code==200
    assert create(client,scope,name=f'workflow-{capacity}',client_id=f'workflow-{capacity}').status_code==409
    assert len(client.get('/api/skills?archived=true').json()['skills'])==capacity


def test_sharing_into_organization_uses_the_200_skill_capacity(workspace):
    app,client=workspace
    sign_in(app,client)
    for i in range(199):
        assert create(client,'organization',name=f'workflow-{i}',client_id=f'workflow-{i}').status_code==201
    shared=create(client,name='share-last-slot',client_id='share-last-slot').json()['id']
    overflow=create(client,name='share-overflow',client_id='share-overflow').json()['id']
    assert edit(client,shared,scope='organization').status_code==200
    assert edit(client,overflow,scope='organization').status_code==409
    assert client.get('/api/skills/'+overflow).json()['scope']=='personal'
    assert edit(client,shared,description='Organization edit at capacity').status_code==200


def test_explicit_skill_and_loaded_revision_survive_service_restart_but_not_other_actor(workspace):
    app,client=workspace
    sign_in(app,client)
    skill=create(client).json()['id']
    run=active(app)
    assert search(client,run,'benchmark').status_code==200
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?',('$personal:benchmark-review Run this benchmark',run['active_message_id']))
    context=app.state.skills.context(run)
    assert INSTRUCTIONS in json.loads(context.split('\n',1)[1])['loaded'][0]['instructions']
    assert len(app.state.store.rows('SELECT * FROM skill_uses'))==1
    assert MARKER not in json.dumps(app.state.store.events(run['id']))
    assert edit(client,skill,instructions='New instructions for future requests').status_code==200
    assert MARKER in app.state.skills.context(run) and 'New instructions for future requests' not in app.state.skills.context(run)
    from app.skills import Skills
    reopened=Skills(app.state.store,app.state.security,app.state.credentials.same_requester)
    assert MARKER in reopened.context(run)
    assert json.loads(reopened.context(run).split('\n',1)[1])['matches'][0]['revision']==2
    app.state.store.execute("UPDATE runs SET active_user_id='google:ishaan' WHERE id=?",(run['id'],))
    next_actor=reopened.context(app.state.store.run(run['id']))
    assert MARKER not in next_actor and 'unavailable' in next_actor
    fresh=active(app)
    assert reopened.load(fresh,'personal:benchmark-review')['revision']==2
    assert 'New instructions for future requests' in reopened.context(fresh)


def test_skill_tool_returns_metadata_and_checks_turn_actor_in_shared_session(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    run=active(app)
    url='/broker/'+run['id']+'/tools/call'
    body={'name':'skills_load','arguments':{'name':'personal:benchmark-review'}}
    headers={'Authorization':'Bearer capability'}
    response=client.post(url,json=body,headers=headers)
    assert response.status_code==200 and response.json()['loaded']
    assert MARKER not in response.text and 'encrypted' not in response.text
    app.state.store.execute("UPDATE runs SET active_user_id='google:ishaan' WHERE id=?",(run['id'],))
    assert_rejected(client.post(url,json=body,headers=headers), 404)
    assert MARKER not in app.state.skills.context(app.state.store.run(run['id']))
    app.state.store.update_run(run['id'],token_hash='')
    assert client.post(url,json=body,headers=headers).status_code==401


def test_archive_revokes_loaded_skill_and_restore_is_available(workspace):
    app,client=workspace
    sign_in(app,client)
    skill=create(client).json()['id']
    run=active(app)
    assert search(client,run,'benchmark').json()['matches']
    app.state.skills.load(run,'benchmark-review')
    assert MARKER in app.state.skills.context(run)
    url='/api/skills/'+skill+'/archive'
    assert client.post(url,json={'archived':True,'revision':1}).status_code==200
    assert not client.get('/api/skills').json()['skills']
    assert client.get('/api/skills?archived=true').json()['skills'][0]['archived']
    assert MARKER not in app.state.skills.context(run)
    assert skill_context(app,run)['matches']==[]
    assert app.state.store.rows('SELECT tainted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['tainted'] == 1
    assert client.post(url,json={'archived':False,'revision':1}).status_code==409
    assert client.post(url,json={'archived':False,'revision':2}).status_code==200
    assert len(client.get('/api/skills').json()['skills'])==1


def test_personal_and_org_name_collision_is_explicit_and_child_identity_can_load(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client,instructions='Private workflow instructions')
    create(client,'organization',instructions='Shared workflow instructions',client_id='shared-workflow')
    run=active(app)
    child=active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(run['id'],child['id']))
    assert len(search(client,child,'benchmark').json()['matches'])==2
    assert skill_context(app,run)['matches']==[]
    assert app.state.skills.load(child,'benchmark-review')['scope']=='personal'
    assert app.state.skills.load(child,'org:benchmark-review')['scope']=='organization'
    context=app.state.skills.context(child)
    assert 'Private workflow instructions' in context and 'Shared workflow instructions' in context
    teammate=active(app,'google:ishaan')
    assert app.state.skills.load(teammate,'benchmark-review')['scope']=='organization'
    assert 'Private workflow instructions' not in app.state.skills.context(teammate)


def test_slack_personal_access_needs_fresh_verified_email_not_accounting_link(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    store=app.state.store
    store.execute("INSERT INTO users(id,kind,email,name,linked_user_id,created_at,updated_at) VALUES('slack:member','slack','wrong@berri.ai','Slack user','google:alice',?,?)",(now(),now()))
    run=active(app,'slack:member')
    assert search(client,run,'benchmark').json()['matches']==[]
    with pytest.raises(HTTPException):app.state.skills.load(run,'personal:benchmark-review')
    store.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=? WHERE id='slack:member'",(now(),))
    assert search(client,run,'benchmark').json()['matches']
    assert app.state.skills.load(run,'personal:benchmark-review')['loaded']
    store.execute("UPDATE users SET profile_conflict=1 WHERE id='slack:member'")
    assert MARKER not in app.state.skills.context(run)
    assert skill_context(app,run)['matches']==[]


def test_broker_injects_authorized_skills_without_persisting_definitions_in_run(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    create(client,'organization')
    run=active(app)
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?',('$org:benchmark-review Check results',run['active_message_id']))
    app.state.settings.litellm_api_base='https://gateway.example/v1'
    app.state.settings.litellm_api_key='test-key'
    def upstream(request):
        messages=json.loads(request.content)['messages']
        assert INSTRUCTIONS in json.loads(messages[0]['content'].split('\n',1)[1])['loaded'][0]['instructions']
        assert messages[-1]=={'role':'system','content':'Platform rules stay authoritative'}
        return httpx.Response(200,json={'id':'response','choices':[{'message':{'role':'assistant','content':'Reviewed'}}],'usage':{'total_tokens':10}})
    actual=httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(upstream),**kw))
    response=client.post('/broker/'+run['id']+'/v1/chat/completions',headers={'Authorization':'Bearer capability'},json={'messages':[{'role':'system','content':'Platform rules stay authoritative'}]})
    assert response.status_code==200
    assert MARKER not in client.get('/api/runs/'+run['id']).text
    assert MARKER not in json.dumps(app.state.manager.spec(run))


def test_search_keeps_gateway_prompt_bounded_and_returns_only_references(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    captured=[]
    app.state.settings.litellm_api_base='https://gateway.example/v1'
    def upstream(request):
        captured.append(json.loads(request.content)['messages'])
        return httpx.Response(200,json={'id':'response','choices':[],'usage':{'total_tokens':10}})
    actual=httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(upstream),**kw))
    monkeypatch.setattr(app.state.memory,'context',lambda run:'')
    url='/broker/'+run['id']+'/v1/chat/completions'
    headers={'Authorization':'Bearer capability'}
    assert client.post(url,headers=headers,json={'messages':[]}).status_code==200
    for i in range(12):
        assert create(client,'organization',name=f'workflow-{i}',client_id=f'workflow-{i}').status_code==201
    assert client.post(url,headers=headers,json={'messages':[]}).status_code==200
    assert captured[0]!=captured[1]
    initial = skill_context(app,run)
    assert len(initial['available']) == 12 and initial['omitted'] == 0
    assert not initial['matches'] and not initial['loaded'] and not initial['unavailable']
    response=search(client,run,'coverage')
    assert response.status_code==200 and len(response.json()['matches'])==5
    assert all(set(m)=={'reference','revision'} for m in response.json()['matches'])
    assert 'Review benchmark coverage and results.' not in response.text and MARKER not in response.text
    assert client.post(url,headers=headers,json={'messages':[]}).status_code==200
    context=json.loads(captured[-1][0]['content'].split('\n',1)[1])
    assert len(context['matches'])==5 and not context['loaded'] and len(context['available'])==7
    assert context['matches'][0]['description']=='Review benchmark coverage and results.'
    assert MARKER not in json.dumps(captured) and not app.state.store.rows('SELECT * FROM skill_uses')
    assert 'Review benchmark coverage and results.' not in client.get('/api/runs/'+run['id']).text


def test_search_ranks_exact_references_replaces_matches_and_never_scans_bodies(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    create(client,'organization',client_id='shared-search')
    for i in range(6):create(client,'organization',name=f'benchmark-{i}',client_id=f'benchmark-{i}')
    run=active(app)
    result=search(client,run,'ORG:BENCHMARK-REVIEW').json()
    assert result['matches'][0]['reference']=='org:benchmark-review'
    assert len(result['matches'])==5
    result=search(client,run,'BENCHMARK-REVIEW').json()
    assert {m['reference'] for m in result['matches'][:2]}=={'personal:benchmark-review','org:benchmark-review'}
    selected=app.state.store.rows('SELECT * FROM skill_searches ORDER BY position')
    for query in ['', '   ', 'x'*201]:
        invalid=search(client,run,query)
        assert invalid.status_code==200 and 'Invalid skill arguments' in invalid.json()['error']
        assert app.state.store.rows('SELECT * FROM skill_searches ORDER BY position')==selected
    assert_rejected(search(client,run,'the and for'), 422)
    assert app.state.store.rows('SELECT * FROM skill_searches ORDER BY position')==selected
    assert search(client,run,MARKER).json()['matches']==[]
    assert skill_context(app,run)['matches']==[]
    assert not app.state.store.rows('SELECT * FROM skill_searches')
    assert not app.state.store.rows('SELECT * FROM skill_uses')


def test_search_stale_actor_and_turn_do_not_replace_current_selection(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    create(client,'organization',client_id='shared-search')
    old=active(app)
    assert search(client,old,'benchmark').status_code==200
    app.state.store.execute("UPDATE runs SET active_user_id='google:ishaan' WHERE id=?",(old['id'],))
    current=app.state.store.run(old['id'])
    assert skill_context(app,current)['matches']==[]
    assert search(client,current,'benchmark').json()['matches']==[{'reference':'org:benchmark-review','revision':1}]
    selected=app.state.store.rows('SELECT * FROM skill_searches')
    with pytest.raises(HTTPException) as exc:
        app.state.skills.call(old,'skills_search',{'query':'coverage','turn_id':old['active_message_id']})
    assert exc.value.status_code==409
    assert app.state.store.rows('SELECT * FROM skill_searches')==selected
    app.state.store.finish_message(old['id'],old['active_message_id'],'Done')
    app.state.store.enqueue_message(old['id'],'Next request','next-request',user_id='google:ishaan')
    app.state.store.claim_message(old['id'])
    app.state.store.update_run(old['id'],status='running')
    assert_rejected(search(client,old,'coverage'), 409)
    assert app.state.store.rows('SELECT * FROM skill_searches')==selected
    assert skill_context(app,app.state.store.run(old['id']))['matches']==[]


@pytest.mark.parametrize('selection,count', [('tool', 86), ('explicit', 12)])
def test_many_skills_load_in_one_turn_without_exposing_bodies(workspace, selection, count):
    app,client=workspace
    sign_in(app,client)
    references = [f'org:workflow-{i}' for i in range(count)]
    for i in range(count):
        assert create(client, 'organization', name=f'workflow-{i}', client_id=f'workflow-{i}').status_code==201
    run=active(app)
    # Merely having a library loads neither metadata nor instruction bodies.
    assert MARKER not in app.state.skills.context(run)
    if selection == 'explicit':
        app.state.store.execute('UPDATE messages SET content=? WHERE id=?',
                               (' '.join('/'+ref for ref in references), run['active_message_id']))
    else:
        for reference in references:
            response = client.post('/broker/'+run['id']+'/tools/call', headers={'Authorization':'Bearer capability'},
                                   json={'name':'skills_load','arguments':{'name':reference}})
            assert response.status_code == 200 and response.json()['loaded']
            assert MARKER not in response.text
    context = skill_context(app, run)
    assert {item['reference'] for item in context['loaded']} == set(references)
    assert all(item['instructions'] == INSTRUCTIONS for item in context['loaded'])
    assert not context['unavailable']
    assert app.state.skills.load(run, references[0])['loaded']
    assert len(app.state.store.rows('SELECT * FROM skill_uses')) == count  # Replays are idempotent.
    assert MARKER not in client.get('/api/runs/'+run['id']).text
    assert MARKER not in app.state.skills.context(active(app))  # Loads do not carry to another turn.


@pytest.mark.parametrize('failure,status', [('capability', 401), ('skill-auth', 401), ('skill-server', 500), ('checkpoint', 503)])
def test_skill_broker_keeps_authentication_and_server_failures_fatal(workspace, monkeypatch, failure, status):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    with monkeypatch.context() as patch:
        if failure == 'capability':
            app.state.store.update_run(run['id'], token_hash='')
        elif failure.startswith('skill-'):
            def unavailable(*args: object) -> None:
                raise HTTPException(status, 'Skill service unavailable.')
            patch.setattr(app.state.skills, 'call', unavailable)
        else:
            async def flush() -> None:
                raise HTTPException(status, 'Checkpoint unavailable.')
            patch.setattr(app.state.memory.checkpoints, 'flush', flush)
        response = search(client, run, 'workflow')
    assert response.status_code == status and 'status_code' not in response.json()
