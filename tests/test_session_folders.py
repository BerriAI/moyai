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
    assert len(commits) == 2
    restored_settings = checkpoints.settings.model_copy(update={'data_dir': tmp_path/'restored'})
    restore_checkpoint(restored_settings)
    reopened = Store(restored_settings.data_dir)
    folders = SessionFolders(reopened, app.state.security, SimpleNamespace())
    assert folders.listing(identity)[0]['name'] == 'Persistent'
    assert folders.memberships(identity) == {item['id']: saved['id']}
