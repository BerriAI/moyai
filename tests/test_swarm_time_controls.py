"""Authenticated time controls keep the same durable swarm and saved work."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from agent.swarm.timing import MAX_DURATION_SECONDS
from app.attachments import inspect_file
from app.config import Settings
from app.main import create_app
from test_spend import sign_in


@pytest.fixture
def swarm_api(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, public_url='https://workspace.test',
        workspace_password='test-workspace-password', temporal_enabled=True,
        session_secret='stable-test-key', modal_token_id='fixture-token',
        modal_token_secret='fixture-secret', litellm_api_key='fixture-key',
        litellm_api_base='https://gateway.test/v1', session_titles_enabled=False,
        agent_model='anthropic/claude-opus-5-5', swarm_models='opus,sol',
    )
    app = create_app(settings)
    # Exercise real routes, authentication, transactions and dispatch records.
    # No lifespan means no external Temporal client or cloud worker is started.
    client = TestClient(app, base_url=settings.public_url)
    sign_in(app, client)
    try:
        yield app, client
    finally:
        client.close()
        app.state.store.close()


def start_swarm(client, budget=300):
    response = client.post('/api/runs', json={
        'prompt': 'Keep researching useful ideas together.', 'mode': 'modal',
        'swarm': {'budget_seconds': budget}, 'client_id': 'timing-' + uuid4().hex,
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def mission(app, root):
    return app.state.manager.swarms.get(root)


def roster(app, root):
    return app.state.store.rows('SELECT id FROM runs WHERE parent_run_id=? ORDER BY id', (root,))


def saved_expired_swarm(app, root):
    """Represent a completed checkpoint followed by the old deadline policy."""
    store = app.state.store
    for run_id in [root, *(row['id'] for row in roster(app, root))]:
        message = store.claim_message(run_id)
        assert message
        store.finish_message(run_id, message['id'], 'Previously saved research for ' + run_id)
        store.update_run(run_id, status='idle', snapshot_id='saved-' + run_id)
        store.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', ('{"phase":"idle"}', run_id))
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    store.execute("UPDATE swarm_missions SET status='expired',reason='Old time limit',ends_at=? WHERE run_id=?",
                  (expired, root))


@pytest.mark.parametrize('options', [{}, {'budget_seconds': 0}, {'budget_seconds': MAX_DURATION_SECONDS}])
def test_api_creates_unlimited_or_month_long_swarms_without_losing_the_roster(swarm_api, options):
    app, client = swarm_api
    response = client.post('/api/runs', json={
        'prompt': 'Investigate together over time.', 'mode': 'modal', 'swarm': options,
    })
    assert response.status_code == 201, response.text
    run = response.json()
    budget = options.get('budget_seconds', 0)
    assert run['swarm']['budget_seconds'] == budget
    assert run['swarm']['status'] == 'active'
    assert len(roster(app, run['id'])) == 10
    if budget == 0:
        assert run['swarm']['ends_at'] == ''
    else:
        remaining = (datetime.fromisoformat(run['swarm']['ends_at']) - datetime.now(timezone.utc)).total_seconds()
        assert budget - 5 <= remaining <= budget


@pytest.mark.parametrize('budget', [-1, 1, 59, MAX_DURATION_SECONDS + 1, True, False, '3600', 3600.5, None])
def test_api_rejects_invalid_duration_without_creating_work(swarm_api, budget):
    app, client = swarm_api
    response = client.post('/api/runs', json={
        'prompt': 'Investigate together.', 'mode': 'modal', 'swarm': {'budget_seconds': budget},
    })
    assert response.status_code == 422
    assert app.state.store.rows('SELECT id FROM runs') == []
    assert app.state.store.rows('SELECT run_id FROM swarm_missions') == []


def test_time_patch_updates_existing_swarm_to_days_then_until_stopped(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    children = roster(app, root)
    url = f'/api/runs/{root}/swarm/time'
    response = client.patch(url, json={'budget_seconds': 2 * 86400})
    assert response.status_code == 200, response.text
    assert response.json()['id'] == root
    assert response.json()['swarm']['budget_seconds'] == 2 * 86400
    remaining = (datetime.fromisoformat(response.json()['swarm']['ends_at']) - datetime.now(timezone.utc)).total_seconds()
    assert 2 * 86400 - 5 <= remaining <= 2 * 86400
    response = client.patch(url, json={'budget_seconds': 0})
    assert response.status_code == 200, response.text
    assert response.json()['swarm']['ends_at'] == ''
    assert response.json()['swarm']['budget_seconds'] == 0
    assert roster(app, root) == children
    assert len(app.state.store.messages(root)) == 1


@pytest.mark.parametrize('body', [{}, {'budget_seconds': True}, {'budget_seconds': 30},
                                 {'budget_seconds': MAX_DURATION_SECONDS + 1}, {'budget_seconds': '3600'},
                                 {'budget_seconds': 3600, 'owner_id': 'someone'}])
def test_invalid_time_patch_preserves_existing_deadline(swarm_api, body):
    app, client = swarm_api
    root = start_swarm(client)
    before = mission(app, root)
    response = client.patch(f'/api/runs/{root}/swarm/time', json=body)
    assert response.status_code == 422
    assert mission(app, root) == before


def test_chat_extension_is_atomic_and_applies_once_when_client_retries(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    before = mission(app, root)
    body = {'content': 'keep going for 3 more hours', 'client_id': 'one-extension-only'}
    url = f'/api/runs/{root}/messages'
    first = client.post(url, json=body)
    assert first.status_code == 202, first.text
    updated = mission(app, root)
    assert datetime.fromisoformat(updated['ends_at']) - datetime.fromisoformat(before['ends_at']) == timedelta(hours=3)
    assert first.json()['created'] is True
    retry = client.post(url, json=body)
    assert retry.status_code == 202, retry.text
    assert retry.json()['id'] == first.json()['id']
    assert retry.json()['created'] is False
    assert mission(app, root) == updated
    assert len(app.state.store.messages(root)) == 2
    changed = client.post(url, json={**body, 'content': 'add 2 days'})
    assert changed.status_code == 409
    assert mission(app, root) == updated
    assert len(app.state.store.messages(root)) == 2


@pytest.mark.parametrize(('content', 'budget'), [('run for two days', 172800), ('no time limit', 0)])
def test_plain_english_owner_message_sets_duration(swarm_api, content, budget):
    app, client = swarm_api
    root = start_swarm(client)
    response = client.post(f'/api/runs/{root}/messages', json={'content': content, 'client_id': 'set-time-from-chat'})
    assert response.status_code == 202, response.text
    updated = mission(app, root)
    assert updated['budget_seconds'] == budget
    assert bool(updated['ends_at']) == bool(budget)
    assert updated['status'] == 'active'


@pytest.mark.parametrize('content', ['continue', 'Keep researching, now focus on small teams.'])
def test_expired_swarm_accepts_followup_in_same_conversation_with_saved_work(swarm_api, content):
    app, client = swarm_api
    root = start_swarm(client)
    saved_expired_swarm(app, root)
    store = app.state.store
    before_messages = store.messages(root)
    children = roster(app, root)
    before_children = {row['id']: store.messages(row['id']) for row in children}
    response = client.post(f'/api/runs/{root}/messages', json={'content': content, 'client_id': 'continue-old-swarm'})
    assert response.status_code == 202, response.text
    current = store.run(root)
    assert current['id'] == root and current['status'] == 'queued'
    assert current['snapshot_id'] == 'saved-' + root
    assert current['swarm']['status'] == 'active' and not current['swarm']['reason']
    assert datetime.fromisoformat(current['swarm']['ends_at']) > datetime.now(timezone.utc)
    assert store.messages(root)[:-1] == before_messages
    assert roster(app, root) == children
    assert {row['id']: store.messages(row['id']) for row in children} == before_children


def test_internal_messages_and_direct_child_chat_cannot_extend_root_budget(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    before = mission(app, root)
    store = app.state.store
    store.enqueue_message(root, 'add 2 days', 'internal-command', user_id='google:alice')
    assert mission(app, root) == before
    child = roster(app, root)[0]['id']
    response = client.post(f'/api/runs/{child}/messages', json={
        'content': 'add 2 days', 'client_id': 'child-command-only',
    })
    assert response.status_code == 202, response.text
    assert mission(app, root) == before
    assert store.messages(child)[-1]['content'] == 'add 2 days'


def test_internal_continuation_cannot_revive_expired_mission(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    saved_expired_swarm(app, root)
    before = mission(app, root)
    with pytest.raises(ValueError, match='ended'):
        app.state.store.enqueue_message(root, 'continue', 'automatic-continuation', user_id='google:alice')
    assert mission(app, root) == before
    assert len(app.state.store.messages(root)) == 2


@pytest.mark.parametrize('invalid_auth', ['csrf', 'origin', 'session'])
def test_time_edit_and_chat_extension_preserve_authentication_and_csrf(swarm_api, invalid_auth):
    app, client = swarm_api
    root = start_swarm(client)
    before = mission(app, root)
    if invalid_auth == 'csrf':
        client.headers['X-CSRF-Token'] = 'wrong'
    elif invalid_auth == 'origin':
        client.headers['Origin'] = 'https://untrusted.example'
    else:
        client.cookies.clear()
    expected = 401 if invalid_auth == 'session' else 403
    edit = client.patch(f'/api/runs/{root}/swarm/time', json={'budget_seconds': 0})
    assert edit.status_code == expected, edit.text
    followup = client.post(f'/api/runs/{root}/messages', json={'content': 'add 2 days', 'client_id': 'bad-auth-extension'})
    assert followup.status_code == expected, followup.text
    assert mission(app, root) == before
    assert len(app.state.store.messages(root)) == 1


@pytest.mark.parametrize('failure', ['missing_attachment', 'after_attachment_binding', 'queue_full'])
def test_failed_chat_enqueue_rolls_back_time_change_and_attachment_binding(swarm_api, monkeypatch, failure):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    upload = store.attachments.save(uuid4().hex, 'google:alice', 'notes.txt', b'Useful notes',
                                    inspect_file(b'Useful notes'), 1024 * 1024)
    body = {'content': 'add 2 days', 'client_id': 'rollback-time-extension', 'attachment_ids': [upload['id']]}
    if failure == 'missing_attachment':
        body['attachment_ids'] = ['a' * 32]
    elif failure == 'after_attachment_binding':
        original = store.attachments.bind_in
        def fail_after_binding(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('Simulated enqueue failure after binding')
        monkeypatch.setattr(store.attachments, 'bind_in', fail_after_binding)
    else:
        store.update_run(root, status='idle')
        store.max_pending_runs = 1
    before = mission(app, root)
    events = store.rows('SELECT * FROM events WHERE run_id=? ORDER BY id', (root,))
    response = client.post(f'/api/runs/{root}/messages', json=body)
    assert response.status_code == 409, response.text
    assert mission(app, root) == before
    assert store.rows('SELECT * FROM events WHERE run_id=? ORDER BY id', (root,)) == events
    assert store.rows('SELECT message_id FROM attachments WHERE id=?', (upload['id'],)) == [{'message_id': None}]
    assert len(store.messages(root)) == 1


def test_time_edit_is_root_scoped_and_missing_swarm_is_reported(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    before = mission(app, root)
    child = roster(app, root)[0]['id']
    response = client.patch(f'/api/runs/{child}/swarm/time', json={'budget_seconds': 0})
    assert response.status_code == 409, response.text
    assert mission(app, root) == before
    response = client.patch('/api/runs/' + 'f' * 32 + '/swarm/time', json={'budget_seconds': 0})
    assert response.status_code == 404


def test_original_creation_retry_survives_later_time_edits_without_new_agents(swarm_api):
    app, client = swarm_api
    body = {
        'prompt': 'Persist this same collaborative mission.', 'mode': 'modal',
        'swarm': {'budget_seconds': 300}, 'client_id': 'stable-original-swarm-request',
    }
    response = client.post('/api/runs', json=body)
    assert response.status_code == 201, response.text
    root = response.json()['id']
    children = roster(app, root)
    for budget in (172800, 0):
        edit = client.patch(f'/api/runs/{root}/swarm/time', json={'budget_seconds': budget})
        assert edit.status_code == 200, edit.text
    updated = mission(app, root)
    retry = client.post('/api/runs', json=body)
    assert retry.status_code == 201, retry.text
    assert retry.json()['id'] == root
    assert mission(app, root) == updated
    assert roster(app, root) == children
    assert len(app.state.store.rows('SELECT id FROM runs')) == 11
    assert len(app.state.store.messages(root)) == 1
    conflict = client.post('/api/runs', json={**body, 'swarm': {'budget_seconds': 600}})
    assert conflict.status_code == 409, conflict.text
    assert mission(app, root) == updated
    assert roster(app, root) == children


@pytest.mark.parametrize(('status', 'content'), [
    ('paused', 'do not continue'), ('stopped', 'stop'), ('expired', 'pause'),
    ('blocked', 'stay paused'), ('paused', "don't resume"),
])
def test_negative_followup_does_not_resume_inactive_swarm(swarm_api, status, content):
    app, client = swarm_api
    root = start_swarm(client)
    saved_expired_swarm(app, root)
    app.state.store.execute('UPDATE swarm_missions SET status=? WHERE run_id=?', (status, root))
    before = mission(app, root)
    messages = app.state.store.messages(root)
    response = client.post(f'/api/runs/{root}/messages', json={
        'content': content, 'client_id': 'preserve-inactive-swarm',
    })
    assert response.status_code == 409, response.text
    assert 'still on hold' in response.json()['detail']
    assert mission(app, root) == before
    assert app.state.store.messages(root) == messages
