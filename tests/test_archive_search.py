"""Search can include archived conversations without changing ordinary inventory."""
from test_user_roles import users_app, sign_as


def test_archive_search_retains_visibility_and_lifecycle(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    store = app.state.store
    own = store.create_run('Maple release checklist', '', 'demo', [], chat_enabled=True, user_id='google:maya')
    message = store.claim_message(own['id'])
    store.finish_message(own['id'], message['id'], 'Saved checkpoint MAPLE-GATE-47')
    store.update_run(own['id'], status='idle')
    other = store.create_run('Maple private conversation', '', 'demo', [], chat_enabled=True, user_id='google:other')
    app.state.session_lifecycle.archive(own['id'], 'google:maya', True)
    for query in ('Maple', 'MAPLE-GATE-47'):
        assert client.get('/api/runs', params={'scope':'mine','search':query}).json() == []
        response = client.get('/api/runs', params={'scope':'mine','search':query,'include_archived':'true'})
        assert response.status_code == 200
        assert [r['id'] for r in response.json()] == [own['id']]
        assert response.json()[0]['archived'] is True
    assert client.get('/api/runs/' + own['id']).json()['archived'] is True
    assert client.get('/api/runs', params={'scope':'all','include_archived':'true'}).status_code == 403
    assert client.get('/api/runs?scope=mine').json() == []
