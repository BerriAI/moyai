from test_attachments import upload, start, workspace, sign_in, storage_mode


def test_selection_order_survives_upload_completion_and_reopen(workspace, storage_mode, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    # The second selected upload finishes first.
    second = upload(client, 'second.csv', b'second').json()
    first = upload(client, 'first.csv', b'first').json()
    response = start(app, client, monkeypatch, [first, second])
    assert response.status_code == 201
    run_id = response.json()['id']
    run = client.get('/api/runs/' + run_id).json()
    message = next(m for m in run['messages'] if m['role'] == 'user')
    expected = [first['id'], second['id']]
    assert [f['id'] for f in message['attachments']] == expected
    assert [f['id'] for f in app.state.store.attachments.for_run(run_id, message['id'])] == expected
