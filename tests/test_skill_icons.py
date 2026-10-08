"""Appearance remains metadata, under the existing skill write permissions."""
import pytest
from app.skills import Skills
from test_skills import create, edit
from test_spend import sign_in
from test_workspace import workspace


def test_icon_defaults_persistence_legacy_edits_and_reset(workspace):
    app, client = workspace
    sign_in(app, client)
    skill = create(client).json()['id']
    assert client.get('/api/skills/'+skill).json()['icon'] == 'auto'
    assert edit(client, skill, icon='video').status_code == 200
    assert client.get('/api/skills').json()['skills'][0]['icon'] == 'video'
    assert edit(client, skill, description='Legacy edit without an icon').status_code == 200
    assert client.get('/api/skills/'+skill).json()['icon'] == 'video'
    Skills(app.state.store, app.state.security, app.state.credentials.same_requester)
    assert client.get('/api/skills/'+skill).json()['icon'] == 'video'
    assert edit(client, skill, icon='auto').status_code == 200
    assert client.get('/api/skills/'+skill).json()['icon'] == 'auto'


@pytest.mark.parametrize('icon', ['https://example.test/icon.svg', '<svg onload=alert(1)>', 'bogus', None])
def test_invalid_icon_is_rejected(workspace, icon):
    app, client = workspace
    sign_in(app, client)
    skill = create(client).json()['id']
    assert edit(client, skill, icon=icon).status_code == 422
    assert client.get('/api/skills/'+skill).json()['revision'] == 1


def test_icon_scope_permissions_and_revision(workspace):
    app, client = workspace
    sign_in(app, client)
    personal = create(client).json()['id']
    org = create(client, 'organization', client_id='org-icon-test').json()['id']
    assert edit(client, org, icon='chart').status_code == 200
    assert edit(client, org, icon='video', revision=1).status_code == 409
    sign_in(app, client, 'ishaan', 'ishaan@berri.ai')
    assert client.get('/api/skills/'+personal).status_code == 404
    assert edit(client, org, icon='video').status_code == 403
    assert client.get('/api/skills/'+org).json()['icon'] == 'chart'


def test_create_retries_include_icon_and_legacy_schema_migrates(workspace):
    app, client = workspace
    sign_in(app, client)
    # Simulate the pre-icon schema, then initialize twice (existing DB and restart).
    app.state.store.execute('ALTER TABLE skills DROP COLUMN icon')
    for _ in range(2):
        Skills(app.state.store, app.state.security, app.state.credentials.same_requester)
    body = dict(name='video', description='Make a video', instructions='Make the video.',
                scope='personal', icon='video', client_id='create-icon-test')
    response = client.post('/api/skills', json=body)
    assert response.status_code == 201
    assert client.post('/api/skills', json=body).json() == response.json()
    assert client.post('/api/skills', json={**body, 'icon':'code'}).status_code == 409
    assert client.get('/api/skills/'+response.json()['id']).json()['icon'] == 'video'
