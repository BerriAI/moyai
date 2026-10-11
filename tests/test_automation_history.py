"""Automation history remains complete across paging, callers and session reads."""
import json
from collections import Counter

import pytest
from fastapi import HTTPException

from app.automation_history import read_history
from app.db import now
from test_workspace import workspace
from test_spend import active, sign_in
from test_automation_tools import create


def tool(client, run, name, **arguments):
    return client.post('/broker/'+run['id']+'/tools/call', headers={'Authorization': 'Bearer capability'},
                       json={'name': name, 'arguments': arguments})


def seed(app, automation_id, owner, count=25):
    rows = []
    for index in range(count):
        run = app.state.store.create_run('Original task '+str(index), '', 'demo', [], chat_enabled=True, user_id=owner)
        app.state.store.update_run(run['id'], status='idle', summary=f'Outcome {index}: ' + 'Evidence 漢🙂 ' * 400)
        app.state.store.execute("INSERT INTO automation_runs VALUES(?,?,1,?,'started','',?)",
            (f'run-{index:03}', automation_id, run['id'], '2026-10-10T00:00:00+00:00'))
        rows.append(run)
    return rows


def test_keyset_pages_keep_all_outcomes_and_full_answers_with_tied_timestamps(workspace):
    app, client = workspace
    owner = sign_in(app, client)
    caller = active(app)
    saved = create(client, caller)
    runs = seed(app, saved['id'], owner)
    result = tool(client, caller, 'automation_history', automation_id=saved['id'], limit=7)
    assert result.status_code == 200, result.text
    observed = []
    while True:
        page = result.json()
        observed.extend(page['history'])
        if not page['next_arguments']:
            break
        result = tool(client, caller, 'automation_history', **page['next_arguments'])
        assert result.status_code == 200
    assert len({row['occurrence'] for row in observed}) == len(runs) == len(observed)
    assert all(row['summary'].endswith('Evidence 漢🙂 ' * 400) for row in observed)
    assert [row['occurrence'] for row in observed] == sorted((row['occurrence'] for row in observed), reverse=True)
    api = client.get('/api/automations').json()['automations'][0]
    assert len(api['history']) == 20 and api['history_cursor']
    older = client.get('/api/automations/'+saved['id']+'/history', params={'cursor': api['history_cursor']}).json()
    assert len(older['history']) == 5 and older['next_cursor'] is None
    assert 'summary' not in older['history'][0]


def test_automatic_and_delegated_callers_can_retrieve_only_their_own_automation(workspace):
    app, client = workspace
    owner = sign_in(app, client)
    direct = active(app)
    saved = create(client, direct)
    prior = seed(app, saved['id'], owner, 2)
    for delegated in (False, True):
        caller = active(app)
        if delegated:
            app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (prior[0]['id'], caller['id']))
        else:
            app.state.store.execute("INSERT INTO automation_runs VALUES(?,?,1,?,'started','',?)",
                ('current', saved['id'], caller['id'], now()))
        catalog = client.get('/broker/'+caller['id']+'/tools', headers={'Authorization': 'Bearer capability'}).json()
        names = Counter(row['name'] for row in catalog)
        assert names['sessions_read'] == names['automation_history'] == 1
        assert 'automation_create' not in names and 'automation_update' not in names
        result = tool(client, caller, 'automation_history')
        assert result.status_code == 200, result.text
        assert result.json()['automation_id'] == saved['id']
        read = tool(client, caller, 'sessions_read', session=prior[1]['id'])
        assert read.status_code == 200, read.text
        assert read.json()['messages'][0]['content'] == 'Original task 1'
        assert tool(client, caller, 'sessions_read', session=direct['id']).status_code == 403
        assert tool(client, caller, 'automation_history', automation_id='a'*32).status_code == 403


def test_history_is_owned_and_source_cursors_do_not_cross_automations(workspace):
    app, client = workspace
    owner = sign_in(app, client)
    caller = active(app)
    saved = create(client, caller)
    seed(app, saved['id'], owner, 2)
    cursor = read_history(app.state.store, saved['id'], limit=1)['next_cursor']
    with pytest.raises(HTTPException, match='cursor'):
        read_history(app.state.store, 'a'*32, cursor=cursor)
    for invalid in ('bad', 'e30=', 'W10='):
        with pytest.raises(HTTPException, match='cursor'):
            read_history(app.state.store, saved['id'], cursor=invalid)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    other = active(app, 'google:bob')
    assert tool(client, other, 'automation_history', automation_id=saved['id']).status_code == 404


def test_deleted_history_retains_outcome_without_session_body(workspace):
    app, client = workspace
    owner = sign_in(app, client)
    caller = active(app)
    saved = create(client, caller)
    run = seed(app, saved['id'], owner, 1)[0]
    app.state.store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), run['id']))
    result = tool(client, caller, 'automation_history', automation_id=saved['id']).json()
    assert result['history'][0]['summary'] == '' and result['history'][0]['status'] == 'deleting'


def test_definition_retains_large_instruction_metadata_and_trigger_sets(workspace):
    app, client = workspace
    sign_in(app, client)
    caller = active(app)
    prompt = 'Use all these instructions.\n' + 'full task 漢🙂 ' * 3000
    metadata = {str(i): 'Large context ' * 2000 + 'END' for i in range(25)}
    triggers = [{'id': f'trigger-{i}', 'schedule': {'frequency': 'daily', 'time': '09:00', 'timezone': 'UTC'}} for i in range(25)]
    saved = create(client, caller, prompt=prompt, metadata=metadata, triggers=triggers)
    assert saved['definition']['prompt'] == prompt.strip()
    assert saved['definition']['metadata'] == metadata
    assert len(saved['definition']['triggers']) == 25
