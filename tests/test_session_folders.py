from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import Response

from app.db import Store, now
from app.persistence import restore_checkpoint
from app.session_folders import SessionFolders
from app.session_lifecycle import SessionLifecycle
from app.temporal_runtime import TemporalRunManager
from test_agents import launch
from test_durable import Cloud, durable
from test_slack import signed, slack_app
from test_slack_chat import send, start
from test_spend import active, sign_in
from test_user_roles import sign_as, users_app


def folder(client, name='Today'):
    response = client.post('/api/session-folders', json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()


def run(app, title='Fix the sidebar'):
    return app.state.store.create_run(title, '', 'demo', [], chat_enabled=True, user_id='google:maya')


def assignment(client, run_id):
    return next(row for row in client.get('/api/runs').json() if row['id'] == run_id)['folder_id']


def test_create_rename_move_unfile_and_remove_preserve_session(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    item = run(app)
    original = app.state.store.run(item['id'])
    first, second = folder(client, ' Today '), folder(client, 'Research')
    assert first['name'] == 'Today'
    url = f"/api/runs/{item['id']}/folder"
    assert client.put(url, json={'folder_id': first['id']}).status_code == 200
    assert assignment(client, item['id']) == first['id']
    assert client.put(url, json={'folder_id': second['id']}).status_code == 200
    counts = {row['name']: row['session_count'] for row in client.get('/api/session-folders').json()['folders']}
    assert counts == {'Today': 0, 'Research': 1}
    renamed = client.patch('/api/session-folders/'+second['id'], json={'name': 'Investigations', 'revision': 1})
    assert renamed.status_code == 200 and renamed.json()['revision'] == 2
    assert client.patch('/api/session-folders/'+second['id'], json={'name': 'Stale', 'revision': 1}).status_code == 409
    assert client.put(url, json={'folder_id': None}).status_code == 200
    assert assignment(client, item['id']) is None
    assert client.put(url, json={'folder_id': second['id']}).status_code == 200
    assert client.request('DELETE', '/api/session-folders/'+second['id'], json={'revision': 1}).status_code == 409
    assert client.request('DELETE', '/api/session-folders/'+second['id'], json={'revision': 2}).status_code == 200
    assert assignment(client, item['id']) is None
    assert app.state.store.run(item['id']) == original
    assert app.state.store.messages(item['id'])[0]['content'] == 'Fix the sidebar'


def test_folders_are_personal_even_for_admins_and_do_not_change_session_sharing(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    item = run(app)
    mine = folder(client)
    client.put(f"/api/runs/{item['id']}/folder", json={'folder_id': mine['id']})
    sign_as(app, client, 'tin@berri.ai')
    assert client.get('/api/session-folders').json()['folders'] == []
    assert assignment(client, item['id']) is None
    assert client.get('/api/runs/'+item['id']).status_code == 200
    assert client.patch('/api/session-folders/'+mine['id'], json={'name': 'Changed', 'revision': 1}).status_code == 404
    assert client.request('DELETE', '/api/session-folders/'+mine['id'], json={'revision': 1}).status_code == 404
    assert client.put(f"/api/runs/{item['id']}/folder", json={'folder_id': mine['id']}).status_code == 404
    other = folder(client)  # The same name is allowed in another person's sidebar.
    assert client.put(f"/api/runs/{item['id']}/folder", json={'folder_id': other['id']}).status_code == 200
    sign_as(app, client, 'maya@berri.ai')
    assert assignment(client, item['id']) == mine['id']
    assert client.get('/api/session-folders').json()['folders'][0]['session_count'] == 1


def test_auth_csrf_validation_and_duplicate_names(users_app):
    app, client = users_app
    assert client.get('/api/session-folders').status_code == 401
    assert client.post('/api/session-folders', json={'name': 'Today'}).status_code == 401
    sign_as(app, client, 'maya@berri.ai')
    for name in ('', '  ', 'a'*81, 'two\nlines'):
        assert client.post('/api/session-folders', json={'name': name}).status_code == 422
    assert client.post('/api/session-folders', json={'name': 'Today', 'owner_id': 'someone-else'}).status_code == 422
    first = folder(client)
    assert client.post('/api/session-folders', json={'name': ' TODAY '}).status_code == 409
    second = folder(client, 'Other')
    assert client.patch('/api/session-folders/'+second['id'], json={'name': 'today', 'revision': 1}).status_code == 409
    item = run(app)
    actions = [('POST', '/api/session-folders', {'name': 'New'}),
               ('PATCH', '/api/session-folders/'+first['id'], {'name': 'Rename', 'revision': 1}),
               ('DELETE', '/api/session-folders/'+first['id'], {'revision': 1}),
               ('PUT', '/api/runs/'+item['id']+'/folder', {'folder_id': first['id']})]
    for method, url, body in actions:
        assert client.request(method, url, json=body, headers={'X-CSRF-Token': ''}).status_code == 403
        assert client.request(method, url, json=body, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.put('/api/runs/missing/folder', json={'folder_id': None}).status_code == 404
    assert client.put('/api/runs/'+item['id']+'/folder', json={'folder_id': 'bad'}).status_code == 422
    assert client.put('/api/runs/'+item['id']+'/folder', json={'folder_id': 'f'*32}).status_code == 404


def test_filed_older_sessions_and_child_agents_remain_in_sidebar(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    parent, child = run(app, 'Earlier parent'), run(app, 'Worker')
    app.state.store.execute('UPDATE runs SET parent_run_id=?,agent_label=? WHERE id=?',
                            (parent['id'], 'Investigate regression', child['id']))
    for index in range(101):
        run(app, f'Recent session {index}')
    assert parent['id'] not in {row['id'] for row in client.get('/api/runs').json()}
    saved = folder(client)
    assert client.put('/api/runs/'+parent['id']+'/folder', json={'folder_id': saved['id']}).status_code == 200
    assert client.put('/api/runs/'+child['id']+'/folder', json={'folder_id': saved['id']}).status_code == 422
    rows = client.get('/api/runs').json()
    found = next(row for row in rows if row['id'] == parent['id'])
    assert found['folder_id'] == saved['id'] and found['children'][0]['id'] == child['id']
    assert child['id'] not in {row['id'] for row in rows}
    assert client.get('/api/session-folders').json()['folders'][0]['session_count'] == 1


def test_folders_survive_restart_and_database_checkpoint(users_app, tmp_path):
    app, client = users_app
    identity = sign_as(app, client, 'maya@berri.ai')['user_id']
    checkpoints = app.state.session_folders.checkpoints
    checkpoints.settings = app.state.settings.model_copy(update={'checkpoint_dir': tmp_path/'checkpoint'})
    commits = []
    async def commit():
        commits.append(True)
    checkpoints.commit = commit
    item, saved = run(app), folder(client, 'Persistent')
    assert client.put('/api/runs/'+item['id']+'/folder', json={'folder_id': saved['id']}).status_code == 200
    assert client.put('/api/runs/'+item['id']+'/pin', json={'pinned': True}).status_code == 200
    assert len(commits) == 3
    restored_settings = checkpoints.settings.model_copy(update={'data_dir': tmp_path/'restored'})
    restore_checkpoint(restored_settings)
    reopened = Store(restored_settings.data_dir)
    folders = SessionFolders(reopened, app.state.security, SimpleNamespace())
    assert folders.listing(identity)[0]['name'] == 'Persistent'
    assert folders.memberships(identity) == {item['id']: saved['id']}
    assert folders.pins(identity) == {item['id']}
    assert reopened.sidebar_metadata(identity, [item['id']])[item['id']]['pinned'] is True


def test_archive_is_personal_keeps_running_session_and_folder(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    item, saved = run(app), folder(client)
    url = '/api/runs/' + item['id']
    assert client.put(url + '/folder', json={'folder_id': saved['id']}).status_code == 200
    original = app.state.store.run(item['id'])
    assert client.post(url + '/archive', json={'archived': True}).status_code == 200
    assert client.get('/api/runs', params={'focus': item['id']}).json() == []
    archived = client.get('/api/runs?archived=true').json()
    assert archived[0]['id'] == item['id'] and archived[0]['archived'] is True
    assert archived[0]['folder_id'] == saved['id']
    assert client.get(url).json()['archived'] is True
    assert client.get('/api/session-folders').json()['folders'][0]['session_count'] == 0
    assert app.state.store.run(item['id']) == original
    sign_as(app, client, 'tin@berri.ai')
    assert client.get('/api/runs?archived=true').json() == []
    assert client.get('/api/runs').json()[0]['archived'] is False
    assert client.get(url).json()['archived'] is False
    sign_as(app, client, 'maya@berri.ai')
    assert client.post(url + '/archive', json={'archived': False}).status_code == 200
    assert assignment(client, item['id']) == saved['id']
    assert client.get('/api/session-folders').json()['folders'][0]['session_count'] == 1
    assert app.state.store.run(item['id']) == original


def test_archive_filters_before_limit_and_keeps_agents_together(users_app):
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    parent, child = run(app, 'Earlier parent'), run(app, 'Worker')
    store = app.state.store
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent['id'], child['id']))
    saved = folder(client)
    client.put('/api/runs/' + parent['id'] + '/folder', json={'folder_id': saved['id']})
    for index in range(101):
        item = run(app, f'Archived {index}')
        app.state.session_lifecycle.archive(item['id'], actor, True)
    rows = client.get('/api/runs').json()
    assert [row['id'] for row in rows] == [parent['id']]
    assert rows[0]['children'][0]['id'] == child['id']
    assert rows[0]['children'][0]['can_delete'] is False
    assert client.post('/api/runs/' + child['id'] + '/archive', json={'archived': True}).status_code == 422
    assert client.post('/api/runs/' + parent['id'] + '/archive', json={'archived': True}).status_code == 200
    assert client.get('/api/runs', params={'focus': child['id']}).json() == []
    rows = client.get('/api/runs?archived=true').json()
    assert len(rows) == 101  # Latest 100 plus the older filed session.
    assert next(row for row in rows if row['id'] == parent['id'])['children'][0]['archived'] is True
    assert client.get('/api/runs/' + child['id']).json()['archived'] is True
    assert client.post('/api/runs/' + parent['id'] + '/archive', json={'archived': 'false'}).status_code == 422
    assert client.post('/api/runs/' + parent['id'] + '/archive', json={'archived': False}, headers={'X-CSRF-Token': ''}).status_code == 403
    client.cookies.clear()
    assert client.post('/api/runs/' + parent['id'] + '/archive', json={'archived': False}).status_code == 401


def test_archive_and_deletion_survive_legacy_upgrade_and_checkpoint(users_app, tmp_path):
    from app.session_lifecycle import SessionLifecycle

    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    item, removed = run(app), run(app, 'Delete after completion')
    store = app.state.store
    # Exercise the actual idempotent upgrade with a pre-field database.
    # This later trigger could not exist before its deleted_at dependency.
    store.execute('DROP TRIGGER revoke_github_write_access')
    store.execute('ALTER TABLE runs DROP COLUMN deleted_at')
    store.execute('DROP TABLE session_archives')
    reopened = Store(app.state.settings.data_dir)
    assert reopened.run(item['id'])['deleted_at'] == ''
    # App startup initializes GitHub access after the Store schema upgrade.
    app.state.connectors.github.init_write_access()
    checkpoints = app.state.session_folders.checkpoints
    checkpoints.settings = app.state.settings.model_copy(update={'checkpoint_dir': tmp_path/'checkpoint'})
    async def commit():
        pass
    checkpoints.commit = commit
    assert client.post('/api/runs/' + item['id'] + '/archive', json={'archived': True}).status_code == 200
    store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (removed['id'],))
    store.update_run(removed['id'], status='idle')
    assert client.delete('/api/runs/' + removed['id']).status_code == 200
    restored_settings = checkpoints.settings.model_copy(update={'data_dir': tmp_path/'restored'})
    restore_checkpoint(restored_settings)
    restored = Store(restored_settings.data_dir)
    lifecycle = SessionLifecycle(restored, app.state.security, app.state.manager, checkpoints)
    assert lifecycle.archives(actor) == {item['id']}
    assert restored.run(removed['id'])['deleted_at']
    assert restored.sidebar_run_ids(actor, archive_owner=actor) == []
    assert restored.sidebar_run_ids(actor, archive_owner=actor, archived=True) == [item['id']]


def test_viewer_can_find_and_restore_archived_shared_link(users_app):
    app, client = users_app
    item = run(app)
    sign_as(app, client, 'viewer@berri.ai')
    url = '/api/runs/' + item['id']
    assert client.get('/api/runs').json() == []
    assert client.get(url).status_code == 200
    assert client.post(url + '/archive', json={'archived': True}).status_code == 200
    assert [row['id'] for row in client.get('/api/runs?archived=true').json()] == [item['id']]
    assert client.put(url + '/title', json={'title': 'Archived shared session', 'expected_title': ''}).status_code == 200
    assert client.get(url).json()['display_title'] == 'Archived shared session'
    assert client.post(url + '/archive', json={'archived': False}).status_code == 200
    assert client.get('/api/runs?archived=true').json() == []
    assert client.get('/api/runs').json() == []  # Viewing/archiving never adds participation.


def test_pins_retain_shared_links_personally_without_creating_participation(users_app):
    app, client = users_app
    item = run(app)
    url = '/api/runs/' + item['id']
    assert client.put(url + '/pin', json={'pinned': True}).status_code == 401
    sign_as(app, client, 'viewer@berri.ai')
    original = app.state.store.run(item['id'])
    assert client.get('/api/runs', params={'focus': item['id']}).json() == []
    for invalid in ('true', 1, None):
        assert client.put(url + '/pin', json={'pinned': invalid}).status_code == 422
    assert client.put(url + '/pin', json={'pinned': True, 'owner_id': 'google:maya'}).status_code == 422
    assert client.put(url + '/pin', json={'pinned': True}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.put(url + '/pin', json={'pinned': True}, headers={'Origin': 'https://other.example'}).status_code == 403
    for _ in range(2):
        assert client.put(url + '/pin', json={'pinned': True}).json() == {'id': item['id'], 'pinned': True}
    rows = client.get('/api/runs').json()
    assert [row['id'] for row in rows] == [item['id']]
    assert rows[0]['pinned'] is True and rows[0]['participated'] is False
    assert client.get(url).json()['pinned'] is True
    assert app.state.store.run(item['id']) == original
    sign_as(app, client, 'other@berri.ai')
    assert client.get('/api/runs').json() == []
    assert client.get(url).json()['pinned'] is False
    assert client.put(url + '/pin', json={'pinned': False}).status_code == 200
    sign_as(app, client, 'viewer@berri.ai')
    assert client.get(url).json()['pinned'] is True
    assert client.put(url + '/pin', json={'pinned': False}).status_code == 200
    assert client.get('/api/runs', params={'focus': item['id']}).json() == []


def test_pins_survive_recent_limit_archive_restore_and_hide_deleted_sessions(users_app):
    app, client = users_app
    store = app.state.store
    parent, child, removed = run(app, 'Pinned parent'), run(app, 'Child'), run(app, 'Deleted pin')
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent['id'], child['id']))
    for index in range(101):
        run(app, f'Newer {index}')
    sign_as(app, client, 'maya@berri.ai')
    assert parent['id'] not in {row['id'] for row in client.get('/api/runs').json()}
    url = '/api/runs/' + parent['id']
    assert client.put(url + '/pin', json={'pinned': True}).status_code == 200
    rows = client.get('/api/runs').json()
    assert len(rows) == 101 and next(row for row in rows if row['id'] == parent['id'])['pinned'] is True
    assert client.put('/api/runs/' + child['id'] + '/pin', json={'pinned': True}).status_code == 422
    assert client.put('/api/runs/missing/pin', json={'pinned': True}).status_code == 404
    assert client.post(url + '/archive', json={'archived': True}).status_code == 200
    assert parent['id'] not in {row['id'] for row in client.get('/api/runs').json()}
    assert client.get('/api/runs?archived=true').json()[0]['pinned'] is True
    assert client.post(url + '/archive', json={'archived': False}).status_code == 200
    assert next(row for row in client.get('/api/runs').json() if row['id'] == parent['id'])['pinned'] is True
    removed_url = '/api/runs/' + removed['id']
    assert client.put(removed_url + '/pin', json={'pinned': True}).status_code == 200
    store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (removed['id'],))
    store.update_run(removed['id'], status='idle')
    assert client.delete(removed_url).status_code == 200
    assert removed['id'] not in {row['id'] for row in client.get('/api/runs').json()}
    assert client.put(removed_url + '/pin', json={'pinned': True}).status_code == 404


def test_shared_link_pins_do_not_displace_recent_personal_sessions(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    personal = {run(app, f'Personal {index}')['id'] for index in range(100)}
    shared = app.state.store.create_run('Newer teammate session', '', 'demo', [], user_id='google:teammate')['id']
    assert {row['id'] for row in client.get('/api/runs').json()} == personal
    url = '/api/runs/' + shared + '/pin'
    assert client.put(url, json={'pinned': True}).status_code == 200
    rows = {row['id']: row for row in client.get('/api/runs').json()}
    assert set(rows) == personal | {shared}
    assert rows[shared]['pinned'] is True and rows[shared]['participated'] is False
    assert client.put(url, json={'pinned': False}).status_code == 200
    assert {row['id'] for row in client.get('/api/runs').json()} == personal


def test_pin_schema_upgrade_is_idempotent_and_preserves_legacy_sessions(users_app):
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    item = run(app)
    app.state.store.execute('DROP TABLE session_pins')
    for _ in range(2):
        reopened = Store(app.state.settings.data_dir)
        assert reopened.run(item['id'])['prompt'] == item['prompt']
        assert reopened.sidebar_metadata(actor, [item['id']])[item['id']]['pinned'] is False
    assert client.put('/api/runs/' + item['id'] + '/pin', json={'pinned': True}).status_code == 200
    reopened = Store(app.state.settings.data_dir)
    assert reopened.sidebar_metadata(actor, [item['id']])[item['id']]['pinned'] is True


def search_sessions(client: TestClient, run_id: str, **arguments: object) -> Response:
    return client.post('/broker/' + run_id + '/tools/call', headers={'Authorization': 'Bearer capability'},
                       json={'name': 'sessions_search', 'arguments': arguments})


def test_session_search_finds_personal_archives_and_participation_without_restoring(
    users_app: tuple[FastAPI, TestClient],
) -> None:
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    store, lifecycle = app.state.store, app.state.session_lifecycle
    archived, visible = run(app, 'Earlier work'), run(app, 'Copper active')
    store.execute('UPDATE runs SET display_title=? WHERE id=?', ('Copper archived', archived['id']))
    lifecycle.archive(archived['id'], actor, True)
    shared = store.create_run('Copper shared link', '', 'demo', [], user_id='google:other')
    pinned = store.create_run('Copper pinned shared link', '', 'demo', [], user_id='google:other')
    assert client.put('/api/runs/' + pinned['id'] + '/pin', json={'pinned': True}).status_code == 200
    participated = store.create_run('Copper participated', '', 'demo', [], chat_enabled=True, user_id='google:other')
    store.enqueue_message(participated['id'], 'Joining this work', 'joined', user_id=actor)
    unrelated = store.create_run('Copper unrelated', '', 'demo', [], user_id='google:other')
    lifecycle.archive(shared['id'], actor, True)
    sign_as(app, client, 'other@berri.ai')
    lifecycle.archive(unrelated['id'], 'google:other', True)
    assert client.put('/api/runs/' + unrelated['id'] + '/pin', json={'pinned': True}).status_code == 200
    sign_as(app, client, 'maya@berri.ai')
    store.execute('INSERT INTO users(id,kind,name,linked_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                  ('slack:T12345678:U12345678', 'slack', 'Maya', actor, now(), now()))
    linked = store.create_run('Copper Slack work', '', 'demo', [], user_id='slack:T12345678:U12345678')
    caller = active(app, actor)
    store.execute('UPDATE runs SET prompt=?,owner_id=? WHERE id=?', ('Copper current search', 'google:other', caller['id']))
    response = search_sessions(client, caller['id'], query='  COPPER  ')
    assert response.status_code == 200, response.text
    result = response.json()
    rows = {row['id']: row for row in result['sessions']}
    assert set(rows) == {item['id'] for item in (archived, visible, shared, pinned, participated, linked)}
    assert result['has_more'] is False
    assert rows[archived['id']]['archived'] is True and rows[visible['id']]['archived'] is False
    assert set(rows[archived['id']]) == {'id', 'title', 'preview', 'status', 'archived', 'chat_enabled', 'updated_at', 'url'}
    assert rows[archived['id']]['url'] == app.state.settings.public_url + '/#run=' + archived['id']
    assert client.get('/api/runs/' + archived['id']).status_code == 200
    assert lifecycle.archives(actor) == {archived['id'], shared['id']}
    assert archived['id'] not in {row['id'] for row in client.get('/api/runs?scope=mine').json()}
    reopened = Store(app.state.settings.data_dir)
    assert reopened.rows('SELECT run_id FROM session_archives WHERE owner_id=? ORDER BY run_id', (actor,)) == [
        {'run_id': run_id} for run_id in sorted((archived['id'], shared['id']))]
    assert set(reopened.sidebar_run_ids(actor, archive_owner=actor, archived=None, pin_owner=actor,
                                        search=['copper'], exclude_id=caller['id'])) == set(rows)
    assert client.put('/api/runs/' + pinned['id'] + '/pin', json={'pinned': False}).status_code == 200
    assert {row['id'] for row in search_sessions(client, caller['id'], query='Copper').json()['sessions']} == set(rows) - {pinned['id']}


def test_session_search_matches_family_history_literal_terms_and_legacy_before_limit(
    users_app: tuple[FastAPI, TestClient],
) -> None:
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    store = app.state.store
    parent, child = run(app, 'Copper recovery'), run(app, 'Worker')
    store.execute('UPDATE runs SET parent_run_id=?,agent_label=? WHERE id=?', (parent['id'], 'Benchmark worker', child['id']))
    message = store.claim_message(child['id'])
    store.finish_message(child['id'], message['id'], 'Measured a 50% improvement for sample_name')
    store.enqueue_message(child['id'], 'Check regression', 'regression-followup', user_id=actor)
    app.state.session_lifecycle.archive(parent['id'], actor, True)
    legacy = store.create_run('Older task', '', 'demo', [], user_id='google:other')
    store.update_run(legacy['id'], summary='Copper benchmark regression 50% sample_name')
    assert client.put('/api/runs/' + legacy['id'] + '/pin', json={'pinned': True}).status_code == 200
    store.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (parent['id'],))
    store.execute("UPDATE runs SET updated_at='2000-01-02T00:00:00+00:00' WHERE id=?", (legacy['id'],))
    deleted = run(app, 'Copper benchmark regression 50% sample_name')
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), deleted['id']))
    removed = run(app, 'Discarded draft')
    store.enqueue_message(removed['id'], 'Copper benchmark regression 50% sample_name', 'removed', user_id=actor)
    store.execute("UPDATE messages SET status='deleted' WHERE run_id=?", (removed['id'],))
    store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'tool',?,'completed',?)",
                  (removed['id'], 'Copper benchmark regression 50% sample_name', now()))
    for index in range(101):
        run(app, f'Copper benchmark regression 500 sampleXname unrelated {index}')
    caller = active(app, actor)
    query = 'copper benchmark regression 50% sample_name'
    response = search_sessions(client, caller['id'], query=query, limit=1)
    assert response.status_code == 200, response.text
    assert [row['id'] for row in response.json()['sessions']] == [legacy['id']] and response.json()['has_more'] is True
    result = search_sessions(client, caller['id'], query=query, limit=20).json()
    rows = {row['id']: row for row in result['sessions']}
    assert list(rows) == [legacy['id'], parent['id']] and result['has_more'] is False
    assert rows[parent['id']]['chat_enabled'] and not rows[legacy['id']]['chat_enabled']
    assert rows[parent['id']]['archived'] and rows[parent['id']]['title']
    assert search_sessions(client, caller['id'], query=query + ' absent').json()['sessions'] == []


@pytest.mark.parametrize('arguments', [{}, {'query': '  '}, {'query': 'x' * 201}, {'query': 'x', 'limit': 0},
                                     {'query': 'x', 'limit': 21}, {'query': 'x', 'scope': 'all'},
                                     {'query': 'x', 'owner_id': 'google:other'}])
def test_session_search_rejects_invalid_or_caller_selected_scope(
    users_app: tuple[FastAPI, TestClient], arguments: dict[str, object],
) -> None:
    app, client = users_app
    caller = active(app, sign_as(app, client, 'maya@berri.ai')['user_id'])
    assert search_sessions(client, caller['id'], **arguments).status_code == 422


@pytest.mark.parametrize('change', ['child', 'automated', 'anonymous', 'no_turn', 'legacy', 'inactive', 'deleted', 'token'])
def test_session_search_is_advertised_and_callable_only_for_live_direct_chat(
    users_app: tuple[FastAPI, TestClient], change: str,
) -> None:
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    caller = active(app, actor)
    store, url = app.state.store, '/broker/' + caller['id'] + '/tools'
    headers = {'Authorization': 'Bearer capability'}
    tool = next(item for item in client.get(url, headers=headers).json() if item['name'] == 'sessions_search')
    assert tool['annotations']['readOnlyHint'] is True
    if change == 'automated':
        store.execute("INSERT INTO automations(id,owner_id,definition,created_at,updated_at) VALUES('search-auto',?,'{}',?,?)",
                      (actor, now(), now()))
        store.execute("INSERT INTO automation_runs VALUES('search-tick','search-auto',1,?,'started','',?)", (caller['id'], now()))
    else:
        field, value = {'child': ('parent_run_id', 'parent'), 'anonymous': ('active_user_id', ''),
                        'no_turn': ('active_message_id', None), 'legacy': ('chat_enabled', 0), 'inactive': ('status', 'idle'),
                        'deleted': ('deleted_at', now()), 'token': ('token_hash', '')}[change]
        store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, caller['id']))
    listing = client.get(url, headers=headers)
    assert listing.status_code == 401 or 'sessions_search' not in {item['name'] for item in listing.json()}
    assert search_sessions(client, caller['id'], query='Copper').status_code in {401, 403}
    assert client.get(url, headers={'Authorization': 'Bearer wrong'}).status_code == 401


@pytest.mark.parametrize('field,value', [('active_user_id', 'google:other'), ('active_message_id', 999999)])
def test_session_search_rechecks_requester_after_reading_broker_body(
    users_app: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch, field: str, value: str | int,
) -> None:
    app, client = users_app
    caller = active(app, sign_as(app, client, 'maya@berri.ai')['user_id'])
    original = Request.body
    async def changed_body(request: Request) -> bytes:
        body = await original(request)
        if request.url.path == '/broker/' + caller['id'] + '/tools/call':
            app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, caller['id']))
        return body
    monkeypatch.setattr(Request, 'body', changed_body)
    assert search_sessions(client, caller['id'], query='Copper').status_code == 409


def test_resuming_archived_session_restores_only_sender_after_new_accepted_message(
    users_app: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    other = sign_as(app, client, 'other@berri.ai')['user_id']
    sign_as(app, client, 'maya@berri.ai')
    store, lifecycle = app.state.store, app.state.session_lifecycle
    item, saved = run(app), folder(client)
    url = '/api/runs/' + item['id']
    assert client.put(url + '/folder', json={'folder_id': saved['id']}).status_code == 200
    assert client.put(url + '/pin', json={'pinned': True}).status_code == 200
    for owner in (actor, other):
        lifecycle.archive(item['id'], owner, True)
    def submit(_: object) -> None:
        pass
    monkeypatch.setattr(app.state.manager, 'submit', submit)
    lookup = client.post(url + '/messages', json={'content': '/session-id', 'client_id': 'archived-id-lookup'})
    assert lookup.status_code == 202 and lookup.json()['status'] == 'completed'
    assert lifecycle.archives(actor) == lifecycle.archives(other) == {item['id']}
    body = {'content': 'Continue the saved task', 'client_id': 'resume-archived'}
    store.update_run(item['id'], status='stopping')
    assert client.post(url + '/messages', json=body).status_code == 409
    assert lifecycle.archives(actor) == {item['id']}
    store.update_run(item['id'], status='idle')
    assert client.get(url).json()['archived'] is True
    assert client.post(url + '/messages', json=body).status_code == 202
    assert lifecycle.archives(actor) == set() and lifecycle.archives(other) == {item['id']}
    restored = next(row for row in client.get('/api/runs').json() if row['id'] == item['id'])
    assert restored['pinned'] is True and restored['folder_id'] == saved['id']
    lifecycle.archive(item['id'], actor, True)
    assert client.post(url + '/messages', json=body).json()['created'] is False
    assert lifecycle.archives(actor) == {item['id']}
    child = run(app, 'Worker continuation')
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (item['id'], child['id']))
    store.enqueue_message(child['id'], 'Human continuation', 'human', user_id=actor)
    assert lifecycle.archives(actor) == set() and lifecycle.archives(other) == {item['id']}


async def test_agent_retry_keeps_parent_archived_for_the_requester(
    durable: tuple[TemporalRunManager, Cloud, str],
) -> None:
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    store = manager.store
    actor = store.identity({'method': 'google', 'identity': {'sub': 'tin', 'email': 'tin@berri.ai'}})
    lifecycle = SessionLifecycle(store, None, manager, None)
    lifecycle.archive(root, actor, True)
    child = coordinator.children(group['group_id'])[0]['id']
    store.execute("UPDATE messages SET status='failed' WHERE run_id=?", (child,))
    store.update_run(child, status='failed')
    await coordinator.call(root, 'agents_retry', {'group_id': group['group_id'], 'child_ids': [child],
        'request_key': 'archive-recovery', 'instructions': 'Retry the failed case from saved state.'})
    assert store.messages(child)[-1]['status'] == 'queued'
    assert lifecycle.archives(actor) == {root}


def test_slack_session_search_follows_current_shared_view_link(
    users_app: tuple[FastAPI, TestClient],
) -> None:
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    other = sign_as(app, client, 'other@berri.ai')['user_id']
    store, lifecycle = app.state.store, app.state.session_lifecycle
    slack, sibling = 'slack:T12345678:U12345678', 'slack:T12345678:U87654321'
    for identity in (slack, sibling):
        store.execute('INSERT INTO users(id,kind,name,linked_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                      (identity, 'slack', 'Slack user', actor, now(), now()))
    web = run(app, 'Copper web work')
    shared = store.create_run('Copper shared link', '', 'demo', [], user_id=other)
    unrelated = store.create_run('Copper other work', '', 'demo', [], user_id=other)
    own = store.create_run('Copper Slack work', '', 'demo', [], user_id=slack)
    linked = store.create_run('Copper second Slack account', '', 'demo', [], user_id=sibling)
    for item in (web, shared):
        lifecycle.archive(item['id'], actor, True)
    lifecycle.archive(unrelated['id'], other, True)
    caller = active(app, slack)
    # These are shared-workspace view links, independent of private credential access.
    assert not app.state.credentials.same_requester(actor, slack)
    for owner, expected, archived in [
        (actor, (web, shared, own, linked), (web, shared)),
        (None, (own,), ()),
        (sibling, (own,), ()),  # A malformed Slack-to-Slack link must not recurse.
        (other, (shared, unrelated, own), (unrelated,)),
    ]:
        store.execute('UPDATE users SET linked_user_id=? WHERE id=?', (owner, slack))
        response = search_sessions(client, caller['id'], query='Copper')
        assert response.status_code == 200, response.text
        rows = response.json()['sessions']
        assert {row['id'] for row in rows} == {item['id'] for item in expected}
        assert {row['id'] for row in rows if row['archived']} == {item['id'] for item in archived}
        assert store.run(caller['id'])['active_user_id'] == slack
    assert lifecycle.archives(actor) == {web['id'], shared['id']}
    assert lifecycle.archives(other) == {unrelated['id']}


def test_slack_resume_restores_current_view_owner_without_rewriting_sender_or_replaying(
    slack_app: tuple[FastAPI, TestClient, list[dict[str, object]], list[dict[str, object]]],
) -> None:
    app, client, root = start(slack_app)
    actor = sign_in(app, client, 'maya', 'maya@berri.ai')
    other = sign_in(app, client, 'other', 'other@berri.ai')
    store, lifecycle = app.state.store, app.state.session_lifecycle
    slack = store.run(root)['owner_id']
    for owner in (actor, other):
        lifecycle.archive(root, owner, True)
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', (actor, slack))
    payload = send(client, 1, 'Continue the archived work')
    assert lifecycle.archives(actor) == set() and lifecycle.archives(other) == {root}
    assert store.messages(root)[-1]['user_id'] == slack
    sign_in(app, client, 'maya', 'maya@berri.ai')
    assert root in {row['id'] for row in client.get('/api/runs?scope=mine').json()}
    lifecycle.archive(root, actor, True)
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert lifecycle.archives(actor) == {root} and len(store.messages(root)) == 2
    store.execute('UPDATE users SET linked_user_id=NULL WHERE id=?', (slack,))
    send(client, 2, 'Continue while unlinked')
    assert lifecycle.archives(actor) == lifecycle.archives(other) == {root}
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', (other, slack))
    send(client, 3, 'Continue after the link changes')
    assert lifecycle.archives(actor) == {root} and lifecycle.archives(other) == set()
    assert all(message['user_id'] == slack for message in store.messages(root))
