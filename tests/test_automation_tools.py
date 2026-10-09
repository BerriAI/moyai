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
    if name not in {'automation_list', 'automation_webhook_info', 'automation_environments'}:
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


def test_repository_must_be_selected_in_saved_connection(workspace, monkeypatch):
    from test_github import select, GitHubAPI
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?', ('["github"]', run['id']))
    app.state.connectors.github.save_app({'id': 123, 'pem': 'test', 'owner_id': 44})
    select(app)
    GitHubAPI(app.state.connectors.github, monkeypatch)
    args = {'request_key': 'skills-repo-check', 'definition': definition(plugins=['github'], repo_url='https://github.com/BerriAI/moyai')}
    assert call(client, run, 'automation_create', **args).status_code == 409
    assert not app.state.store.rows('SELECT * FROM automations')
    select(app, (101, 202))
    saved = call(client, run, 'automation_create', **args).json()
    assert saved['status'] == 'paused'
    select(app)
    runtime(app)
    assert call(client, run, 'automation_enable', automation_id=saved['id'], revision=1, request_key='repo-revoked').status_code == 409


def test_requester_or_cancellation_during_access_check_prevents_write(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    async def cancel(*args, **kwargs):
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


def test_environment_discovery_exposes_reusable_build_not_setup_scripts(workspace):
    from app.environments import Recipe, SaveRecipe
    from test_environments import prepared
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    env = app.state.environments
    build_id = prepared(env, default=False)
    # Editing a recipe retains the previously validated version until rebuilt.
    env.save_recipe('e' * 32, SaveRecipe(revision=1, recipe=Recipe(
        name='Edited setup', repository='BerriAI/moyai', setup='private installation script', verify='true')), 'admin')
    prepared(env, default=False, identity='f' * 32)
    env.save_recipe('d' * 32, SaveRecipe(recipe=Recipe(
        name='Disabled setup', repository='BerriAI/moyai', verify='true')), 'admin')
    result = call(client, run, 'automation_environments', limit=1)
    assert result.status_code == 200, result.text
    assert result.json()['next_offset'] == 1
    pages = [result.json(), call(client, run, 'automation_environments', offset=1, limit=1).json()]
    assert pages[1]['next_offset'] is None
    items = {item['id']: item for page in pages for item in page['environments']}
    assert set(items) == {'e' * 32, 'f' * 32}
    item = items['e' * 32]
    assert item['revision'] == 2 and item['repository'] == 'BerriAI/litellm'
    assert item['prepared_build'] == {'id': build_id, 'revision': 1, 'commit_sha': 'a' * 40, 'sandbox_provider': 'modal'}
    assert item['setup_blocker'] == ''
    assert not {'recipe', 'builds', 'setup', 'startup', 'verify', 'shutdown', 'instructions'} & item.keys()
    assert 'private installation script' not in str(pages)
    tools = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'}).json()
    tool = next(tool for tool in tools if tool['name'] == 'automation_environments')
    assert tool['annotations']['readOnlyHint'] is True
    saved = create(client, run, environment_id=item['id'])
    assert saved['definition']['environment_id'] == item['id']
    assert saved['definition']['repo_url'] == '' and saved['definition']['metadata'] == {}
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], run['id']))
    assert call(client, run, 'automation_environments').status_code == 403


def test_environment_discovery_reports_unbuilt_setup_blocker(workspace):
    from app.environments import Recipe, SaveRecipe
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    env = app.state.environments
    env.save_recipe('e' * 32, SaveRecipe(recipe=Recipe(name='Database setup', repository='BerriAI/litellm', verify='true')), 'admin')
    env.store.execute('UPDATE environments SET activate_on_ready=1')
    build = env.enqueue('e' * 32, 1, 'admin')
    env.update(build['id'], phase='failed')
    item = call(client, run, 'automation_environments').json()['environments'][0]
    assert item['prepared_build'] is None
    assert 'Project setup failed' in item['setup_blocker']
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1  # Discovery never retries setup.


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


def webhook_credential(app, client, run, *, value='synthetic-webhook-secret', lifetime='persistent', name='webhook-signing-secret', expires_at=''):
    import json
    from app.credentials import CredentialRequest
    saved = client.post('/api/credentials/secrets', json={
        'provider': 'generic', 'name': name, 'format': 'env', 'label': 'Webhook test',
        'scope': 'personal', 'lifetime': lifetime, 'root_id': run['id'] if lifetime == 'session' else '',
        'expires_at': expires_at, 'value': json.dumps({'WEBHOOK_SECRET': value}), 'client_id': 'webhook-' + str(len(value)),
    })
    assert saved.status_code == 201, saved.text
    result = app.state.credentials.request(run, CredentialRequest(
        provider='generic', name=name, format='env', secret_id=saved.json()['id'],
        reason='Configure a persistent automation receiver', request_key='webhook-' + str(len(value))))
    assert result['status'] == 'provided'
    return result['request_id']


def webhook_automation(client, run):
    return create(client, run, timing=None, event={'provider': 'webhook', 'event': 'probe.ready'})


def test_webhook_tools_configure_receiver_atomically_without_secret_disclosure(workspace):
    import json
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = webhook_automation(client, run)
    handle = webhook_credential(app, client, run)
    args = dict(automation_id=saved['id'], revision=1, provider='webhook',
                credential_request_id=handle, request_key='configure-webhook')
    info = call(client, run, 'automation_webhook_info', automation_id=saved['id'])
    assert info.status_code == 200
    assert not info.json()['receiver']['ready']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(client, run, 'automation_webhook_setup', **args), range(4)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert {r.json()['revision'] for r in results} == {2}
    result = results[0].json()
    assert result['paused'] and result['receiver']['ready']
    assert result['provider_registration'] == 'not_verified'
    assert result['receiver']['providers'][0]['url'].endswith('/' + saved['id'] + '/webhook')
    serialized = json.dumps(result) + json.dumps(app.state.store.rows('SELECT * FROM automation_operations'))
    assert 'synthetic-webhook-secret' not in serialized
    encrypted = app.state.store.rows('SELECT encrypted FROM automation_webhooks')[0]['encrypted']
    assert encrypted not in serialized
    assert app.state.security.decrypt(encrypted) == 'synthetic-webhook-secret'
    assert call(client, run, 'automation_webhook_setup', **{**args, 'provider': 'github'}).status_code == 409
    assert call(client, run, 'automation_webhook_setup', **{**args, 'request_key': 'stale-webhook-config'}).status_code == 409


@pytest.mark.parametrize('lifetime,name,value', [
    ('session', 'webhook-signing-secret', 'synthetic-webhook-secret'),
    ('persistent', 'other-capability', 'synthetic-webhook-secret'),
    ('persistent', 'webhook-signing-secret', 'short'),
    ('persistent', 'webhook-signing-secret', 'x' * 513),
])
def test_webhook_setup_rejects_wrong_credential_shape_without_echo(workspace, lifetime, name, value):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = webhook_automation(client, run)
    handle = webhook_credential(app, client, run, lifetime=lifetime, name=name, value=value)
    result = call(client, run, 'automation_webhook_setup', automation_id=saved['id'], revision=1,
                  provider='webhook', credential_request_id=handle, request_key='invalid-webhook')
    assert result.status_code == 422
    assert value not in result.text
    assert not app.state.store.rows('SELECT * FROM automation_webhooks')
    assert app.state.automations.row(saved['id'])['revision'] == 1


def test_webhook_setup_owner_turn_and_revocation_checks(workspace):
    app, client = workspace
    sign_in(app, client)
    alice = active(app)
    saved = webhook_automation(client, alice)
    handle = webhook_credential(app, client, alice)
    args = dict(automation_id=saved['id'], revision=1, provider='webhook',
                credential_request_id=handle, request_key='protected-webhook')
    assert call(client, alice, 'automation_webhook_setup', **args, turn_id=alice['active_message_id']+1).status_code == 409
    sign_in(app, client, 'bob', 'bob@berri.ai')
    app.state.settings.google_admin_emails = 'bob@berri.ai'
    bob = active(app, 'google:bob')
    assert call(client, bob, 'automation_webhook_info', automation_id=saved['id']).status_code == 404
    assert call(client, bob, 'automation_webhook_setup', **args).status_code == 404
    bobs = webhook_automation(client, bob)
    assert call(client, bob, 'automation_webhook_setup', **{**args, 'automation_id': bobs['id']}).status_code == 403
    app.state.store.execute("UPDATE provider_secrets SET revoked_at=?", (now(),))
    assert call(client, alice, 'automation_webhook_setup', **args).status_code == 403
    assert not app.state.store.rows('SELECT * FROM automation_webhooks')


def test_webhook_setup_checkpoint_recovery_and_rotation(workspace, monkeypatch):
    from test_automation_events import signed
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = webhook_automation(client, run)
    handle = webhook_credential(app, client, run, value='webhook-test-secret-only')
    args = dict(automation_id=saved['id'], revision=1, provider='webhook',
                credential_request_id=handle, request_key='recover-webhook')
    flush = app.state.automations.checkpoints.flush
    async def failed():
        raise RuntimeError('synthetic checkpoint failure')
    monkeypatch.setattr(app.state.automations.checkpoints, 'flush', failed)
    with pytest.raises(RuntimeError, match='synthetic checkpoint'):
        call(client, run, 'automation_webhook_setup', **args)
    monkeypatch.setattr(app.state.automations.checkpoints, 'flush', flush)
    assert call(client, run, 'automation_webhook_setup', **args).json()['revision'] == 2
    app.state.store.execute('UPDATE automations SET paused=0 WHERE id=?', (saved['id'],))
    path = '/hooks/automations/' + saved['id'] + '/webhook'
    payload = {'event': 'probe.ready'}
    assert client.post(path, **signed('webhook', payload)).json()['status'] == 'accepted'
    second = webhook_credential(app, client, run, value='rotated-webhook-secret')
    rotated = call(client, run, 'automation_webhook_setup', **{
        **args, 'revision': 2, 'credential_request_id': second, 'request_key': 'rotate-webhook'})
    assert rotated.json()['revision'] == 3 and rotated.json()['paused']
    assert client.post(path, **signed('webhook', payload)).status_code == 401
    assert call(client, run, 'automation_webhook_setup', **args).status_code == 409


def test_webhook_catalog_and_storage_failure_are_safe(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    catalog = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'}).json()
    tools = {t['name']: t for t in catalog}
    assert tools['automation_webhook_info']['annotations']['readOnlyHint']
    setup = tools['automation_webhook_setup']
    assert not setup['annotations']['readOnlyHint']
    assert 'secret' not in setup['inputSchema']['properties']
    assert 'credential_request_id' in setup['inputSchema']['required']
    saved = webhook_automation(client, run)
    handle = webhook_credential(app, client, run)
    args = dict(automation_id=saved['id'], revision=1, provider='webhook',
                credential_request_id=handle, request_key='rollback-webhook')
    response = call(client, run, 'automation_webhook_setup', **args, secret='canary-never-echo')
    assert response.status_code == 422 and 'canary-never-echo' not in response.text
    def fail_encrypt(value):
        raise RuntimeError('synthetic encryption failure')
    monkeypatch.setattr(app.state.security, 'encrypt', fail_encrypt)
    with pytest.raises(RuntimeError, match='synthetic encryption'):
        call(client, run, 'automation_webhook_setup', **args)
    assert app.state.automations.row(saved['id'])['revision'] == 1
    assert not app.state.store.rows('SELECT * FROM automation_webhooks')
    assert len(app.state.store.rows('SELECT * FROM automation_operations')) == 1


@pytest.mark.parametrize('provider,event', [('linear', 'issue.created'), ('pagerduty', 'incident.triggered'),
                                            ('github', 'push'), ('slack', 'message.posted'), ('webhook', 'probe.ready')])
def test_shared_receiver_configuration_validates_provider_invariant(workspace, provider, event):
    from app.automation_events import WebhookSetup
    from app.automations import Definition, Save
    from fastapi import HTTPException
    app, client = workspace
    sign_in(app, client)
    source = {'provider': provider, 'event': event}
    if provider == 'github':
        source.update(repository='BerriAI/example', repository_id=123)
    if provider == 'slack':
        source['channel_id'] = 'C12345678'
    if provider == 'linear':
        source['team_id'] = '11111111-1111-1111-1111-111111111111'
    model = Definition(name='Provider test', prompt='Test only', event=source)
    row = app.state.automations.save(Save(definition=model), 'google:alice')
    service = app.state.automations.events
    if provider == 'slack':
        with pytest.raises(HTTPException) as error:
            service.configure(row['id'], WebhookSetup(revision=1, provider=provider, secret='synthetic-valid-secret'), 'google:alice')
        assert error.value.status_code == 409
    else:
        if provider in {'linear', 'pagerduty'}:
            with pytest.raises(HTTPException) as error:
                service.configure(row['id'], WebhookSetup(revision=1, provider=provider), 'google:alice')
            assert error.value.status_code == 422
        secret, revision = service.configure(row['id'], WebhookSetup(revision=1, provider=provider, secret='synthetic-valid-secret'), 'google:alice')
        assert secret == 'synthetic-valid-secret' and revision == 2
    with pytest.raises(HTTPException):
        service.configure(row['id'], WebhookSetup(revision=2, provider=provider, secret='synthetic-valid-secret'), 'google:bob')


def test_webhook_setup_does_not_extend_credential_expiration(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    saved = webhook_automation(client, run)
    handle = webhook_credential(app, client, run, expires_at='2099-01-01T00:00:00+00:00')
    response = call(client, run, 'automation_webhook_setup', automation_id=saved['id'], revision=1,
                    provider='webhook', credential_request_id=handle, request_key='expiring-secret')
    assert response.status_code == 422
    assert not app.state.store.rows('SELECT * FROM automation_webhooks')
