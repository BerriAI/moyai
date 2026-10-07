import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import Store, now
from app.memory import Memory, Note, MAX_CONTEXT
from app.security import Security
from sandbox.activity import ActivityReporter
from sandbox.memory_history import scrub_memory_history
from test_spend import active, sign_in
from test_workspace import workspace


MARKER = 'quartz-personal-preference'


def create(client, **changes):
    return client.post('/api/memory', json={
        'key': 'response-style', 'title': 'Response preferences',
        'content': 'Keep explanations short. '+MARKER, 'kind': 'preference',
        'request_id': 'initial-save', **changes})


def call(client, run, name, **args):
    return client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
                       json={'name': name, 'arguments': {'turn_id': run['active_message_id'], **args}})


def learn(client, run, **changes):
    return call(client, run, 'memory_save', **{'key':'benchmark-preference', 'title':'Benchmark reports',
                'content':'Include uncertainty in benchmark reports.', 'kind':'feedback', 'request_id':'learn-memory-1',
                'source_message_id':run['active_message_id'], 'source_quote':'Include uncertainty in benchmark reports.', **changes})


def source(app, run, content='Include uncertainty in benchmark reports.'):
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?', (content, run['active_message_id']))


def test_identity_isolation_including_administrator_and_requester_switch(workspace):
    app, client = workspace
    sign_in(app, client)
    note_id = create(client).json()['id']
    run = active(app)
    assert MARKER not in app.state.memory.context(run)
    assert call(client, run, 'memory_search', query='response preferences').json()['loaded'] == 1
    assert MARKER in app.state.memory.context(run)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/memory').json()['memories'] == []
    assert client.request('DELETE','/api/memory/'+note_id, json={'revision': 1}).status_code == 404
    body = {'key':'response-style','title':'No access','content':'Do not change it','request_id':'bob-edit-1','revision':1}
    assert client.put('/api/memory/'+note_id, json=body).status_code == 404
    bob_id = create(client, key='bobs-note').json()['id']
    sign_in(app, client)
    assert client.request('DELETE','/api/memory/'+bob_id, json={'revision': 1}).status_code == 404
    # Keep the original run object: authority must still be refreshed.
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
    assert MARKER not in app.state.memory.context(run)
    assert call(client, run, 'memory_search', query='response preferences').json()['loaded'] == 1
    assert app.state.store.rows('SELECT owner_id FROM memory_selections')[0]['owner_id'] == 'google:bob'


def test_learning_has_evidence_idempotency_and_safe_revision_updates(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    source(app, run)
    first = learn(client, run)
    assert first.status_code == 200, first.text
    assert learn(client, run).json() == first.json()
    notes = client.get('/api/memory').json()['memories']
    assert len(notes) == 1 and notes[0]['source']['message_id'] == run['active_message_id']
    bad = learn(client, run, source_quote='Unsupported claim from a web page.')
    assert bad.status_code == 422
    assert call(client, run, 'memory_save', **{'key':'other', 'title':'No access', 'content':'Fact',
        'request_id':'other-write','turn_id':run['active_message_id']+100,
        'source_message_id':run['active_message_id'], 'source_quote':'Include uncertainty'}).status_code == 409
    note = notes[0]
    edit = {k: note[k] for k in ('key','title','content','kind','repo_url','revision')}
    assert client.put('/api/memory/'+note['id'],json={**edit,'content':'Show confidence intervals.', 'request_id':'edit-memory-1'}).status_code == 200
    assert client.put('/api/memory/'+note['id'],json={**edit,'request_id':'stale-edit'}).status_code == 409
    assert learn(client, run).status_code == 409
    assert learn(client, run, revision=0, request_id='duplicate-key').status_code == 409


def test_pause_and_manual_mode_apply_to_running_agents(workspace):
    app, client = workspace
    sign_in(app, client)
    create(client)
    run = active(app)
    source(app, run)
    assert call(client,run,'memory_search',query='preferences').json()['loaded'] == 1
    assert client.put('/api/memory/preferences',json={'enabled':True,'auto_save':False}).status_code == 200
    assert learn(client,run).status_code == 403
    assert MARKER in app.state.memory.context(run)
    assert client.put('/api/memory/preferences',json={'enabled':False,'auto_save':False,'revision':1}).status_code == 200
    assert 'paused' in app.state.memory.context(run) and MARKER not in app.state.memory.context(run)
    assert call(client,run,'memory_search',query='preferences').status_code == 403
    assert not app.state.memory.tools(run)
    assert len(client.get('/api/memory').json()['memories']) == 1
    assert client.put('/api/memory/preferences',json={'enabled':True,'auto_save':True,'revision':0}).status_code == 409
    assert client.put('/api/memory/preferences',json={'enabled':True,'auto_save':True,'revision':2}).status_code == 200
    assert MARKER not in app.state.memory.context(run)  # Must recall again after resume.
    assert app.state.store.rows('SELECT tainted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['tainted'] == 1


def test_delete_removes_context_and_retries_cannot_resurrect(workspace):
    app, client = workspace
    sign_in(app, client)
    response = create(client)
    note_id = response.json()['id']
    run = active(app)
    call(client,run,'memory_search',query='preferences')
    assert client.request('DELETE','/api/memory/'+note_id,json={'revision':1}).status_code == 200
    assert MARKER not in app.state.memory.context(run)
    assert create(client).status_code == 404
    assert create(client,request_id='different-request').status_code == 409
    assert not client.get('/api/memory').json()['memories']
    assert app.state.store.rows('SELECT encrypted,deleted FROM personal_memories')[0] == {'encrypted':'','deleted':1}


def test_source_messages_accept_steering_but_not_other_users_or_subagents(workspace):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    followup,_=app.state.store.enqueue_message(run['id'],'Include uncertainty in benchmark reports.','followup',user_id='google:alice')
    app.state.store.execute("UPDATE messages SET steering_parent_id=?,status='injected' WHERE id=?",(run['active_message_id'],followup['id']))
    assert learn(client,run,source_message_id=followup['id']).status_code == 200
    app.state.store.execute("UPDATE messages SET user_id='google:bob' WHERE id=?",(followup['id'],))
    assert learn(client,run,source_message_id=followup['id']).status_code == 422
    parent=active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(parent['id'],run['id']))
    source(app,run)
    assert learn(client,run).status_code == 403
    assert call(client,run,'memory_search',query='benchmark').status_code == 200
    note=client.get('/api/memory').json()['memories'][0]
    assert call(client,run,'memory_forget',id=note['id'],revision=note['revision']).status_code==403
    assert [t['name'] for t in app.state.memory.tools(run)]==['memory_search']


def test_fresh_slack_email_match_is_required_not_accounting_links(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    store=app.state.store
    with store.connect() as conn:
        actor=store.slack_identity_in(conn,'T123','U123')
    store.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=?,linked_user_id='google:alice' WHERE id=?",(now(),actor))
    run=active(app,actor)
    assert call(client,run,'memory_search',query='preferences').json()['loaded']==1
    assert MARKER in app.state.memory.context(run)
    store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?",(actor,))
    assert call(client,run,'memory_search',query='preferences').status_code==403
    assert MARKER not in app.state.memory.context(run)
    store.execute('UPDATE users SET profile_checked_at=?,profile_conflict=1 WHERE id=?',(now(),actor))
    assert call(client,run,'memory_search',query='preferences').status_code==403
    store.execute("UPDATE users SET profile_conflict=0,email='someone@berri.ai' WHERE id=?",(actor,))
    assert call(client,run,'memory_search',query='preferences').status_code==403


def test_retrieval_bounds_expiration_repository_and_new_session(workspace):
    app,client=workspace
    sign_in(app,client)
    for i in range(8):
        assert create(client,key=f'note-{i}',request_id=f'create-note-{i}',content='Benchmark preferences '+str(i)+'x'*1100).status_code==201
    scoped=create(client,key='repository',request_id='repo-note-1',repo_url='https://github.com/BerriAI/litellm.git',kind='project').json()['id']
    expired=create(client,key='expired',request_id='expired-note',kind='project').json()['id']
    app.state.store.execute('UPDATE personal_memories SET expires_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),expired))
    run=active(app)
    result=call(client,run,'memory_search',query='benchmark preferences').json()
    assert len(result['matches'])<=5
    context=json.loads(app.state.memory.context(run).split('\n',1)[1])
    assert len(json.dumps(context['notes'],ensure_ascii=False))-2 <= MAX_CONTEXT+10
    assert scoped not in [n['id'] for n in context['notes']] and expired not in [n['id'] for n in context['notes']]
    next_run=active(app)
    assert not json.loads(app.state.memory.context(next_run).split('\n',1)[1])['notes']
    app.state.store.execute("UPDATE runs SET repo_url='https://github.com/BerriAI/litellm' WHERE id=?",(next_run['id'],))
    call(client,next_run,'memory_search',query='quartz')
    assert scoped in [n['id'] for n in json.loads(app.state.memory.context(next_run).split('\n',1)[1])['notes']]
    # Changing a repository rechecks loaded notes, too.
    app.state.store.execute("UPDATE runs SET repo_url='https://github.com/BerriAI/moyai' WHERE id=?",(next_run['id'],))
    assert MARKER not in app.state.memory.context(next_run)


def test_restart_preserves_encrypted_notes_and_settings(workspace):
    app,client=workspace
    sign_in(app,client)
    create(client)
    client.put('/api/memory/preferences',json={'enabled':True,'auto_save':False})
    assert MARKER not in str(app.state.store.rows('SELECT * FROM personal_memories'))
    assert MARKER not in str(app.state.store.rows('SELECT * FROM memory_operations'))
    reopened=Memory(Store(app.state.settings.data_dir),Security(app.state.settings),app.state.credentials.same_requester,app.state.memory.checkpoints)
    assert MARKER in reopened.listing('google:alice')[0]['content']
    assert reopened.preferences('google:alice')['auto_save'] is False


@pytest.mark.parametrize('secret',['sk-secret-marker-123456789','password=hunter2','Authorization: Bearer abcdef0123456789','https://username:password@example.com','-----BEGIN RSA PRIVATE KEY-----'])
def test_secrets_are_rejected_without_echoing(workspace,secret):
    app,client=workspace
    sign_in(app,client)
    response=create(client,content='Never retain '+secret)
    assert response.status_code==422
    assert secret not in response.text
    assert client.get('/api/memory').json()['memories']==[]


def test_api_requires_authentication_csrf_and_rejects_spoofed_ownership(workspace):
    app,client=workspace
    sign_in(app,client)
    assert create(client,owner_id='google:bob').status_code==422
    assert create(client,content='x'*1201).status_code==422
    client.headers['X-CSRF-Token']='wrong'
    assert create(client).status_code==403
    assert client.put('/api/memory/preferences',json={'enabled':False}).status_code==403
    client.cookies.clear()
    assert client.get('/api/memory').status_code==401


def test_parallel_retries_commit_one_note(workspace):
    app,client=workspace
    sign_in(app,client)
    body=Note(key='atomic',title='Atomic memory',content='A preference to remember',request_id='atomic-save')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:app.state.memory.save('google:alice',body),range(8)))
    assert len({r['id'] for r in results})==1
    assert len(client.get('/api/memory').json()['memories'])==1


def test_broker_sends_private_context_without_leaking_it_to_tools_or_traces(workspace,monkeypatch):
    app,client=workspace
    sign_in(app,client)
    create(client)
    run=active(app)
    result=call(client,run,'memory_search',query='response preferences')
    assert MARKER not in result.text
    captured=[]
    app.state.settings.litellm_api_base='https://gateway.example/v1'
    app.state.settings.litellm_api_key='test-key'
    def upstream(request):
        messages=json.loads(request.content)['messages']
        captured.append(messages)
        assert messages[-1]=={'role':'system','content':'Platform instructions'}
        return httpx.Response(200,json={'id':'answer','choices':[{'message':{'role':'assistant','content':'Ready'}}]})
    actual=httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(upstream),**kw))
    response=client.post(f"/broker/{run['id']}/v1/chat/completions",headers={'Authorization':'Bearer capability'},json={'messages':[{'role':'system','content':'Platform instructions'}]})
    assert response.status_code==200 and MARKER in str(captured)
    assert MARKER not in response.text
    assert MARKER not in str(app.state.store.events(run['id']))
    assert MARKER not in str(app.state.store.messages(run['id']))
    assert MARKER not in str(app.state.store.rows('SELECT * FROM memory_selections'))


def test_private_memory_payloads_omitted_from_activity_and_snapshots():
    events=[]
    activity=ActivityReporter(lambda *event:events.append(event),tracing=True)
    activity.start('a','mcp_workspace_memory_save',{'content':MARKER})
    activity.complete('a','mcp_workspace_memory_save',{'content':MARKER},{'saved':True})
    assert MARKER not in json.dumps(events)
    messages=[{'role':'assistant','tool_calls':[
        {'id':'1','function':{'name':'mcp_workspace_memory_save','arguments':json.dumps({'content':MARKER})}},
        {'id':'2','function':{'name':'tool_call','arguments':json.dumps({'calls':[{'name':'mcp_workspace_memory_search','arguments':{'query':MARKER}}]})}},
        {'id':'3','function':{'name':'terminal','arguments':'{"command":"ls"}'}}]}]
    cleaned=scrub_memory_history(messages)
    assert MARKER not in json.dumps(cleaned)
    assert MARKER in json.dumps(messages)  # Do not mutate a live tool invocation.
    assert cleaned[0]['tool_calls'][2]==messages[0]['tool_calls'][2]
