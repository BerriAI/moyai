import json
from uuid import uuid4

from app.db import now
from test_workspace import workspace
from test_spend import sign_in
from test_slack import slack_app, event, signed
from test_slack_chat import start
from app.db import Store


def mine(client, **params):
    response = client.get('/api/runs', params={'scope': 'mine', **params})
    assert response.status_code == 200
    return {row['id'] for row in response.json()}


def test_my_sessions_created_or_participated_not_viewed_or_assistant(workspace, monkeypatch):
    app, client = workspace
    store = app.state.store
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    own = client.post('/api/runs', json={'prompt': 'My created session'}).json()['id']
    other = store.create_run('Shared session', '', 'demo', [], chat_enabled=True, user_id='google:alice')['id']
    unrelated = store.create_run('Unrelated session', '', 'demo', [], chat_enabled=True, user_id='google:alice')['id']
    store.execute("INSERT INTO messages(run_id,role,content,status,created_at,user_id) VALUES(?,'assistant','Attributed answer','completed',?,?)", (unrelated, now(), 'google:bob'))
    assert mine(client) == {own}
    assert client.get('/api/runs/'+other).status_code == 200
    assert mine(client, focus=other, user_id='google:alice') == {own}
    assert client.post('/api/runs/'+other+'/messages', json={'content': 'Joining via shared link', 'client_id': 'joining-shared'}).status_code == 202
    assert mine(client) == {own, other}
    store.execute("UPDATE messages SET status='deleted' WHERE run_id=? AND user_id='google:bob'", (other,))
    store.execute("UPDATE runs SET active_user_id='google:alice' WHERE id=?", (other,))
    assert mine(client) == {own, other}
    assert {r['id'] for r in client.get('/api/runs').json()} == {own, other}
    assert client.get('/api/runs?scope=all').status_code == 403
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert {r['id'] for r in client.get('/api/runs?scope=all').json()} == {own, other, unrelated}
    assert {r['id'] for r in client.get('/api/runs').json()} == {own, other, unrelated}
    assert mine(client) == {other, unrelated}
    assert client.get('/api/runs?scope=invalid').status_code == 422
    sign_in(app, client, 'nobody', 'nobody@berri.ai')
    assert mine(client) == set()
    assert client.get('/api/runs').json() == []
    assert client.get('/api/runs?scope=all&role=admin&user_id=google:alice').status_code == 403
    client.cookies.clear()
    assert client.get('/api/runs?scope=mine').status_code == 401
    assert client.get('/api/runs?scope=all').status_code == 401
    reopened = Store(app.state.settings.data_dir)
    assert set(reopened.sidebar_run_ids('google:bob')) == {own, other}
    assert reopened.sidebar_run_ids('') == []


def test_all_sessions_rechecks_role_after_demotion_without_new_login(workspace):
    app, client = workspace
    store = app.state.store
    own = store.create_run('Bob session', '', 'demo', [], user_id='google:bob')['id']
    other = store.create_run('Alice session', '', 'demo', [], user_id='google:alice')['id']
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert client.put('/api/admin/users/role', json={
        'email': 'bob@berri.ai', 'role': 'admin', 'revision': 0}).status_code == 200
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert {row['id'] for row in client.get('/api/runs?scope=all').json()} == {own, other}
    assert client.put('/api/admin/users/role', json={
        'email': 'bob@berri.ai', 'role': 'member', 'revision': 1}).status_code == 200
    assert client.get('/api/runs?scope=all').status_code == 403
    assert {row['id'] for row in client.get('/api/runs').json()} == {own}


def test_my_sessions_filters_before_limit_and_guards_folder_and_focus(workspace):
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    old = store.create_run('Old matching session', '', 'demo', [], user_id='google:bob')['id']
    for i in range(101):
        other = store.create_run(f'Unrelated {i}', '', 'demo', [], user_id='google:alice')['id']
    folder = client.post('/api/session-folders', json={'name': 'Research'}).json()['id']
    assert client.put('/api/runs/'+other+'/folder', json={'folder_id': folder}).status_code == 200
    assert mine(client, focus=other) == {old}
    assert {r['id'] for r in client.get('/api/runs', params={'focus': other}).json()} == {old}
    assert client.put('/api/runs/'+old+'/folder', json={'folder_id': folder}).status_code == 200
    for i in range(101):
        store.create_run(f'New matching {i}', '', 'demo', [], user_id='google:bob')
    assert len(mine(client)) == 101 and old in mine(client)
    assert other not in mine(client, focus=other)
    assert client.put('/api/runs/'+old+'/folder', json={'folder_id': None}).status_code == 200
    assert old not in mine(client)
    assert old in mine(client, focus=old)


def test_my_sessions_child_participant_includes_parent(workspace, monkeypatch):
    app, client = workspace
    parent, child, _ = seeded_group(app)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    assert mine(client, focus=child) == set()
    assert client.post('/api/runs/'+child+'/messages', json={'content': 'Participate in agent chat', 'client_id': 'child-mine-message'}).status_code == 202
    assert mine(client) == {parent}
    row = client.get('/api/runs?scope=mine').json()[0]
    assert row['children'][0]['id'] == child


def test_my_sessions_slack_participation_follows_persisted_identity_links(slack_app):
    app, client, run_id = start(slack_app)
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert mine(client) == set()
    payload = event(event_id='MineSecondSender', text='<@U99999999> Joining this session')
    payload['event'].update(user='U87654321', ts='1790720765.000001', thread_ts=store.slack_source(run_id)['thread_ts'])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert store.messages(run_id)[-1]['user_id'] == 'slack:T12345678:U87654321'
    assert mine(client) == set()  # Unresolved Slack profiles cannot guess identity.
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:bob', 'slack:T12345678:U87654321'))
    assert mine(client) == {run_id}
    store.execute('UPDATE users SET linked_user_id=NULL WHERE id=?', ('slack:T12345678:U87654321',))
    assert mine(client) == set()
    # The creator qualifies even without any messages (e.g. control-only creation).
    creator = store.run(run_id)['owner_id']
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:bob', creator))
    store.execute('DELETE FROM messages WHERE run_id=?', (run_id,))
    assert mine(client) == {run_id}



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
