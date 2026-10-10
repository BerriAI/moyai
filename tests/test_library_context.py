"""Library preservation, revision pinning, scope and model-view contracts."""
import json

import pytest
from fastapi import HTTPException

from agent.repository_guidance import resolve
from agent.skills import catalog, read
from app.library_views import fit_context, page
from app.memory_review import review_pages
from test_skills import create, edit, search, skill_context
from test_skill_saving import call as skill_call, form
from test_spend import active, sign_in
from test_workspace import workspace
from test_memory import learn, source
from test_github import connected, select


def test_full_skill_catalog_pages_and_rejects_changed_snapshot(workspace):
    app, client = workspace
    sign_in(app, client)
    for number in range(71):
        assert create(client, name=f'procedure-{number}', client_id=f'procedure-{number}').status_code == 201
    run = active(app)
    cursor, found = '', []
    while True:
        result = search(client, run, '', cursor=cursor, page_size=11).json()
        found.extend(result['matches'])
        if not result['next']:
            break
        cursor = result['next']
    assert len(found) == len({entry['reference'] for entry in found}) == 71
    first = search(client, run, '', page_size=2).json()
    create(client, name='new-procedure', client_id='new-procedure')
    assert search(client, run, '', cursor=first['next']).json()['status_code'] == 409


def test_large_primary_skill_has_pinned_private_range_reads(workspace):
    app, client = workspace
    sign_in(app, client)
    body = '# Procedure\n' + '完全な説明\n' * 20000 + 'Last requirement.'
    saved = create(client, instructions=body).json()
    run = active(app)
    app.state.skills.load(run, 'personal:benchmark-review')
    view = json.loads(fit_context(app.state.skills.context(run), 8000).split('\n', 1)[1])
    assert view['loaded'][0]['instructions_omitted']
    offset, chunks = 0, []
    # Six large read receipts survive, beyond the former four-excerpt policy.
    for i in range(6):
        result = skill_call(client, run, 'skills_read_file', name='personal:benchmark-review',
                            path='SKILL.md', offset=i*1000, limit=1000).json()
        assert result['loaded'] and body[:10] not in json.dumps(result)
    assert len(app.state.store.rows('SELECT * FROM skill_file_reads')) == 6
    assert edit(client, saved['id'], instructions='A newer procedure.').status_code == 200
    result = skill_call(client, run, 'skills_read_file', name='personal:benchmark-review',
                        path='SKILL.md', offset=len(body)-17, limit=17).json()
    assert result['revision'] == 1 and result['next_offset'] is None
    assert skill_context(app, run)['loaded'][0]['excerpts'][-1]['content'] == body[-17:]


def test_builtin_versions_and_supporting_files_do_not_silently_change(tmp_path, monkeypatch):
    import agent.skills as skills
    bundle = tmp_path/'example'
    bundle.mkdir()
    (bundle/'SKILL.md').write_text('---\ndescription: Example\n---\nRead guide.md.')
    (bundle/'guide.md').write_text('First revision')
    monkeypatch.setattr(skills, 'ROOT', tmp_path)
    item = catalog()[0]
    assert read('example', item['revision'], 'guide.md')['content'] == 'First revision'
    (bundle/'guide.md').write_text('Changed revision')
    with pytest.raises(ValueError, match='version changed'):
        read('example', item['revision'], 'guide.md')
    with pytest.raises(ValueError):
        read('example', catalog()[0]['revision'], '../outside')


def test_repository_guidance_respects_directory_scope_and_large_ranges(tmp_path):
    (tmp_path/'AGENTS.md').write_text('Root instructions')
    nested=tmp_path/'package'
    nested.mkdir()
    (nested/'AGENTS.md').write_text('Narrow scope\n' * 5000)
    sibling=tmp_path/'other'
    sibling.mkdir()
    result=resolve(['package/file.py', 'other/file.py'], workspace=tmp_path)
    assert [len(item['guidance']) for item in result['paths']] == [2,1]
    guide=result['paths'][0]['guidance'][-1]
    chunks=[]
    offset=0
    while True:
        item=resolve([],workspace=tmp_path,source=guide['path'],offset=offset,characters=10000)
        chunks.append(item['content'])
        if item['next_offset'] is None:
            break
        offset=item['next_offset']
    assert ''.join(chunks)==(nested/'AGENTS.md').read_text()
    with pytest.raises(ValueError):
        resolve(['../outside.py'],workspace=tmp_path)


def test_memory_review_pages_preserve_large_unicode_and_tail_evidence():
    text='Full source "\\n界' * 10000 + 'Last correction: use p99.'
    sources=[{'id':1,'content':text},{'id':2,'content':'Another user input'}]
    batches=review_pages(sources, 12000)
    assert len(batches)>3
    restored={1:'',2:''}
    for batch in batches:
        assert len(json.dumps(batch,ensure_ascii=False).encode()) <= 12000
        for source in batch:
            assert len(restored[source['id']])==source['offset']
            restored[source['id']]+=source['content']
    assert restored=={source['id']:source['content'] for source in sources}


def test_view_allocation_preserves_retrieval_metadata_and_varies_with_window():
    raw='Private library\n'+json.dumps({'turn_id':1,'available':[{'reference':f'personal:procedure-{i}',
        'description':'Procedural discovery metadata '+str(i),'revision':1} for i in range(200)]})
    small=fit_context(raw, 3000)
    large=fit_context(raw, 12000)
    assert len(small.encode()) <= 3000 and len(large.encode()) <= 12000
    assert len(json.loads(small.split('\n')[1])['available']) < len(json.loads(large.split('\n')[1])['available'])


def test_large_orientation_and_repository_index_cannot_exceed_view_budget():
    raw = 'Long orientation ' * 1000 + '\n' + json.dumps({'notes': [],
        'confirmed_repositories': [{'id': i, 'url': 'https://github.com/org/repo-'+str(i)} for i in range(500)]})
    for allocation in (10, 400, 8000):
        assert len(fit_context(raw, allocation).encode()) <= allocation


def test_memory_can_use_confirmed_identity_without_picker_and_rechecks_access(workspace):
    app, client = workspace
    sign_in(app, client)
    connected(app)
    select(app, (101, 202))
    run = active(app)
    app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?', ('["github"]', run['id']))
    run = app.state.store.run(run['id'])
    source(app, run)
    assert not run['repo_url']
    saved = learn(client, run, repository_id=202)
    assert saved.json()['saved'], saved.text
    assert app.state.memory.listing('google:alice')[0]['repo_url'] == 'https://github.com/berriai/moyai'
    # The connected catalog is authority for scope, not a claim of checkout.
    assert not app.state.store.run(run['id'])['repo_url']
    select(app, (101,))
    rejected = learn(client, run, repository_id=202, key='revoked-project', request_id='revoked-project')
    assert rejected.json()['status_code'] == 422
    app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?', ('[]', run['id']))
    rejected = learn(client, run, repository_id=101, key='disabled-project', request_id='disabled-project')
    assert rejected.json()['status_code'] == 422


@pytest.mark.parametrize('surface', ['non-chat', 'automation', 'child'])
def test_bundled_procedures_available_on_all_execution_surfaces(workspace, surface):
    app, client = workspace
    run = active(app)
    if surface == 'non-chat':
        app.state.store.execute('UPDATE runs SET chat_enabled=0 WHERE id=?', (run['id'],))
    elif surface == 'child':
        parent = active(app)
        app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent['id'], run['id']))
    else:
        # Automation mode has no live requester or chat-memory context.
        app.state.store.execute("UPDATE runs SET chat_enabled=0,active_user_id='' WHERE id=?", (run['id'],))
    headers = {'Authorization': 'Bearer capability'}
    tools = client.get('/broker/'+run['id']+'/tools', headers=headers).json()
    assert 'bundled_skill_read' in {tool['name'] for tool in tools}
    entry = next(item for item in catalog() if item['name'] == 'goals')
    result = client.post('/broker/'+run['id']+'/tools/call', headers=headers,
        json={'name': 'bundled_skill_read', 'arguments': {'name': entry['name'], 'revision': entry['revision']}})
    assert result.json()['content'] == read(entry['name'], entry['revision'])['content']
