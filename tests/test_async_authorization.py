"""Authorization remains bound to the captured caller across worker waits."""
import asyncio
import json
import threading

from fastapi import HTTPException
import pytest

from app.credentials import CredentialRequest
from app.db import database, now
from app.security import digest
from test_spend import active, sign_in
from test_workspace import workspace


def pause_operation(monkeypatch, owner, name, *, after=False):
    entered, release = threading.Event(), threading.Event()
    operation = getattr(owner, name)

    def paused(*args, **kwargs):
        result = operation(*args, **kwargs) if after else None
        entered.set()
        assert release.wait(5), 'The test did not release the paused operation.'
        return result if after else operation(*args, **kwargs)

    monkeypatch.setattr(owner, name, paused)
    return entered, release


def next_turn(store, run):
    store.finish_message(run['id'], run['active_message_id'], 'Previous turn finished')
    store.enqueue_message(run['id'], 'Continue as Bob', 'authorization-next-turn', user_id='google:bob')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('replacement-capability'))


def saved_note(client):
    response = client.post('/api/memory', json={
        'key': 'authorization-note', 'title': 'Personal preference', 'content': 'Keep explanations concise.',
        'kind': 'preference', 'request_id': 'authorization-save'})
    assert response.status_code == 201, response.text
    return response.json()


def saved_secret(client, **changes):
    response = client.post('/api/credentials/secrets', json={
        'provider': 'openai', 'label': 'Personal credential', 'scope': 'personal',
        'value': 'authorization-test-secret', 'client_id': 'authorization-key', **changes})
    assert response.status_code == 201, response.text
    return response.json()['id']


def slack_actor(store):
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute("UPDATE users SET email='alice@berri.ai',profile_eligible=1,profile_checked_at=? WHERE id=?",
                     (now(), actor))
    return actor


@pytest.mark.parametrize('change,status', [
    ('turn', 409), ('requester', 409), ('capability', 409), ('stopped', 409),
    ('paused', 403), ('delegated', 403), ('profile_revoked', 403),
])
async def test_memory_forget_revalidates_after_authorization_wait(workspace, monkeypatch, change, status):
    app, client = workspace
    sign_in(app, client)
    store, memory = app.state.store, app.state.memory
    note = saved_note(client)
    actor = slack_actor(store) if change == 'profile_revoked' else 'google:alice'
    run = active(app, actor)
    before = store.rows('SELECT * FROM personal_memories WHERE id=?', (note['id'],))
    entered, release = pause_operation(monkeypatch, memory, 'forget')
    pending = asyncio.create_task(database(memory.call, run, 'memory_forget', {
        'turn_id': run['active_message_id'], 'id': note['id'], 'revision': note['revision']}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if change == 'turn':
            next_turn(store, run)
        elif change == 'requester':
            store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
        elif change == 'capability':
            store.update_run(run['id'], token_hash=digest('replacement-capability'))
        elif change == 'stopped':
            store.update_run(run['id'], status='stopping')
        elif change == 'paused':
            response = client.put('/api/memory/preferences', json={'enabled': False})
            assert response.status_code == 200, response.text
        elif change == 'delegated':
            root = store.create_run('A delegated parent', '', 'demo', [])
            store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root['id'], run['id']))
        else:
            store.execute('UPDATE users SET profile_eligible=0 WHERE id=?', (actor,))
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(pending, 3)
        assert denied.value.status_code == status
        assert store.rows('SELECT * FROM personal_memories WHERE id=?', (note['id'],)) == before
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('operation', ['memory_search', 'memory_read'])
@pytest.mark.parametrize('change,status', [('turn', 409), ('paused', 403)])
async def test_memory_selection_revalidates_before_selection_write(
        workspace, monkeypatch, operation, change, status):
    app, client = workspace
    sign_in(app, client)
    store, memory = app.state.store, app.state.memory
    note, run = saved_note(client), active(app)
    if operation == 'memory_search':
        # The search has already read the old owner's notes, but has not yet
        # replaced selections. Revalidate before that replacement can commit.
        entered, release = pause_operation(monkeypatch, memory, 'listing', after=True)
        arguments = {'query': ''}
    else:
        entered, release = pause_operation(monkeypatch, memory, 'read')
        arguments = {'id': note['id'], 'revision': note['revision']}
    pending = asyncio.create_task(database(memory.call, run, operation, {
        'turn_id': run['active_message_id'], **arguments}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if change == 'turn':
            next_turn(store, run)
        else:
            response = client.put('/api/memory/preferences', json={'enabled': False})
            assert response.status_code == 200, response.text
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(pending, 3)
        assert denied.value.status_code == status
        assert not store.rows('SELECT * FROM memory_selections WHERE run_id=?', (run['id'],))
        assert store.rows('SELECT deleted FROM personal_memories WHERE id=?', (note['id'],)) == [{'deleted': 0}]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_current_slack_requester_can_forget_when_automatic_saving_is_off(workspace):
    app, client = workspace
    sign_in(app, client)
    note = saved_note(client)
    response = client.put('/api/memory/preferences', json={'auto_save': False})
    assert response.status_code == 200, response.text
    run = active(app, slack_actor(app.state.store))
    result = await database(app.state.memory.call, run, 'memory_forget', {
        'turn_id': run['active_message_id'], 'id': note['id'], 'revision': note['revision']})
    assert result == {'forgotten': True}
    assert app.state.store.rows('SELECT deleted,encrypted,revision FROM personal_memories WHERE id=?', (note['id'],)) == [
        {'deleted': 1, 'encrypted': '', 'revision': note['revision'] + 1}]


@pytest.mark.parametrize('change', ['turn', 'requester', 'capability', 'deleting', 'stopped'])
async def test_inventory_rejects_captured_capability_after_worker_wait(workspace, monkeypatch, change):
    app, client = workspace
    sign_in(app, client)
    store, vault = app.state.store, app.state.credentials
    saved_secret(client)
    run = active(app)
    entered, release = pause_operation(monkeypatch, vault, 'inventory')
    pending = asyncio.create_task(vault.call(run, 'credentials_list', {}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if change == 'turn':
            next_turn(store, run)
        elif change == 'requester':
            store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
        elif change == 'capability':
            store.update_run(run['id'], token_hash=digest('replacement-capability'))
        elif change == 'stopped':
            store.update_run(run['id'], status='stopping')
        else:
            store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), run['id']))
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(pending, 3)
        assert denied.value.status_code == 409
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_inventory_authority_and_results_share_one_snapshot_during_revocation(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    store, vault = app.state.store, app.state.credentials
    root, run = active(app), active(app)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root['id'], run['id']))
    run = store.run(run['id'])
    request = vault.request(run, CredentialRequest(
        provider='fireworks', reason='Inspect the requested models', request_key='snapshot-request'))
    actor = slack_actor(store)
    store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (actor, run['id']))
    run = store.run(run['id'])
    secret_id = saved_secret(client, provider='generic', name='1password-shared', format='env',
                             lifetime='session', root_id=root['id'],
                             value=json.dumps({'OP_SERVICE_ACCOUNT_TOKEN': 'authorization-test-secret'}))
    entered, release = pause_operation(monkeypatch, vault, 'pending_rows')
    pending = asyncio.create_task(vault.call(run, 'credentials_list', {}))
    try:
        # Secret permissions have been read. Revoke the secret and the verified
        # identity while this snapshot is still gathering pending requests.
        assert await asyncio.to_thread(entered.wait, 3)
        with store.connect() as conn:
            conn.begin_write()
            conn.execute('UPDATE provider_secrets SET revoked_at=?,revision=revision+1 WHERE id=?', (now(), secret_id))
            conn.execute('UPDATE users SET profile_eligible=0 WHERE id=?', (actor,))
            conn.execute("UPDATE credential_requests SET status='declined' WHERE id=?", (request['request_id'],))
        release.set()
        result = await asyncio.wait_for(pending, 3)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert [item['id'] for item in result['credentials']] == [secret_id]
    assert [item['id'] for item in result['credential_sources']] == [secret_id]
    assert [item['request_id'] for item in result['pending_requests']] == [request['request_id']]
    assert result['discovery']['available']['shared_vault_sources'] == 1
    assert 'authorization-test-secret' not in json.dumps(result)
    # A fresh snapshot must honor every committed revocation.
    refreshed = await vault.call(run, 'credentials_list', {})
    assert {key: refreshed[key] for key in ('credentials', 'credential_sources', 'pending_requests')} == {
        'credentials': [], 'credential_sources': [], 'pending_requests': []}
    assert refreshed['discovery']['available'] == {
        'saved_personal_connections': 0, 'saved_organization_connections': 0,
        'shared_vault_sources': 0, 'capability_verification': 'not_assessed',
    }


async def test_private_inventory_checks_stored_owner_and_memory_delete_stays_forbidden(workspace):
    app, client = workspace
    sign_in(app, client)
    store, vault = app.state.store, app.state.credentials
    note, secret_id = saved_note(client), saved_secret(client)
    run = store.create_run('A private request', '', 'modal', [], chat_enabled=True,
                           user_id='google:alice', private_owner_id='google:alice')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('capability'))
    run = {**store.run(run['id']), 'private_owner_id': ''}
    result = await vault.call(run, 'credentials_list', {})
    assert [item['id'] for item in result['credentials']] == [secret_id]
    with pytest.raises(HTTPException) as denied:
        await database(app.state.memory.forget, 'google:alice', note['id'], note['revision'], run=run)
    assert denied.value.status_code == 403
    assert store.rows('SELECT deleted FROM personal_memories WHERE id=?', (note['id'],)) == [{'deleted': 0}]
    # Forging caller privacy metadata cannot authorize a different stored actor.
    store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
    run = {**store.run(run['id']), 'private_owner_id': ''}
    with pytest.raises(HTTPException) as denied:
        await vault.call(run, 'credentials_list', {})
    assert denied.value.status_code == 404
