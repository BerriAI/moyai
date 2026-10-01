import json
from uuid import uuid4

from app.db import now
from test_workspace import workspace
from test_spend import sign_in


def seeded_group(app):
    store = app.state.store
    parent = store.create_run('Benchmark gateway models', '', 'demo', [], chat_enabled=True)
    child = store.create_run('Assigned cases 1 to 20', '', 'demo', [], chat_enabled=True)
    group = uuid4().hex
    store.execute("INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at) VALUES(?,?,1,'seed','{}','completed',?)", (group, parent['id'], now()))
    store.execute('UPDATE runs SET parent_run_id=?,agent_group_id=?,agent_label=? WHERE id=?', (parent['id'], group, 'Cases 1–20', child['id']))
    for run in (parent, child):
        store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (run['id'],))
        store.update_run(run['id'], status='idle', summary='Original result')
    return parent['id'], child['id'], group


def test_sidebar_nests_children_without_losing_other_parent_sessions(workspace):
    app, client = workspace
    parent, child, group = seeded_group(app)
    second, second_child, _ = seeded_group(app)
    data = client.get('/api/runs').json()
    assert {r['id'] for r in data} == {parent, second}
    assert [c['id'] for r in data if r['id'] == parent for c in r['children']] == [child]
    assert [c['id'] for r in data if r['id'] == second for c in r['children']] == [second_child]
    c = next(r for r in data if r['id'] == parent)['children'][0]
    assert c['agent_label'] == 'Cases 1–20' and c['status'] == 'idle'
    assert not {'summary','prompt','token_hash','pending_result'}.intersection(c)
    assert client.get('/api/runs/'+child).json()['parent_run_id'] == parent


def test_child_deep_link_keeps_older_parent_visible_beyond_list_limit(workspace):
    app, client = workspace
    parent, child, _ = seeded_group(app)
    for i in range(101):
        row = app.state.store.create_run(f'Newer session {i}', '', 'demo', [], chat_enabled=True)
        app.state.store.update_run(row['id'], status='idle')
    assert parent not in [r['id'] for r in client.get('/api/runs').json()]
    rows = client.get('/api/runs', params={'focus':child}).json()
    assert parent in [r['id'] for r in rows] and child == next(r for r in rows if r['id'] == parent)['children'][0]['id']
    # Resuming an old session must bring it back into the main list, not just
    # reorder whichever 100 sessions happened to be created most recently.
    app.state.store.update_run(parent, status='running')
    rows = client.get('/api/runs').json()
    assert len(rows) == 100 and rows[0]['id'] == parent
    assert rows[0]['children'][0]['id'] == child


def test_sidebar_orders_by_the_displayed_update_time_and_keeps_children_nested(workspace, monkeypatch):
    app, client = workspace
    store = app.state.store
    older, child, _ = seeded_group(app)
    newer, newer_child, _ = seeded_group(app)
    store.execute('UPDATE runs SET created_at=?,updated_at=? WHERE id=?',
                  ('2026-09-01T00:00:00+00:00', '2026-09-03T00:00:00+00:00', older))
    store.execute('UPDATE runs SET created_at=?,updated_at=? WHERE id=?',
                  ('2026-09-02T00:00:00+00:00', '2026-09-04T00:00:00+00:00', newer))
    assert [r['id'] for r in client.get('/api/runs').json()] == [newer, older]

    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    response = client.post('/api/runs/'+older+'/messages',
                           json={'content': 'Continue the older task', 'client_id': 'resume-older'})
    assert response.status_code == 202
    rows = client.get('/api/runs').json()
    assert [r['id'] for r in rows] == [older, newer]
    assert rows[0]['updated_at'] > rows[1]['updated_at']
    assert [c['id'] for r in rows for c in r['children']] == [child, newer_child]
    # Looking at an older session does not count as new activity.
    client.get('/api/runs/'+newer)
    assert [r['id'] for r in client.get('/api/runs').json()] == [older, newer]

    # Equal update times remain stable across refreshes.
    store.execute('UPDATE runs SET updated_at=? WHERE parent_run_id=?',
                  ('2026-09-05T00:00:00+00:00', ''))
    assert [r['id'] for r in client.get('/api/runs').json()] == [newer, older]


def test_authenticated_direct_chat_wakes_only_child_and_preserves_legacy_results(workspace, monkeypatch):
    app, client = workspace
    parent, child, group = seeded_group(app)
    sent = []
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: sent.append(run['id']))
    sign_in(app, client, 'bob', 'bob@berri.ai')
    body = {'content': 'Explain case 17', 'client_id': 'explain-case-17'}
    response = client.post('/api/runs/'+child+'/messages', json=body)
    assert response.status_code == 202 and response.json()['created']
    assert client.post('/api/runs/'+child+'/messages', json=body).json()['created'] is False
    assert set(sent) == {child}
    assert app.state.store.messages(child)[-1]['user_id'] == 'google:bob'
    assert app.state.store.run(parent)['status'] == 'idle'
    snapshot = json.loads(app.state.store.rows('SELECT result_snapshot FROM agent_groups WHERE id=?', (group,))[0]['result_snapshot'])
    assert snapshot[0]['summary'] == 'Original result'
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert client.post('/api/runs/'+child+'/messages', json=body).status_code == 409
    client.headers['X-CSRF-Token'] = 'incorrect'
    assert client.post('/api/runs/'+child+'/messages', json={**body,'client_id':'bad-csrf-new'}).status_code == 403
