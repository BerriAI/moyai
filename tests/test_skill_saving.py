import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.db import Store, now
from app.skills import Skills
from test_attachments import upload, storage_mode
from storage_fixture import MemoryObjects
from test_skills import assert_rejected, edit
from test_spend import active, sign_in
from test_workspace import workspace


def call(client, run, tool_name='skills_save', **arguments):
    return client.post('/broker/'+run['id']+'/tools/call', headers={'Authorization':'Bearer capability'},
                       json={'name':tool_name, 'arguments':arguments})


def form(**changes):
    return {'name':'team-review', 'scope':'personal', 'description':'Review a team change.', 'instructions':'# Review\nCheck evidence.\n',
            'request_id':'save-team-review', **changes}


def attach(app, client, run, name, content):
    file = upload(client,name,content).json()
    app.state.store.execute('UPDATE attachments SET message_id=? WHERE id=?', (run['active_message_id'],file['id']))
    return file['id']


def test_save_current_requester_explicit_personal_and_admin_org_only(workspace):
    app,client = workspace
    sign_in(app,client)
    run = active(app)
    names = {t['name'] for t in client.get('/broker/'+run['id']+'/tools', headers={'Authorization':'Bearer capability'}).json()}
    assert {'skills_search','skills_save','skills_load','skills_read_file'} <= names
    saved = call(client,run,**form()).json()
    assert saved['saved'] and saved['reference']=='personal:team-review' and saved['revision']==1
    assert client.get('/api/skills/'+saved['id']).json()['instructions']==form()['instructions']
    assert call(client,run,**form(scope='organization',request_id='save-org-review')).status_code==200
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    # Follow-up ownership uses active_user_id, even when the original owner is Alice.
    member = active(app,'google:ishaan')
    app.state.store.execute("UPDATE runs SET owner_id='google:alice' WHERE id=?",(member['id'],))
    assert_rejected(call(client,member,**form(scope='organization')), 403)
    mine = call(client,member,**form()).json()
    assert app.state.store.rows('SELECT owner_id FROM skills WHERE id=?',(mine['id'],))[0]['owner_id']=='google:ishaan'
    for fake in ({'owner_id':'google:alice'},{'admin':True}):
        invalid = call(client,member,**form(**fake))
        assert invalid.status_code==200 and 'Invalid skill arguments' in invalid.json()['error']
    sign_in(app,client)
    assert client.get('/api/skills/'+mine['id']).status_code==404
    app.state.store.update_run(member['id'],token_hash='')
    assert call(client,member,**form()).status_code==401


def test_unspecified_skill_scope_returns_question_and_saves_nothing(workspace):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    args=form()
    del args['scope']
    question=call(client,run,**args)
    assert question.status_code==200
    assert question.json()['status']=='scope_required' and question.json()['saved'] is False
    assert [c['scope'] for c in question.json()['choices']]==['personal','organization']
    assert all(c['available'] for c in question.json()['choices'])
    assert not app.state.store.rows('SELECT * FROM skills')
    assert not app.state.store.rows('SELECT * FROM skill_saves')
    # Asking the question consumes no save receipt; an explicit reply can save.
    assert call(client,run,**args,scope='organization').json()['scope']=='organization'
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    member=active(app,'google:ishaan')
    choices=call(client,member,**args).json()['choices']
    assert choices[0]['available'] and not choices[1]['available']
    assert_rejected(call(client,member,**args,scope='organization'), 403)


@pytest.mark.parametrize('scope',[None,''])
def test_library_api_requires_explicit_skill_scope(workspace,scope):
    app,client=workspace
    sign_in(app,client)
    body={k:v for k,v in form().items() if k not in {'request_id','scope'}}
    body['client_id']='scope-choice-test'
    if scope is not None:
        body['scope']=scope
    response=client.post('/api/skills',json=body)
    assert response.status_code==422 and 'Personal or Organization' in response.text
    assert not app.state.store.rows('SELECT * FROM skills')


def test_atomic_save_replay_concurrent_updates_and_no_duplicate_audit(workspace):
    app,client = workspace
    sign_in(app,client)
    run = active(app)
    args = form(files=[{'path':'references/checks.md','content':'Original reference.'}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: call(client,run,**args),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert responses[0].json()==responses[1].json()
    assert len(app.state.store.rows('SELECT * FROM skill_audit'))==1
    saved = responses[0].json()
    change = form(expected_revision=1,request_id='update-review-1',instructions=None,
                  files=[{'path':'references/checks.md','content':'New reference.'}])
    assert call(client,run,**change).json()['revision']==2
    assert call(client,run,**change).json()['revision']==2
    assert call(client,run,**args).json()==saved  # Lost initial response after a later edit.
    assert_rejected(call(client,run,**form(description='Conflicting retry')), 409)
    assert_rejected(call(client,run,**form(expected_revision=1,request_id='stale-save-1')), 409)
    assert len(app.state.store.rows('SELECT * FROM skill_audit'))==2
    assert client.get('/api/skills/'+saved['id']).json()['revision']==2
    assert client.get('/api/skills/'+saved['id']+'/files/references/checks.md').text=='New reference.'


def test_large_attachments_persist_exactly_and_references_are_private_pinned_and_bounded(workspace,monkeypatch,storage_mode):
    app,client = workspace
    sign_in(app,client)
    run = active(app)
    raw = ('runbook-secret-marker\n'+'Reference line ü\n'*22000).encode()
    assert len(raw)>354109
    instructions = attach(app,client,run,'SKILL.md',b'# Team review\n\nRead references/runbook.md.\n')
    reference = attach(app,client,run,'runbook.md',raw)
    saved = call(client,run,**form(instructions=None,instructions_attachment_id=instructions,
                                 files=[{'path':'references/runbook.md','attachment_id':reference}])).json()
    skill_id = saved['id']
    file_url = '/api/skills/'+skill_id+'/files/references/runbook.md'
    assert client.get(file_url).content==raw
    assert 'runbook-secret-marker' not in json.dumps(app.state.store.rows('SELECT * FROM skill_bundles'))
    later = active(app)  # A later session has no original attachments.
    loaded = call(client,later,'skills_load',name='personal:team-review')
    assert loaded.json()['files'][0]['size']==len(raw)
    assert 'runbook-secret-marker' not in loaded.text+app.state.skills.context(later)
    result = call(client,later,'skills_read_file',name='personal:team-review',path='references/runbook.md',limit=40)
    assert result.status_code==200 and result.json()['next_offset']==40
    assert 'runbook-secret-marker' not in result.text
    assert 'runbook-secret-marker' in app.state.skills.context(later)
    for offset in range(100,600,100):
        assert call(client,later,'skills_read_file',name='personal:team-review',path='references/runbook.md',offset=offset,limit=80).status_code==200
    assert len(app.state.store.rows('SELECT * FROM skill_file_reads'))==4
    # Search can bring a relevant excerpt back without replaying the whole file.
    assert call(client,later,'skills_read_file',name='personal:team-review',path='references/runbook.md',search='runbook-secret-marker',limit=40).status_code==200
    missing = call(client,later,'skills_read_file',name='personal:team-review',path='references/runbook.md',search='not-in-this-document')
    assert missing.json()['found'] is False
    assert 'runbook-secret-marker' not in client.get('/api/runs/'+later['id']).text
    assert 'runbook-secret-marker' not in json.dumps(app.state.manager.spec(later))
    # Excerpts travel only on inference, not in shared transcript or tool responses.
    app.state.settings.litellm_api_base='https://gateway.example/v1'
    app.state.settings.litellm_api_key='test-key'
    def gateway(request):
        content=json.loads(request.content)['messages'][0]['content']
        assert 'runbook-secret-marker' in content and len(content)<10000
        return httpx.Response(200,json={'choices':[],'usage':{'total_tokens':10}})
    actual=httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(gateway),**kw))
    assert client.post('/broker/'+later['id']+'/v1/chat/completions',headers={'Authorization':'Bearer capability'},json={'messages':[]}).status_code==200
    # New revisions cannot silently replace references in an already loaded turn.
    update = call(client,run,**form(instructions=None,expected_revision=1,request_id='replace-runbook',
                                  files=[{'path':'references/runbook.md','content':'Changed reference.'}]))
    assert update.json()['revision']==2
    assert 'runbook-secret-marker' in app.state.skills.context(later)
    reopened = Store(app.state.settings.data_dir)
    skills = Skills(reopened,app.state.security,app.state.credentials.same_requester)
    assert 'runbook-secret-marker' in skills.context(later)
    fresh = active(app)
    assert call(client,fresh,'skills_read_file',name='personal:team-review',path='references/runbook.md').json()['revision']==2
    assert 'Changed reference.' in app.state.skills.context(fresh)
    # UI edits and archive/restore revisions preserve files.
    assert edit(client,skill_id,description='Edited in the library').status_code==200
    assert client.get(file_url).text=='Changed reference.'
    assert client.post('/api/skills/'+skill_id+'/archive',json={'archived':True,'revision':3}).status_code==200
    assert 'runbook-secret-marker' not in skills.context(later)
    assert client.post('/api/skills/'+skill_id+'/archive',json={'archived':False,'revision':4}).status_code==200
    assert client.get(file_url).text=='Changed reference.'
    sign_in(app,client,'ishaan','ishaan@berri.ai')
    assert client.get(file_url).status_code==404
    app.state.store.execute("UPDATE runs SET active_user_id='google:ishaan' WHERE id=?",(later['id'],))
    assert 'runbook-secret-marker' not in skills.context(app.state.store.run(later['id']))


def test_attachment_cutoff_and_failed_import_leave_no_partial_skill(workspace,storage_mode):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    draft=upload(client,'private.md',b'Draft document').json()['id']
    other=active(app)
    other_id=attach(app,client,other,'other.md',b'Other session document')
    future,_=app.state.store.enqueue_message(run['id'],'Later message','future-message',user_id='google:alice')
    future_id=upload(client,'future.md',b'Future document').json()['id']
    app.state.store.execute('UPDATE attachments SET message_id=? WHERE id=?',(future['id'],future_id))
    for attachment_id in (draft,other_id,future_id):
        result=call(client,run,**form(files=[{'path':'references/private.md','attachment_id':attachment_id}]))
        assert_rejected(result, 404)
    assert not app.state.store.rows('SELECT * FROM skills')
    assert not app.state.store.rows('SELECT * FROM skill_saves')


@pytest.mark.parametrize('path',['../escape','/absolute','a/../escape','a//b','a\\b','SKILL.md','.hidden','a/./b'])
def test_skill_paths_rejected(workspace,path):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    invalid=call(client,run,**form(files=[{'path':path,'content':'private content'}]))
    assert invalid.status_code==200 and 'Invalid skill arguments' in invalid.json()['error']
    assert 'private content' not in invalid.text
    assert not app.state.store.rows('SELECT * FROM skills')


def test_file_limits_invalid_text_removal_and_rollback(workspace,storage_mode):
    app,client=workspace
    sign_in(app,client)
    run=active(app)
    for raw,status in [(b'x'*(1024*1024+1),413),(b'binary\x00content',422),(b'\xff\xff',422)]:
        attachment_id=attach(app,client,run,'reference.md',raw)
        assert_rejected(call(client,run,**form(files=[{'path':'ref.md','attachment_id':attachment_id}])), status)
    assert not app.state.store.rows('SELECT * FROM skills')
    saved=call(client,run,**form(files=[{'path':'ref.md','content':'Keep this file.'}])).json()
    large=attach(app,client,run,'large.md',b'x'*(1024*1024))
    result=call(client,run,**form(expected_revision=1,request_id='oversized-update',
        files=[{'path':f'ref{i}.md','attachment_id':large} for i in range(5)]))
    assert_rejected(result, 413)
    assert client.get('/api/skills/'+saved['id']).json()['revision']==1
    removed=call(client,run,**form(expected_revision=1,request_id='remove-reference',remove_files=['ref.md']))
    assert removed.json()['files']==[]
    assert client.get('/api/skills/'+saved['id']+'/files/ref.md').status_code==404


def test_slack_save_uses_verified_google_owner_and_live_admin_policy(workspace):
    app,client=workspace
    sign_in(app,client)
    store=app.state.store
    store.execute("INSERT INTO users(id,kind,email,name,linked_user_id,created_at,updated_at) VALUES('slack:member','slack','wrong@berri.ai','Slack user','google:alice',?,?)",(now(),now()))
    run=active(app,'slack:member')
    assert_rejected(call(client,run,**form()), 403)
    store.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=? WHERE id='slack:member'",(now(),))
    saved=call(client,run,**form()).json()
    assert store.rows('SELECT owner_id FROM skills WHERE id=?',(saved['id'],))[0]['owner_id']=='google:alice'
    assert client.get('/api/skills/'+saved['id']).status_code==200
    assert call(client,run,**form(scope='organization',request_id='slack-shared-save')).status_code==200
    app.state.settings.google_admin_emails='other@berri.ai'
    assert_rejected(call(client,run,**form(scope='organization',request_id='slack-shared-save')), 403)
    store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id='slack:member'")
    assert_rejected(call(client,run,**form()), 403)
    store.execute("UPDATE users SET profile_checked_at=?,profile_conflict=1 WHERE id='slack:member'",(now(),))
    assert_rejected(call(client,run,**form()), 403)


def test_nonchat_and_unknown_identities_cannot_write_skills(workspace):
    app,client=workspace
    sign_in(app,client)
    unknown=active(app,'google:unknown')
    assert_rejected(call(client,unknown,**form()), 403)
    run=active(app)
    app.state.store.execute('UPDATE runs SET chat_enabled=0 WHERE id=?',(run['id'],))
    assert_rejected(call(client,run,**form()), 403)


def test_remote_skill_import_has_no_writer_lock_and_completed_retry_survives_outage(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    app.state.store.objects = backend = MemoryObjects()
    attachment_id = attach(app, client, run, 'reference.md', b'Privately imported reference.')
    original_read = backend.read
    def unlocked_read(reference, limit):
        app.state.store.execute('UPDATE organization SET name=name')
        return original_read(reference, limit)
    monkeypatch.setattr(backend, 'read', unlocked_read)
    args = form(files=[{'path': 'reference.md', 'attachment_id': attachment_id}])
    saved = call(client, run, **args)
    assert saved.status_code == 200, saved.text
    backend.fail = True
    assert call(client, run, **args).json() == saved.json()
    failed = call(client, run, **{**args, 'request_id': 'retry-during-outage', 'expected_revision': 1})
    assert failed.status_code == 503
    assert app.state.store.rows('SELECT revision FROM skills')[0]['revision'] == 1
    assert len(app.state.store.rows('SELECT * FROM skill_saves')) == 1


@pytest.mark.parametrize('change', ['scope', 'content', 'turn'])
def test_skill_import_revalidates_authorized_identity_after_remote_read(workspace, monkeypatch, change):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    store = app.state.store
    store.objects = backend = MemoryObjects()
    attachment_id = attach(app, client, run, 'reference.md', b'Private reference before a concurrent change.')
    original_read = backend.read
    def raced_read(reference, limit):
        raw = original_read(reference, limit)
        if change == 'scope':
            store.execute("UPDATE messages SET status='deleted' WHERE id=?", (run['active_message_id'],))
        elif change == 'content':
            store.execute("UPDATE attachments SET sha256='changed' WHERE id=?", (attachment_id,))
        else:
            store.execute("UPDATE runs SET active_user_id='google:someone-else' WHERE id=?", (run['id'],))
        return raw
    monkeypatch.setattr(backend, 'read', raced_read)
    response = call(client, run, **form(files=[{'path': 'reference.md', 'attachment_id': attachment_id}]))
    assert_rejected(response, 404 if change == 'scope' else 409)
    assert not store.rows('SELECT * FROM skills')
    assert not store.rows('SELECT * FROM skill_saves')
