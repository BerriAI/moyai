"""Real broker requests: requester isolation, durable retries and schedule confirmation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from temporalio.testing import WorkflowEnvironment

from app.automation_tools import TOOL_NAMES
from app.db import now
from test_spend import active, sign_in
from test_workspace import workspace


def definition(**changes):
    return {'name': 'Weekly skills audit', 'prompt': 'Audit skills and open a PR only when updates are needed.',
            'timing': {'frequency': 'weekly', 'weekday': 1, 'time': '09:00', 'timezone': 'America/Los_Angeles'},
            **changes}


def call(client, run, name, **arguments):
    if name != 'automation_list':
        arguments.setdefault('turn_id', run['active_message_id'])
    return client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
                       json={'name': name, 'arguments': arguments})


def create(client, run, **changes):
    response = call(client, run, 'automation_create', request_key='create-weekly-audit', definition=definition(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def runtime(app):
    app.state.settings.temporal_enabled = True
    app.state.settings.modal_token_id = 'test-runtime-id'
    app.state.settings.modal_token_secret = 'test-runtime-secret'
    app.state.settings.litellm_api_base = 'https://model.example/v1'
    app.state.settings.litellm_api_key = 'test-runtime-key'


def test_tools_available_in_verified_chat_and_create_retries_are_atomic(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    tools = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'}).json()
    assert TOOL_NAMES <= {t['name'] for t in tools}
    listing = call(client, run, 'automation_list').json()
    assert listing == {'turn_id': run['active_message_id'], 'automations': [], 'next_offset': None,
                       'instruction': 'Read an automation by automation_id for its full definition and confirmed next run before editing.'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(client, run), range(4)))
    assert len({r['id'] for r in results}) == 1
    assert all(r['status'] == 'paused' and r['next_run_at'] is None for r in results)
    assert len(app.state.store.rows('SELECT * FROM automations')) == 1
    assert len(app.state.store.rows('SELECT * FROM automation_operations')) == 1
    conflict = call(client, run, 'automation_create', request_key='create-weekly-audit', definition=definition(name='Changed request'))
    assert conflict.status_code == 409
    detail = call(client, run, 'automation_list', automation_id=results[0]['id']).json()['automations'][0]
    assert detail['definition']['triggers'][0]['schedule']['frequency'] == 'weekly'
    assert detail['revision'] == 1
    # Owner supplied by the model cannot override server identity.
    assert call(client, run, 'automation_create', request_key='forged-owner', definition=definition(), owner_id='google:bob').status_code == 422


def test_other_requester_cannot_read_edit_enable_or_pause_even_as_admin(workspace):
    app, client = workspace
    sign_in(app, client)
    alice = active(app)
    saved = create(client, alice)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    app.state.settings.google_admin_emails = 'bob@berri.ai'
    bob = active(app, 'google:bob')
    assert call(client, bob, 'automation_list').json()['automations'] == []
    assert call(client, bob, 'automation_list', automation_id=saved['id']).status_code == 404
    for name in ('automation_update', 'automation_enable', 'automation_pause'):
        args = {'automation_id': saved['id'], 'revision': 1, 'request_key': name}
        if name == 'automation_update':
            args['definition'] = saved['definition']
        assert call(client, bob, name, **args).status_code == 404
    # The active requester owns new work, not the original session owner.
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (alice['id'],))
    created = create(client, alice, name='Bob’s check')
    assert app.state.automations.row(created['id'])['owner_id'] == 'google:bob'


def test_slack_profile_must_freshly_match_verified_google_identity(workspace):
    app, client = workspace
    sign_in(app, client)
    with app.state.store.connect() as conn:
        actor = app.state.store.slack_identity_in(conn, 'T12345678', 'U12345678')
    run = active(app, actor)
    assert call(client, run, 'automation_list').status_code == 403
    app.state.store.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=? WHERE id=?", (now(), actor))
    saved = create(client, run)
    assert app.state.automations.row(saved['id'])['owner_id'] == 'google:alice'
    assert call(client, active(app), 'automation_list').json()['automations'][0]['id'] == saved['id']
    app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00',linked_user_id='google:alice' WHERE id=?", (actor,))
    assert call(client, run, 'automation_pause', automation_id=saved['id'], revision=1, request_key='stale-profile').status_code == 403


def test_automation_and_delegated_runs_cannot_create_recursive_schedules(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], run['id']))
    assert call(client, run, 'automation_list').status_code == 403
    assert not app.state.automation_tools.tools(run)
    app.state.store.execute("UPDATE runs SET parent_run_id='' WHERE id=?", (run['id'],))
    saved = create(client, run)
    app.state.store.execute("INSERT INTO automation_runs VALUES(?,?,1,?,'started','',?)", ('tick', saved['id'], run['id'], now()))
    assert call(client, run, 'automation_create', request_key='recursive-create', definition=definition()).status_code == 403
    assert not app.state.automation_tools.tools(run)
    assert app.state.automations.tools(run)[0]['name'] == 'automation_claim_item'


def test_turn_revision_scope_and_execution_checks(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    assert call(client, run, 'automation_create', turn_id=run['active_message_id'] + 1, request_key='wrong-turn', definition=definition()).status_code == 409
    assert call(client, run, 'automation_create', request_key='more-access', definition=definition(plugins=['github'])).status_code == 403
    assert call(client, run, 'automation_create', request_key='change-mode', definition=definition(mode='demo')).status_code == 403
    saved = create(client, run)
    args = {'automation_id': saved['id'], 'revision': 1, 'request_key': 'enable-weekly'}
    assert call(client, run, 'automation_enable', **args).status_code == 409  # Temporal disabled
    app.state.settings.temporal_enabled = True
    assert call(client, run, 'automation_enable', **args).status_code == 409  # Runtime incomplete
    runtime(app)
    enabled = call(client, run, 'automation_enable', **args).json()
    assert enabled['revision'] == 2 and not enabled['paused']
    assert enabled['status'] == 'scheduler_unavailable' and enabled['next_run_at'] is None
    assert call(client, run, 'automation_enable', **args).json() == enabled
    edited = call(client, run, 'automation_update', automation_id=saved['id'], revision=2,
                  request_key='change-schedule', definition=definition(timing={'frequency': 'daily', 'time': '10:30', 'timezone': 'UTC'})).json()
    assert edited['paused'] and edited['revision'] == 3
    assert call(client, run, 'automation_enable', **args).status_code == 409  # Retry must not re-enable old work
    assert call(client, run, 'automation_pause', automation_id=saved['id'], revision=2, request_key='stale-revision').status_code == 409
    read = call(client, run, 'automation_list', automation_id=saved['id']).json()['automations'][0]
    assert read['definition']['triggers'][0]['schedule']['time'] == '10:30'


def test_repository_must_be_in_both_configuration_and_installation(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?', ('["github"]', run['id']))
    app.state.connectors.save('github', {'kind': 'github_app', 'repositories': ['BerriAI/litellm']}, 'Test installation')
    args = {'request_key': 'skills-repo-check', 'definition': definition(plugins=['github'], repo_url='https://github.com/BerriAI/litellm-skills')}
    assert call(client, run, 'automation_create', **args).status_code == 409
    app.state.settings.github_repositories = 'BerriAI/litellm,BerriAI/litellm-skills'
    assert call(client, run, 'automation_create', **args).status_code == 409
    assert not app.state.store.rows('SELECT * FROM automations')
    app.state.connectors.save('github', {'kind': 'github_app', 'repositories': ['BerriAI/litellm', 'BerriAI/litellm-skills']}, 'Test installation')
    saved = call(client, run, 'automation_create', **args).json()
    assert saved['status'] == 'paused'
    app.state.settings.github_repositories = 'BerriAI/litellm'
    runtime(app)
    assert call(client, run, 'automation_enable', automation_id=saved['id'], revision=1, request_key='repo-revoked').status_code == 409


def test_requester_or_cancellation_during_access_check_prevents_write(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    async def cancel(*args):
        app.state.store.update_run(run['id'], status='cancelled', token_hash='')
    monkeypatch.setattr(app.state.automation_tools, 'check_scope', cancel)
    assert call(client, run, 'automation_create', request_key='cancelled-request', definition=definition()).status_code == 403
    assert not app.state.store.rows('SELECT * FROM automations')
    # A switch while a read is awaiting scheduler confirmation cannot return private instructions.
    run = active(app)
    monkeypatch.undo()
    saved = create(client, run)
    original = app.state.automation_tools.result
    async def switch(row, **kwargs):
        result = await original(row, **kwargs)
        app.state.store.execute("UPDATE runs SET active_user_id='unverified' WHERE id=?", (run['id'],))
        return result
    monkeypatch.setattr(app.state.automation_tools, 'result', switch)
    assert call(client, run, 'automation_list', automation_id=saved['id']).status_code == 403


def test_list_paginates_without_exposing_saved_prompts(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    for i in range(3):
        assert call(client, run, 'automation_create', request_key=f'paginated-save-{i}', definition=definition(name=f'Check {i}', prompt='PRIVATE SAVED PROMPT')).status_code == 200
    first = call(client, run, 'automation_list', limit=2)
    assert first.json()['next_offset'] == 2 and len(first.json()['automations']) == 2
    assert 'PRIVATE SAVED PROMPT' not in first.text
    second = call(client, run, 'automation_list', limit=2, offset=2).json()
    assert second['next_offset'] is None and len(second['automations']) == 1


async def test_committed_mutation_recovers_after_lost_checkpoint_ack(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    args = {'turn_id': run['active_message_id'], 'request_key': 'lost-response', 'definition': definition()}
    attempts = 0
    original = app.state.checkpoints.flush if hasattr(app.state, 'checkpoints') else app.state.automations.checkpoints.flush
    async def flush():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError('response lost after commit')
        await original()
    monkeypatch.setattr(app.state.automations.checkpoints, 'flush', flush)
    with pytest.raises(ConnectionError):
        await app.state.automation_tools.call(run, 'automation_create', args)
    recovered = await app.state.automation_tools.call(run, 'automation_create', args)
    assert recovered['revision'] == 1 and len(app.state.store.rows('SELECT * FROM automations')) == 1


def test_scheduler_failure_does_not_claim_success_or_leak_diagnostics(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = create(client, run)
    runtime(app)
    class Unavailable:
        async def create_schedule(self, *args, **kwargs):
            raise ConnectionError('SECRET scheduler connection string')
    app.state.manager.temporal = Unavailable()
    response = call(client, run, 'automation_enable', automation_id=saved['id'], revision=1, request_key='enable-offline')
    assert response.status_code == 200
    assert response.json()['status'] == 'pending_sync'
    assert response.json()['next_run_at'] is None and 'SECRET' not in response.text


async def test_real_temporal_confirms_weekly_next_run_update_and_pause(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = create(client, run)
    runtime(app)
    tools = app.state.automation_tools
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        app.state.manager.temporal = env.client
        enabled = await tools.call(run, 'automation_enable', {'turn_id': run['active_message_id'],
            'request_key': 'real-enable', 'automation_id': saved['id'], 'revision': 1})
        assert enabled['status'] == 'enabled' and enabled['next_run_at']
        due = datetime.fromisoformat(enabled['next_runs'][0]['local_time'])
        assert due.weekday() == 0 and due.hour == 9 and due.minute == 0
        schedule = env.client.get_schedule_handle('moyai-automation-' + saved['id'])
        remote = await schedule.describe()
        assert enabled['next_run_at'] == min(remote.info.next_action_times).astimezone(timezone.utc).isoformat()
        updated = await tools.call(run, 'automation_update', {'turn_id': run['active_message_id'],
            'request_key': 'real-update', 'automation_id': saved['id'], 'revision': 2,
            'definition': definition(timing={'frequency': 'weekly', 'weekday': 3, 'time': '14:15', 'timezone': 'Asia/Tokyo'})})
        assert updated['paused'] and updated['next_run_at'] is None
        enabled = await tools.call(run, 'automation_enable', {'turn_id': run['active_message_id'],
            'request_key': 'enable-update', 'automation_id': saved['id'], 'revision': 3})
        due = datetime.fromisoformat(enabled['next_runs'][0]['local_time'])
        assert due.weekday() == 2 and due.hour == 14 and due.minute == 15
        paused = await tools.call(run, 'automation_pause', {'turn_id': run['active_message_id'],
            'request_key': 'real-pause', 'automation_id': saved['id'], 'revision': 4})
        assert paused['status'] == 'paused' and paused['next_run_at'] is None
        # The DB blocks future deliveries immediately, before asynchronous sync.
        assert (await app.state.automations.launch(saved['id'], 4, 'late-delivery'))['outcome'] == 'skipped'
        await app.state.automations.sync(env.client, automation_id=saved['id'])
        assert (await schedule.describe()).schedule.state.paused
        await schedule.delete()
