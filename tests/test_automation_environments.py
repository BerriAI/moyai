import asyncio
from unittest.mock import AsyncMock

import pytest

from app.automation_events import AutomationEvents
from app.environments import Recipe, SaveRecipe
from app.temporal_runtime import TemporalRunManager
from test_automations import create
from test_automation_events import SECRET, signed, complete
from test_workspace import workspace  # noqa: F401


@pytest.fixture
def blocked(workspace, monkeypatch):
    app, client = workspace
    settings, env = app.state.settings, app.state.environments
    monkeypatch.setattr(app.state.connectors.github, 'public_repository', AsyncMock(return_value={'id': 202, 'full_name': 'BerriAI/moyai', 'private': False}))
    settings.modal_token_id = settings.modal_token_secret = settings.litellm_api_key = 'test-placeholder'
    settings.litellm_api_base = 'https://unused.invalid/v1'
    monkeypatch.setattr(env, 'advance', AsyncMock())
    env.save_recipe('e' * 32, SaveRecipe(recipe=Recipe(name='Moyai development', repository='BerriAI/moyai', verify='true')), 'admin')
    env.store.execute('UPDATE environments SET activate_on_ready=1')
    build = env.enqueue('e' * 32, 1, 'admin')
    env.update(build['id'], phase='failed')
    a = create(client, mode='modal', repo_url='https://github.com/BerriAI/moyai',
               event={'provider': 'webhook', 'event': 'complaint.received'})
    response = client.post(f"/api/automations/{a['id']}/webhook", json={'revision': 1, 'secret': SECRET})
    assert response.status_code == 200, response.text
    TemporalRunManager(app.state.store, settings)
    settings.temporal_enabled = True
    response = client.post(f"/api/automations/{a['id']}/state", json={'revision': 2, 'paused': False})
    assert response.status_code == 200, response.text
    return app, client, response.json()


def deliver(client, automation, number=1):
    return client.post('/hooks/automations/' + automation['id'],
                       **signed('webhook', {'event': 'complaint.received', 'body': f'Complaint {number}'}, delivery=f'event-{number}'))


def test_failed_environment_holds_events_across_restart_and_resumes_once_ready(blocked):
    app, client, a = blocked
    service, store, env = app.state.automations, app.state.store, app.state.environments
    for number in range(3):
        assert deliver(client, a, number).json()['status'] == 'accepted'
    for _ in range(2):
        asyncio.run(service.events.dispatch())
    assert not store.rows('SELECT id FROM runs')
    assert not store.rows('SELECT occurrence FROM automation_runs')
    pending = store.rows('SELECT * FROM automation_events')
    assert all(e['status'] == 'pending' and 'Complaint' in e['context'] for e in pending)
    assert 'Project setup failed' in pending[0]['detail']
    public = client.get('/api/automations').json()['automations'][0]
    assert 'Project setup failed' in public['environment_blocker']

    service.events = AutomationEvents(service)  # Reload the persisted inbox.
    build = env.enqueue('e' * 32, 1, 'admin')
    asyncio.run(service.events.dispatch())
    assert not store.rows('SELECT id FROM runs')
    assert 'Waiting for a successful build' in client.get('/api/automations').json()['automations'][0]['environment_blocker']
    env.update(build['id'], phase='ready', snapshot_id='im-ready', commit_sha='a' * 40)
    store.execute('UPDATE environments SET enabled=1,activate_on_ready=0,active_build=?', (build['id'],))
    assert client.get('/api/automations').json()['automations'][0]['environment_blocker'] == ''
    for number in range(3):
        asyncio.run(service.events.dispatch())
        runs = store.rows('SELECT * FROM runs ORDER BY rowid')
        assert len(runs) == number + 1
        assert f'Complaint {number}' in runs[-1]['prompt']
        assert runs[-1]['owner_id'] == runs[-1]['active_user_id'] == a['owner_id']
        complete(app, runs[-1]['id'])
    assert deliver(client, a, 0).json()['status'] == 'duplicate'
    asyncio.run(service.events.dispatch())
    assert len(store.rows('SELECT id FROM runs')) == 3
    assert all(e['status'] == 'started' and e['context'] == '{}' for e in store.rows('SELECT * FROM automation_events'))


@pytest.mark.parametrize('change', ['expired', 'paused', 'edited', 'access_removed'])
def test_environment_wait_still_respects_event_expiry_and_authorization(blocked, change):
    app, client, a = blocked
    assert deliver(client, a).json()['status'] == 'accepted'
    asyncio.run(app.state.automations.events.dispatch())
    store = app.state.store
    if change == 'expired':
        store.execute("UPDATE automation_events SET expires_at='2000-01-01T00:00:00+00:00'")
    elif change == 'paused':
        store.execute('UPDATE automations SET paused=1')
    elif change == 'edited':
        store.execute('UPDATE automations SET revision=revision+1')
    else:
        app.state.settings.modal_token_secret = ''
    asyncio.run(app.state.automations.events.dispatch())
    event = store.rows('SELECT * FROM automation_events')[0]
    assert event['status'] == ('blocked' if change == 'access_removed' else 'skipped')
    assert event['context'] == '{}'
    assert not store.rows('SELECT id FROM runs')


@pytest.mark.parametrize('manual', [True, False])
def test_manual_and_scheduled_runs_report_blocker_without_creating_session(blocked, manual):
    app, client, a = blocked
    if manual:
        response = client.post(f"/api/automations/{a['id']}/run", json={'revision': a['revision'], 'client_id': 'manual-check'})
        assert response.status_code == 202
        result = response.json()
    else:
        schedule = create(client, mode='modal', repo_url='https://github.com/BerriAI/moyai')
        app.state.store.execute('UPDATE automations SET paused=0 WHERE id=?', (schedule['id'],))
        result = asyncio.run(app.state.automations.launch(schedule['id'], schedule['revision'], 'scheduled-check'))
    assert result == {'run_id': '', 'outcome': 'blocked'}
    assert not app.state.store.rows('SELECT id FROM runs')
    assert 'Project setup failed' in app.state.store.rows('SELECT detail FROM automation_runs')[0]['detail']


def test_environment_blocker_respects_selection_and_last_good_build(blocked):
    app, _, _ = blocked
    env, store = app.state.environments, app.state.store
    repo = 'https://github.com/BerriAI/moyai'
    assert env.setup_blocker('e' * 32, repo)
    assert env.setup_blocker('auto', repo)
    assert env.setup_blocker('none', repo) == ''
    assert env.setup_blocker('auto', 'https://github.com/BerriAI/litellm') == ''
    assert env.setup_blocker('auto', '') == ''
    store.execute('UPDATE environments SET is_default=1')
    assert env.setup_blocker('auto', '')
    # A failed refresh must never block sessions using an existing good snapshot.
    good = store.rows('SELECT id FROM environment_builds')[0]['id']
    env.update(good, phase='ready', snapshot_id='im-good')
    store.execute('UPDATE environments SET enabled=1,active_build=?', (good,))
    failed_refresh = env.enqueue('e' * 32, 1, 'admin')
    env.update(failed_refresh['id'], phase='failed')
    assert env.setup_blocker('auto', repo) == ''


def test_first_build_and_demo_mode_are_not_blocked(blocked):
    app, client, _ = blocked
    demo = create(client, repo_url='https://github.com/BerriAI/moyai')
    assert demo['environment_blocker'] == ''
    app.state.store.execute('DELETE FROM environment_builds')
    assert app.state.environments.setup_blocker('auto', 'https://github.com/BerriAI/moyai') == ''
