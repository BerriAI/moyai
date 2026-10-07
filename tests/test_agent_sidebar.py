import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    listing = {row['id']: row for row in client.get('/api/runs?scope=mine').json()}
    assert listing[own]['participated'] is False and listing[other]['participated'] is True
    assert listing[own]['pinned'] is False and listing[other]['slack_connected'] is False
    assert client.get('/api/runs/'+other).json()['participated'] is True
    store.execute("UPDATE messages SET status='deleted' WHERE run_id=? AND user_id='google:bob'", (other,))
    store.execute("UPDATE runs SET active_user_id='google:alice' WHERE id=?", (other,))
    assert mine(client) == {own, other}
    assert client.get('/api/runs/'+other).json()['participated'] is True
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
    assert row['participated'] is True
    store = app.state.store
    store.execute("DELETE FROM messages WHERE run_id=? AND user_id='google:bob'", (child,))
    store.execute("UPDATE runs SET owner_id='google:bob' WHERE id=?", (child,))
    assert mine(client) == {parent}  # A directly owned child also contributes to its parent.
    assert client.get('/api/runs?scope=mine').json()[0]['participated'] is True


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
    row = client.get('/api/runs?scope=mine').json()[0]
    assert row['participated'] is True and row['slack_connected'] is True
    store.execute('UPDATE users SET linked_user_id=NULL WHERE id=?', ('slack:T12345678:U87654321',))
    assert mine(client) == set()
    # The creator qualifies even without any messages (e.g. control-only creation).
    creator = store.run(run_id)['owner_id']
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:bob', creator))
    store.execute('DELETE FROM messages WHERE run_id=?', (run_id,))
    assert mine(client) == {run_id}
    assert client.get('/api/runs?scope=mine').json()[0]['participated'] is False


def test_slack_source_metadata_covers_legacy_events_and_live_bindings(workspace):
    app, client = workspace
    store = app.state.store
    legacy = store.create_slack_run('legacy-source', 'Old Slack session', [], 'COLD', '1.0', 'UOLD', team_id='TOLD')
    live = store.create_run('Bound session', '', 'demo', [])
    merely_enabled = store.create_run('Slack tools enabled', '', 'demo', ['slack'])
    store.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)',
                  ('TNEW', 'CNEW', '2.0', live['id'], '2.0'))
    rows = {row['id']: row for row in client.get('/api/runs?scope=all').json()}
    assert rows[legacy['id']]['slack_connected'] is True
    assert rows[live['id']]['slack_connected'] is True
    assert rows[merely_enabled['id']]['slack_connected'] is False



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


def test_session_search_finds_saved_conversation_before_recent_limit(workspace: tuple[FastAPI, TestClient]) -> None:
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    old = store.create_run('A neutral title', '', 'demo', [], chat_enabled=True, user_id='google:bob')['id']
    initial = store.claim_message(old)
    store.finish_message(old, initial['id'], 'Before ' * 100 + 'Cobalt checksum validated.' + ' After' * 100)
    store.enqueue_message(old, 'Follow up on the violet migration', 'search-followup', user_id='google:bob')
    for i in range(101):
        store.create_run(f'New unrelated task {i}', '', 'demo', [], user_id='google:bob')
    assert old not in mine(client)
    for term in ('  COBALT checksum  ', 'violet migration'):
        rows = client.get('/api/runs', params={'scope': 'mine', 'search': term}).json()
        assert [row['id'] for row in rows] == [old]
        assert rows[0]['search_match'] is True
        assert rows[0]['search_query'] == term.strip().lower()
        assert term.strip().lower() in rows[0]['search_snippet'].lower()
        assert len(rows[0]['search_snippet']) <= 240
        assert 'messages' not in rows[0]


def test_session_search_preserves_titles_and_legacy_results_without_private_context(workspace: tuple[FastAPI, TestClient]) -> None:
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    root = store.create_run('Original request', '', 'demo', [], chat_enabled=True, user_id='google:bob')['id']
    store.execute('UPDATE runs SET display_title=?,agent_label=? WHERE id=?', ('Renamed discussion', 'Custom assignment', root))
    store.update_run(root, summary='Saved public summary', pending_result=json.dumps({'private_protocol': 'Hidden protocol'}))
    for role in ('system', 'tool'):
        store.execute('INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,?,?,?,?)',
                      (root, role, role+' hidden material', 'completed', now()))
    legacy = store.create_run('Legacy task', '', 'demo', [], user_id='google:bob')['id']
    store.update_run(legacy, summary='Legacy answer text')
    side = store.create_run('Side topic', '', 'demo', [], chat_enabled=True, user_id='google:bob', side_chat_of=root)['id']
    store.enqueue_message(side, 'Separate side conversation', 'side-search', user_id='google:bob')
    for term in ('original request', 'renamed discussion', 'custom assignment'):
        assert mine(client, search=term) == {root}
    assert mine(client, search='legacy answer') == {legacy}
    assert mine(client, search='separate side conversation') == {side}
    assert mine(client, search='saved public summary') == {root}  # Public saved summary remains searchable.
    for term in ('hidden protocol', 'system hidden material', 'tool hidden material'):
        assert mine(client, search=term) == set()
    assert mine(client, search='   ') == mine(client)


def test_session_search_scopes_folder_matches_and_focused_results(workspace: tuple[FastAPI, TestClient]) -> None:
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    own = store.create_run('Personal task', '', 'demo', [], user_id='google:bob')['id']
    foreign = store.create_run('Foreign needle', '', 'demo', [], user_id='google:alice')['id']
    store.create_run('Another personal task', '', 'demo', [], user_id='google:bob')
    folder = client.post('/api/session-folders', json={'name': 'Research collection'}).json()['id']
    for run_id in (own, foreign):
        assert client.put('/api/runs/'+run_id+'/folder', json={'folder_id': folder}).status_code == 200
    assert mine(client, search='research collection') == {own}
    assert mine(client, search='foreign needle', focus=foreign) == set()
    assert mine(client, search='unmatched', focus=own) == set()
    assert client.put('/api/runs/'+foreign+'/pin', json={'pinned': True}).status_code == 200
    assert mine(client, search='foreign needle') == {foreign}
    assert mine(client, search='unmatched') == set()
    assert client.get('/api/runs', params={'search': 'foreign needle', 'scope': 'all'}).status_code == 403
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert {row['id'] for row in client.get('/api/runs', params={'search': 'foreign needle', 'scope': 'all'}).json()} == {foreign}
    assert client.get('/api/runs', params={'search': 'research collection', 'scope': 'all'}).json() == []


def test_session_search_tracks_queue_edits_archives_and_retained_deletion(workspace: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    def submit(run: dict[str, object]) -> None:
        pass
    monkeypatch.setattr(app.state.manager, 'submit', submit)
    run_id = store.create_run('Neutral discussion', '', 'demo', [], chat_enabled=True, user_id='google:bob')['id']
    url = '/api/runs/' + run_id
    message = client.post(url+'/messages', json={'content': 'First needle', 'client_id': 'editable-search'}).json()['id']
    assert mine(client, search='first needle') == {run_id}
    assert client.patch(f'{url}/messages/{message}', json={'action': 'edit', 'revision': 0, 'content': 'Second needle'}).status_code == 200
    assert mine(client, search='first needle') == set()
    assert mine(client, search='second needle') == {run_id}
    assert client.patch(f'{url}/messages/{message}', json={'action': 'delete', 'revision': 1}).status_code == 200
    assert mine(client, search='second needle') == set()
    store.enqueue_message(run_id, 'Archive conversation token', 'archive-search', user_id='google:bob')
    assert client.post(url+'/archive', json={'archived': True}).status_code == 200
    assert mine(client, search='archive conversation token') == set()
    assert mine(client, search='archive conversation token', archived=True) == {run_id}
    store.execute("UPDATE messages SET status='completed' WHERE run_id=? AND status!='deleted'", (run_id,))
    store.update_run(run_id, status='idle')
    assert client.delete(url).status_code == 200
    assert store.run(run_id)['deleted_at']
    for archived in (False, True):
        assert mine(client, search='archive conversation token', archived=archived, focus=run_id) == set()


def test_session_search_keeps_matching_agents_under_their_parent(workspace: tuple[FastAPI, TestClient]) -> None:
    app, client = workspace
    store = app.state.store
    parent, child, _ = seeded_group(app)
    _, sibling, _ = seeded_group(app)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, sibling))
    store.execute("UPDATE messages SET content='Agent transcript needle' WHERE run_id=?", (child,))
    rows = client.get('/api/runs', params={'search': 'TRANSCRIPT needle', 'scope': 'all'}).json()
    assert [row['id'] for row in rows] == [parent]
    assert rows[0]['search_match'] is False
    children = {row['id']: row for row in rows[0]['children']}
    assert children[child]['search_match'] is True and children[sibling]['search_match'] is False
    assert children[child]['search_query'] == 'transcript needle'
    assert 'transcript needle' in children[child]['search_snippet'].lower()
    assert 'prompt' not in children[child] and 'messages' not in children[child]
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), child))
    assert client.get('/api/runs', params={'search': 'transcript needle', 'scope': 'all'}).json() == []


def test_session_search_treats_sql_characters_literally_and_bounds_input(workspace: tuple[FastAPI, TestClient]) -> None:
    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'bob', 'bob@berri.ai')
    matching = store.create_run('Literal text', '', 'demo', [], chat_enabled=True, user_id='google:bob')['id']
    store.enqueue_message(matching, "100% user_name O'Reilly München <script>display text</script>", 'literal-search', user_id='google:bob')
    store.create_run('1000 userXname OReilly', '', 'demo', [], user_id='google:bob')
    for term in ('100%', 'user_name', "O'Reilly", 'MÜNCHEN', '<script>'):
        assert mine(client, search=term) == {matching}
    assert mine(client, search="' OR 1=1 --") == set()
    assert client.get('/api/runs', params={'search': 'x' * 200}).status_code == 200
    assert client.get('/api/runs', params={'search': 'x' * 201}).status_code == 422
    client.cookies.clear()
    assert client.get('/api/runs', params={'search': 'user_name'}).status_code == 401
