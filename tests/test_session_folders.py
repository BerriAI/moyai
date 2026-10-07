from types import SimpleNamespace

import pytest

from app.db import Store
from app.persistence import restore_checkpoint
from app.session_folders import SessionFolders
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
    store.execute('ALTER TABLE runs DROP COLUMN deleted_at')
    store.execute('DROP TABLE session_archives')
    reopened = Store(app.state.settings.data_dir)
    assert reopened.run(item['id'])['deleted_at'] == ''
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
