"""Behavioral regressions for operations that must fit in one DB connection.

Run normally for SQLite, or with --postgres-backend and
MOYAI_TEST_POSTGRES_URL for an isolated, single-connection PostgreSQL pool.
"""
import hashlib
import json
import os
from uuid import uuid4

from fastapi import HTTPException
import pytest

from app.automations import Definition, Save as SaveAutomation
from app import captures
from app.config import Settings
from app.credentials import CredentialRequest, Materialize, Resolve, SaveSecret, UpdateSecret
from app.db import Store, now
from app.main import create_app
from app.message_queue import MessageQueue
from app.security import digest
from app.skills import SearchSkills, SkillForm
from test_deepagents_runtime import durable_app  # noqa: F401
from test_github_write_access import call as github_access, decide as github_decide, external
from test_spend import sign_in
from test_workspace import workspace


ALICE = 'google:alice'
BOB = 'google:bob'
SECRET = json.dumps({'REVIEW_TOKEN': 'synthetic-connection-reuse-value'})
POOL_TIMEOUT = .5


def test_durable_deletion_reuses_its_existing_run_snapshot(durable_app):
    store = durable_app.state.store
    run = store.create_run('Delete an already stopped session.', '', 'modal', [])
    lifecycle = durable_app.state.session_lifecycle
    lifecycle.request_delete(run['id'], '', True)
    store.update_run(run['id'], status='cancelled')
    assert lifecycle.delete(run['id'], '', True) == {'id': run['id'], 'deleted': True}
    assert store.run(run['id'])['deleted_at']


@pytest.fixture(autouse=True)
def postgres_backend(request, monkeypatch):
    """Follow conftest's opt-in pattern, using only disposable audit schemas."""
    if not request.config.getoption('--postgres-backend'):
        yield
        return
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.fail('MOYAI_TEST_POSTGRES_URL is required with --postgres-backend.')
    import psycopg
    from psycopg import sql

    initialize = Store.__init__
    schemas, stores = {}, []

    def initialize_one_connection(store, directory, *args, **kwargs):
        key = str(directory.resolve())
        if key not in schemas:
            schema = 'moyai_audit_' + uuid4().hex
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            schemas[key] = schema
        kwargs.update(database_url=url, database_schema=schemas[key],
                      database_initialize=True, database_pool_size=1)
        initialize(store, directory, *args, **kwargs)
        # A regressed nested checkout fails promptly instead of waiting 10 s.
        store.database.pool.timeout = POOL_TIMEOUT
        stores.append(store)

    monkeypatch.setattr(Store, '__init__', initialize_one_connection)
    try:
        yield
    finally:
        for store in stores:
            store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            for schema in schemas.values():
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def single_connection_app(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, moyai_database_pool_size=1,
        agent_harness='hermes', litellm_api_key='',
        google_client_id='test-google-client', google_client_secret='test-google-secret',
        google_allowed_domains='example.test', google_admin_emails='alice@example.test',
        repository_context_enabled=False, bundled_skills_enabled=False,
    )
    # Exercise the real services without starting unrelated polling workers.
    app = create_app(settings)
    for subject in ('alice', 'bob'):
        app.state.store.identity({'method': 'google', 'identity': {
            'sub': subject, 'email': subject + '@example.test', 'name': subject.title()}})
    try:
        yield app
    finally:
        app.state.store.close()


def active_run(app, actor=ALICE, *, prompt='Review the current work.'):
    store = app.state.store
    run = store.create_run(prompt, '', 'modal', [], chat_enabled=True, user_id=actor)
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('test-capability'))
    return store.run(run['id'])


def save_secret(app, *, root_id='', owner=ALICE, name='review-service'):
    body = SaveSecret(
        provider='generic', name=name, format='env', label='Review service',
        scope='personal', lifetime='session' if root_id else 'persistent',
        root_id=root_id, value=SECRET, client_id='save-' + name,
    )
    with app.state.store.connect() as conn:
        conn.begin_write()
        return app.state.credentials.insert_secret(conn, body, owner, False)


def credential_request(name='review-service'):
    return CredentialRequest(provider='generic', name=name, format='env',
                             reason='Read the requested review data.', request_key='review-access')


def skill_form(name, *, scope='personal', revision=0, instructions='Check the saved review inputs.'):
    return SkillForm(name=name, description='Review the current work.', instructions=instructions,
                     scope=scope, revision=revision, client_id='save-' + name)


def automation_definition():
    return {
        'name': 'Weekly review', 'prompt': 'Review the current work.',
        'timing': {'frequency': 'weekly', 'weekday': 1, 'time': '09:00', 'timezone': 'UTC'},
    }


def test_duplicate_create_run_returns_original_receipt_without_another_session(single_connection_app):
    store = single_connection_app.state.store
    arguments = dict(chat_enabled=True, user_id=ALICE, client_id='same-submission')
    first = store.create_run('Review the current work.', '', 'modal', [], **arguments)
    repeated = store.create_run('Review the current work.', '', 'modal', [], **arguments)
    assert repeated['id'] == first['id']
    assert store.rows('SELECT id FROM runs') == [{'id': first['id']}]
    messages = store.rows('SELECT run_id,content,client_id FROM messages')
    assert messages == [{'run_id': first['id'], 'content': 'Review the current work.',
                         'client_id': 'new:same-submission'}]
    with pytest.raises(ValueError, match='different content'):
        store.create_run('Different work.', '', 'modal', [], **arguments)
    assert store.rows('SELECT run_id,content,client_id FROM messages') == messages


def test_session_secret_update_keeps_scope_and_rejects_a_stale_revision(single_connection_app):
    app = single_connection_app
    run = active_run(app)
    secret_id = save_secret(app, root_id=run['id'])
    updated = app.state.credentials.update_secret(
        secret_id, UpdateSecret(revision=1, label='Updated review service'), ALICE, False)
    assert (updated['id'], updated['revision'], updated['root_id'], updated['scope']) == (
        secret_id, 2, run['id'], 'personal')
    assert updated['label'] == 'Updated review service'
    with pytest.raises(HTTPException) as stale:
        app.state.credentials.update_secret(
            secret_id, UpdateSecret(revision=1, label='Stale edit'), ALICE, False)
    assert stale.value.status_code == 409
    assert app.state.credentials.list_secrets(ALICE)[0]['label'] == 'Updated review service'


def test_saved_session_access_reuses_one_handle_and_stays_in_its_root(single_connection_app):
    app = single_connection_app
    run = active_run(app)
    secret_id = save_secret(app, root_id=run['id'])
    credentials = app.state.credentials
    first = credentials.request(run, credential_request())
    assert first['status'] == 'provided'
    assert credentials.request(run, credential_request()) == first
    result = credentials.materialize(run, Materialize(request_ids=[first['request_id']]))
    assert result['status'] == 'ready'
    assert json.loads(result['bindings'][0]['value']) == json.loads(SECRET)
    assert app.state.store.rows('SELECT secret_id FROM credential_requests') == [{'secret_id': secret_id}]
    other_run = active_run(app)
    assert credentials.request(other_run, credential_request())['status'] == 'pending'
    with pytest.raises(HTTPException) as denied:
        credentials.materialize(other_run, Materialize(request_ids=[first['request_id']]))
    assert denied.value.status_code == 403


def test_providing_new_persistent_access_is_idempotent(single_connection_app):
    app = single_connection_app
    run = active_run(app)
    credentials = app.state.credentials
    pending = credentials.request(run, credential_request())
    supplied = Resolve(scope='personal', lifetime='persistent', value=SECRET, label='Persistent access')
    assert credentials.resolve(pending['request_id'], supplied, ALICE, False) == run['id']
    assert credentials.resolve(pending['request_id'], supplied, ALICE, False) == run['id']
    secrets = credentials.list_secrets(ALICE)
    assert len(secrets) == 1 and secrets[0]['lifetime'] == 'persistent' and secrets[0]['root_id'] == ''
    ready = credentials.request(run, credential_request())
    assert ready['request_id'] == pending['request_id'] and ready['status'] == 'provided'
    result = credentials.materialize(run, Materialize(request_ids=[ready['request_id']]))
    assert json.loads(result['bindings'][0]['value']) == json.loads(SECRET)


def test_saved_personal_access_requires_a_fresh_matching_slack_identity(single_connection_app):
    app = single_connection_app
    store, credentials = app.state.store, app.state.credentials
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, 'T12345678', 'U12345678')
    store.execute('UPDATE users SET email=?,profile_eligible=1,profile_checked_at=? WHERE id=?',
                  ('alice@example.test', now(), actor))
    run = active_run(app, actor)
    save_secret(app)
    granted = credentials.request(run, credential_request())
    assert granted['status'] == 'provided'
    result = credentials.materialize(run, Materialize(request_ids=[granted['request_id']]))
    assert json.loads(result['bindings'][0]['value']) == json.loads(SECRET)
    store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?", (actor,))
    result = credentials.materialize(run, Materialize(request_ids=[granted['request_id']]))
    assert result['status'] == 'pending' and 'bindings' not in result


def test_skill_search_keeps_visibility_and_current_turn_checks(single_connection_app):
    app = single_connection_app
    skills = app.state.skills
    skills.save(skill_form('alice-review'), ALICE, False)
    skills.save(skill_form('shared-review', scope='organization'), ALICE, True)
    skills.save(skill_form('bob-review'), BOB, False)
    run = active_run(app)
    result = skills.search(run, SearchSkills(query='review', turn_id=run['active_message_id']))
    assert {item['reference'] for item in result['matches']} == {
        'personal:alice-review', 'org:shared-review'}
    assert result['total'] == 2
    with pytest.raises(HTTPException) as stale:
        skills.search(run, SearchSkills(query='review', turn_id=run['active_message_id'] + 1))
    assert stale.value.status_code == 409


def test_skill_load_retry_keeps_the_already_loaded_revision(single_connection_app):
    app = single_connection_app
    skills = app.state.skills
    original = 'Check the original saved review inputs.'
    skill_id = skills.save(skill_form('review', instructions=original), ALICE, False)
    run = active_run(app)
    first = skills.load(run, 'personal:review')
    skills.save(skill_form('review', revision=1, instructions='A newer procedure.'), ALICE, False, skill_id)
    assert skills.load(run, 'personal:review') == first
    assert first['revision'] == 1 and first['loaded']
    uses = app.state.store.rows('SELECT revision,encrypted FROM skill_uses WHERE run_id=?', (run['id'],))
    assert len(uses) == 1 and uses[0]['revision'] == 1
    assert app.state.security.decrypt(uses[0]['encrypted']) == original


def test_agent_memory_save_is_private_idempotent_and_bound_to_requester(single_connection_app):
    app = single_connection_app
    quote = 'Keep status updates concise and include verification results.'
    run = active_run(app, prompt=quote)
    args = {
        'turn_id': run['active_message_id'], 'key': 'review-updates', 'title': 'Review updates',
        'content': quote, 'kind': 'preference', 'request_id': 'save-review-preference',
        'source_message_id': run['active_message_id'], 'source_quote': quote,
    }
    memory = app.state.memory
    saved = memory.call(run, 'memory_save', args)
    assert saved['saved'] and saved['revision'] == 1
    assert memory.call(run, 'memory_save', args) == saved
    notes = memory.listing(ALICE)
    assert len(notes) == 1 and notes[0]['content'] == quote
    assert notes[0]['source'] == {
        'type': 'chat', 'run_id': run['id'], 'message_id': run['active_message_id'], 'quote': quote}
    assert memory.listing(BOB) == []
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (BOB, run['id']))
    with pytest.raises(HTTPException) as stale:
        memory.call(run, 'memory_save', args)
    assert stale.value.status_code == 409
    assert memory.listing(ALICE) == notes and memory.listing(BOB) == []


async def test_automation_create_retry_has_one_receipt_and_rejects_changed_content(single_connection_app):
    app = single_connection_app
    run = active_run(app)
    args = {'turn_id': run['active_message_id'], 'request_key': 'create-weekly-review',
            'definition': automation_definition()}
    tools = app.state.automation_tools
    first = await tools.call(run, 'automation_create', args)
    repeated = await tools.call(run, 'automation_create', args)
    assert first['id'] == repeated['id'] and first['revision'] == repeated['revision'] == 1
    assert first['paused'] and first['status'] == 'paused'
    assert app.state.store.rows('SELECT id,owner_id FROM automations') == [{'id': first['id'], 'owner_id': ALICE}]
    assert app.state.store.rows('SELECT automation_id FROM automation_operations') == [{'automation_id': first['id']}]
    with pytest.raises(HTTPException) as conflict:
        await tools.call(run, 'automation_create', {
            **args, 'definition': {**args['definition'], 'name': 'Changed review'}})
    assert conflict.value.status_code == 409
    assert app.state.automations.row(first['id'])['revision'] == 1


async def test_automation_pause_retry_preserves_revision_and_owner(single_connection_app):
    app = single_connection_app
    row = app.state.automations.save(
        SaveAutomation(definition=Definition.model_validate(automation_definition())), ALICE)
    # Represent an already-enabled schedule without contacting a scheduler.
    app.state.store.execute('UPDATE automations SET paused=0 WHERE id=?', (row['id'],))
    run = active_run(app)
    args = {'turn_id': run['active_message_id'], 'request_key': 'pause-weekly-review',
            'automation_id': row['id'], 'revision': row['revision']}
    first = await app.state.automation_tools.call(run, 'automation_pause', args)
    repeated = await app.state.automation_tools.call(run, 'automation_pause', args)
    assert first['paused'] and repeated['paused']
    assert first['revision'] == repeated['revision'] == row['revision'] + 1
    other = active_run(app, BOB)
    with pytest.raises(HTTPException) as denied:
        await app.state.automation_tools.call(other, 'automation_pause', {
            **args, 'turn_id': other['active_message_id'], 'revision': first['revision']})
    assert denied.value.status_code == 404
    assert app.state.automations.row(row['id'])['revision'] == first['revision']


@pytest.mark.parametrize('decision', ['approve', 'deny'])
def test_github_write_decision_preserves_verified_aliases_and_terminal_consent(external, decision):
    app, client, run_id, _, _, api = external
    store = app.state.store
    actor = sign_in(app, client)
    with store.connect() as conn:
        slack_actor = store.slack_identity_in(conn, 'T12345678', 'U87654321')
    store.execute("""UPDATE users SET email='alice@berri.ai',linked_user_id=?,profile_eligible=1,
        profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?""", (actor, slack_actor))
    store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (slack_actor, run_id))
    slack_response = github_access(external)
    assert slack_response.status_code == 200, slack_response.text
    slack_request = slack_response.json()
    assert slack_request['status'] == 'pending'
    store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (actor, run_id))
    web_response = github_access(external)
    assert web_response.status_code == 200, web_response.text
    web_request = web_response.json()
    assert web_request['status'] == 'pending'
    assert web_request['id'] != slack_request['id']

    # A fresh verified profile permits deciding the Slack request as its web
    # alias; approval also checks the current App configuration and org policy.
    store.execute('UPDATE users SET profile_checked_at=? WHERE id=?', (now(), slack_actor))
    expected = 'approved' if decision == 'approve' else 'denied'
    for _ in range(2):
        response = github_decide(external, slack_request['id'], decision)
        assert response.status_code == 200, response.text
        assert response.json() == {'status': expected}
    assert github_access(external).json()['status'] == expected

    terminal = 'denied'
    if decision == 'approve':
        response = github_decide(external, slack_request['id'], 'revoke')
        assert response.status_code == 200, response.text
        terminal = 'revoked'
        assert response.json() == {'status': terminal}
    rows = store.rows('SELECT status FROM github_write_access WHERE run_id=?', (run_id,))
    assert len(rows) == 2 and {row['status'] for row in rows} == {terminal}
    store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?", (slack_actor,))
    assert github_decide(external, web_request['id'], 'approve').status_code == 409
    for current in (actor, slack_actor):
        store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (current, run_id))
        assert github_access(external).json()['status'] == terminal
    assert not [method for method, _, _ in api.calls if method != 'GET']


@pytest.fixture
def slack_connection_app(single_connection_app):
    app = single_connection_app
    settings = app.state.settings
    settings.slack_bot_enabled = True
    settings.slack_signing_secret = 'test-slack-signature'
    settings.slack_session_users = '*'
    settings.slack_thread_chat_enabled = True
    app.state.connectors.save('slack', {
        'kind': 'oauth', 'access_token': 'synthetic-user-token',
        'bot': {'access_token': 'synthetic-bot-token', 'team': {'id': 'T12345678'},
                'bot_user_id': 'U99999999', 'scope': 'reactions:write,channels:history,groups:history'},
    }, 'Test Slack workspace')
    run = app.state.slack.chat.accept(
        team='T12345678', event_id='EvConnectionReuse', channel='C12345678',
        ts='1790719000.123456', root='1790719000.123456', user='U12345678',
        prompt='Review with <@U12345678> and <@U99999999>.', mentioned=True, missing_cloud=[])
    message = app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running')
    return app, app.state.store.run(run['id']), message


def test_slack_publication_callbacks_preserve_receipts_and_mention_scope(slack_connection_app):
    app, run, message = slack_connection_app
    store, chat = app.state.store, app.state.slack.chat
    for _ in range(2):
        store.event(run['id'], 'message', 'Checking the review inputs.',
                    {'phase': 'commentary', 'activity_id': 'review-progress', 'input_id': message['id']})
    progress = store.rows("SELECT * FROM slack_outbox WHERE run_id=? AND kind='progress'", (run['id'],))
    assert len(progress) == 1 and progress[0]['status'] == 'pending'
    answer = 'Ready for <@U12345678>, <@U99999999> and <@U87654321>.'
    for _ in range(2):
        store.finish_message(run['id'], message['id'], answer)
        chat.collect_thread(run['id'])
    answers = store.rows("SELECT * FROM messages WHERE run_id=? AND role='assistant'", (run['id'],))
    outbox = store.rows("SELECT * FROM slack_outbox WHERE run_id=? AND kind='answer'", (run['id'],))
    assert len(answers) == len(outbox) == 1
    assert answers[0]['response_to_id'] == message['id'] and answers[0]['content'] == answer
    assert '<@U12345678>' in outbox[0]['text']
    assert '<@U99999999>' not in outbox[0]['text'] and '<@U87654321>' not in outbox[0]['text']
    assert '&lt;@U99999999&gt;' in outbox[0]['text'] and '&lt;@U87654321&gt;' in outbox[0]['text']


def test_slack_queued_message_callbacks_retire_input_and_correct_delivered_edit(slack_connection_app):
    app, run, _ = slack_connection_app
    store, chat = app.state.store, app.state.slack.chat
    message, created = chat.enqueue_web(run['id'], 'Original follow-up', 'web-follow-up', None, ALICE)
    assert created
    queue = MessageQueue(store, chat.change_queued_in)
    changed = queue.change(run['id'], message['id'], ALICE, False, 0, 'edit', 'Corrected follow-up')
    assert changed['revision'] == 1 and changed['content'] == 'Corrected follow-up'
    assert store.rows('SELECT status FROM slack_outbox WHERE dedupe_key=?',
                      (f"input:{message['id']}:0",)) == [{'status': 'skipped'}]
    corrections = store.rows("SELECT * FROM slack_outbox WHERE run_id=? AND kind='input_update'", (run['id'],))
    assert len(corrections) == 1 and 'Corrected follow-up' in corrections[0]['text']
    store.execute("UPDATE slack_outbox SET status='sent',slack_ts='1790719999.123456' WHERE id=?",
                  (corrections[0]['id'],))
    removed = queue.change(run['id'], message['id'], ALICE, False, 1, 'delete')
    assert removed['status'] == 'deleted' and removed['revision'] == 2
    notices = store.rows("SELECT text FROM slack_outbox WHERE run_id=? AND kind='input_update' AND status='pending'",
                         (run['id'],))
    assert len(notices) == 1 and 'removed in Moyai' in notices[0]['text']


def test_slack_credential_collection_keeps_one_card_for_the_original_installation(slack_connection_app):
    app, run, _ = slack_connection_app
    request = app.state.credentials.request(run, credential_request())
    assert request['status'] == 'pending'
    for _ in range(2):
        app.state.slack.chat.collect_thread(run['id'])
    cards = app.state.store.rows('SELECT metadata,status FROM slack_outbox WHERE dedupe_key=?',
                                 ('credential:' + request['request_id'],))
    assert len(cards) == 1 and cards[0]['status'] == 'pending'
    metadata = json.loads(cards[0]['metadata'])
    assert metadata['credential_request_id'] == request['request_id']
    assert (metadata['team_id'], metadata['bot_user_id'], metadata['channel'], metadata['thread_ts']) == (
        'T12345678', 'U99999999', 'C12345678', '1790719000.123456')


def test_slack_publication_observes_policy_revocation_in_the_same_transaction(slack_connection_app):
    app, run, message = slack_connection_app
    store = app.state.store
    with store.connect() as conn:
        conn.begin_write()
        conn.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
        store.finish_message_in(conn, run['id'], message['id'], 'Saved while Slack is disabled.', 'completed')
    assert store.rows("SELECT status FROM messages WHERE run_id=? AND role='assistant'",
                      (run['id'],)) == [{'status': 'completed'}]
    assert not store.rows('SELECT 1 FROM slack_outbox WHERE run_id=?', (run['id'],))
    store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='slack'")
    app.state.slack.chat.collect_thread(run['id'])
    assert not store.rows('SELECT 1 FROM slack_outbox WHERE run_id=?', (run['id'],))


def test_slack_answer_and_outbox_roll_back_together_when_callback_fails(slack_connection_app, monkeypatch):
    app, run, message = slack_connection_app
    store, chat = app.state.store, app.state.slack.chat
    queue = chat.queue

    def fail_after_queuing(*args, **kwargs):
        queue(*args, **kwargs)
        raise RuntimeError('Synthetic publication failure')

    with monkeypatch.context() as patch:
        patch.setattr(chat, 'queue', fail_after_queuing)
        with pytest.raises(RuntimeError, match='Synthetic publication failure'):
            store.finish_message(run['id'], message['id'], 'Atomic answer.')
    assert store.rows('SELECT status FROM messages WHERE id=?', (message['id'],)) == [{'status': 'running'}]
    assert not store.rows("SELECT 1 FROM messages WHERE run_id=? AND role='assistant'", (run['id'],))
    assert not store.rows('SELECT 1 FROM slack_outbox WHERE run_id=?', (run['id'],))
    store.finish_message(run['id'], message['id'], 'Atomic answer.')
    assert len(store.rows("SELECT id FROM slack_outbox WHERE run_id=? AND kind='answer'", (run['id'],))) == 1


def test_slack_local_capture_publication_selects_only_referenced_saved_media(slack_connection_app):
    app, run, message = slack_connection_app
    store = app.state.store
    raw = b'\x89PNG\r\n\x1a\nsynthetic-selected-capture'
    store.artifacts.save(run['id'] + '-captures/result.png', raw)
    store.artifacts.save(run['id'] + '-captures/unrelated.png', b'\x89PNG\r\n\x1a\nnot-selected')
    answer = '[Screenshot](moyai-captures/result.png) and [missing](moyai-captures/missing.png)'
    store.finish_message(run['id'], message['id'], answer)
    app.state.slack.chat.collect_thread(run['id'])
    rows = store.rows("SELECT text,metadata FROM slack_outbox WHERE run_id=? AND kind='answer'", (run['id'],))
    selected = [item for row in rows for item in json.loads(row['metadata']).get('captures', [])]
    assert selected == [{'name': 'result.png', 'sha256': hashlib.sha256(raw).hexdigest()}]
    assert len(rows) == 2
    assert not any('unrelated.png' in json.dumps(row) for row in rows)


def test_capture_read_with_existing_connection_preserves_private_owner_check(single_connection_app):
    app = single_connection_app
    store = app.state.store
    run = store.create_run('Private capture.', '', 'modal', [], chat_enabled=True,
                           user_id=ALICE, private_owner_id=ALICE)
    raw = b'\x89PNG\r\n\x1a\nsynthetic-private-capture'
    store.artifacts.save(run['id'] + '-captures/private.png', raw)
    path = captures.directory(app.state.settings, run['id']) / 'private.png'
    with store.connect() as conn:
        with pytest.raises(HTTPException) as denied:
            captures.read(path, store=store, actor=BOB, connection=conn)
        assert denied.value.status_code == 404
        assert captures.read(path, store=store, actor=ALICE, connection=conn) == (raw, 'image/png')
