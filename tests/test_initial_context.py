"""Private first-inference context, without agent-driven discovery round trips."""
import json

import httpx
import pytest

from app.memory import MAX_AUTOMATIC_CONTEXT, MAX_CONTEXT
from app.skills import MAX_INDEX_CONTEXT, MAX_INDEX_SKILLS
from test_memory import create as memory, source, call as memory_call
from test_skills import create as skill, edit, skill_context, search
from test_spend import active, sign_in
from test_workspace import workspace


def memory_context(app, run):
    return json.loads(app.state.memory.context(run).split('\n', 1)[1])


@pytest.mark.parametrize('route', ['chat/completions', 'messages', 'responses'])
def test_first_inference_gets_private_context_and_loads_skill_on_demand(workspace, monkeypatch, route):
    app, client = workspace
    sign_in(app, client)
    memory(client, content='Prefer concise explanations. PRIVATE_NOTE')
    skill_id = skill(client, instructions='PRIVATE_PROCEDURE: verify benchmark coverage').json()['id']
    edit(client, skill_id, description='PRIVATE_DESCRIPTION: review benchmarks')
    run = active(app)
    captured = []
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    actual = httpx.AsyncClient
    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id':'response', 'choices':[], 'usage':{'total_tokens':10}})
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))
    url = f"/broker/{run['id']}/v1/{route}"
    headers = {'Authorization':'Bearer capability'}
    payload = {'messages':[], 'input':[], 'stream':False}
    assert client.post(url, headers=headers, json=payload).status_code == 200
    first = json.dumps(captured[-1])
    assert 'PRIVATE_NOTE' in first and 'PRIVATE_DESCRIPTION' in first
    assert 'PRIVATE_PROCEDURE' not in first
    assert not app.state.store.rows('SELECT * FROM memory_selections')
    assert not app.state.store.rows('SELECT * FROM skill_searches')
    assert not app.state.store.rows('SELECT * FROM skill_uses')
    # The directory supports direct loading, without a preliminary search.
    result = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
        json={'name':'skills_load', 'arguments':{'name':'personal:benchmark-review'}})
    assert result.json()['loaded'] and 'PRIVATE_' not in result.text
    assert client.post(url, headers=headers, json=payload).status_code == 200
    assert 'PRIVATE_PROCEDURE' in json.dumps(captured[-1])
    assert skill_context(app, run)['available'] == []  # No duplicate directory entry.
    public = json.dumps(app.state.manager.spec(run)) + client.get('/api/runs/'+run['id']).text
    public += json.dumps(app.state.store.events(run['id']))
    assert 'PRIVATE_' not in public
    assert app.state.store.rows('SELECT tainted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['tainted'] & 1
    # Revalidate even after the model has seen the context.
    assert client.post('/api/skills/'+skill_id+'/archive', json={'archived':True,'revision':2}).status_code == 200
    assert client.put('/api/memory/preferences', json={'enabled':False,'auto_save':False}).status_code == 200
    assert client.post(url, headers=headers, json=payload).status_code == 200
    assert 'PRIVATE_' not in json.dumps(captured[-1])


def test_directory_bounds_ranks_metadata_and_preserves_search(workspace):
    app, client = workspace
    sign_in(app, client)
    for i in range(16):
        skill(client, name=f'workflow-{i}', client_id=f'workflow-{i}')
    relevant = skill(client, name='zebra', client_id='zebra-workflow').json()['id']
    edit(client, relevant, description='Inspect sqlite replication durability.')
    run = active(app)
    source(app, run, 'Investigate sqlite replication durability')
    context = skill_context(app, run)
    assert context['available'][0]['reference'] == 'personal:zebra'
    assert len(context['available']) == MAX_INDEX_SKILLS and context['omitted'] == 5
    assert len(json.dumps(context['available'], ensure_ascii=False)) <= MAX_INDEX_CONTEXT
    assert not context['loaded'] and not context['matches']
    # An omitted workflow remains searchable; search wins over automatic ranking.
    assert search(client, run, 'personal:workflow-9').json()['matches'][0]['reference'] == 'personal:workflow-9'
    context = skill_context(app, run)
    references = [s['reference'] for s in context['matches'] + context['available']]
    assert len(references) == len(set(references))


def test_directory_rechecks_requester_scope_revision_and_archive(workspace):
    app, client = workspace
    sign_in(app, client)
    personal = skill(client).json()['id']
    shared = skill(client, 'organization', name='shared', client_id='shared-workflow').json()['id']
    run = active(app)
    assert len(skill_context(app, run)['available']) == 2
    edit(client, personal, description='Updated private discovery hint')
    assert skill_context(app, run)['available'][0]['revision'] == 2
    edit(client, shared, scope='personal')
    # A stale run object cannot carry Alice's directory into another requester.
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
    assert skill_context(app, run)['available'] == []
    app.state.store.execute("UPDATE runs SET active_user_id='google:alice' WHERE id=?", (run['id'],))
    client.post('/api/skills/'+personal+'/archive', json={'archived':True,'revision':2})
    assert [s['reference'] for s in skill_context(app, run)['available']] == ['personal:shared']


def test_directory_budget_counts_json_escaping(workspace):
    app, client = workspace
    sign_in(app, client)
    for i in range(12):
        skill_id = skill(client, name=f'workflow-{i}', client_id=f'workflow-{i}').json()['id']
        edit(client, skill_id, description='"'*320)
    context = skill_context(app, active(app))
    assert 0 < len(context['available']) < MAX_INDEX_SKILLS
    assert len(json.dumps(context['available'], ensure_ascii=False)) <= MAX_INDEX_CONTEXT
    assert len(context['available']) + context['omitted'] == 12


def test_automatic_ranking_uses_only_delivered_requester_messages(workspace):
    app, client = workspace
    sign_in(app, client)
    special = memory(client, key='orchid', request_id='orchid-note', title='Orchid care',
        kind='reference', content='Water orchids sparingly.').json()['id']
    run = active(app)
    queued, _ = app.state.store.enqueue_message(run['id'], 'orchid care', 'queued-steering', user_id='google:alice')
    foreign, _ = app.state.store.enqueue_message(run['id'], 'orchid care', 'foreign-steering', user_id='google:bob')
    app.state.store.execute("UPDATE messages SET steering_parent_id=?,status='injected' WHERE id=?",
        (run['active_message_id'],foreign['id']))
    assert not memory_context(app, run)['notes']
    app.state.store.execute("UPDATE messages SET steering_parent_id=?,status='injected' WHERE id=?",
        (run['active_message_id'],queued['id']))
    assert [n['id'] for n in memory_context(app, run)['notes']] == [special]


def test_automatic_memory_bounds_relevance_and_explicit_priority(workspace):
    app, client = workspace
    sign_in(app, client)
    for i in range(8):
        memory(client, key=f'note-{i}', request_id=f'note-save-{i}', content='Prefer concise prose. '+'x'*1050)
    matched = memory(client, key='replication', request_id='replication-note', kind='project',
        content='Check sqlite replication durability before release.').json()['id']
    irrelevant = memory(client, key='gardening', request_id='gardening-note', kind='reference',
        content='Orchid watering schedule.').json()['id']
    run = active(app)
    source(app, run, 'Investigate sqlite replication durability')
    notes = memory_context(app, run)['notes']
    assert notes[0]['id'] == matched and irrelevant not in {n['id'] for n in notes}
    assert len(notes) <= 3 and len(json.dumps(notes, ensure_ascii=False)) <= MAX_AUTOMATIC_CONTEXT
    assert memory_call(client, run, 'memory_search', query='orchid').json()['loaded'] == 1
    notes = memory_context(app, run)['notes']
    assert notes[0]['id'] == irrelevant and len(notes) <= 5
    assert len(json.dumps(notes, ensure_ascii=False)) <= MAX_CONTEXT
    assert len({n['id'] for n in notes}) == len(notes)


def test_automatic_memory_rechecks_repository_expiry_edits_deletion_and_identity(workspace):
    app, client = workspace
    sign_in(app, client)
    note_id = memory(client, repo_url='https://github.com/BerriAI/moyai', content='Scoped preference').json()['id']
    run = active(app)
    source(app, run, 'Work on https://github.com/BerriAI/moyai')
    assert not memory_context(app, run)['notes']  # Mention is not selection.
    app.state.store.execute("UPDATE runs SET repo_url='https://github.com/BerriAI/moyai' WHERE id=?", (run['id'],))
    assert memory_context(app, run)['notes'][0]['id'] == note_id
    note = client.get('/api/memory').json()['memories'][0]
    body = {k:note[k] for k in ('key','title','kind','repo_url','revision')}
    client.put('/api/memory/'+note_id, json={**body,'content':'Revised preference','request_id':'revised-note'})
    assert memory_context(app, run)['notes'][0]['content'] == 'Revised preference'
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
    assert app.state.memory.context(run) == ''
    app.state.store.execute("UPDATE runs SET active_user_id='google:alice' WHERE id=?", (run['id'],))
    app.state.store.execute("UPDATE personal_memories SET expires_at='2000-01-01' WHERE id=?", (note_id,))
    assert not memory_context(app, run)['notes']
    app.state.store.execute("UPDATE personal_memories SET expires_at='' WHERE id=?", (note_id,))
    assert client.request('DELETE', '/api/memory/'+note_id, json={'revision':2}).status_code == 200
    assert not memory_context(app, run)['notes']
