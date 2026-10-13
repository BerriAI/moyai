"""Real store, owner resolver and Slack HTTP adapter; no external Slack writes."""
import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.config import Settings
from app.db import database, now
from app.main import create_app
from test_spend import sign_in


@pytest.fixture
def notifications(tmp_path, monkeypatch):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
        agent_harness='hermes', agent_model='test-model', slack_bot_enabled=True, slack_session_users='*',
        auto_prepare_repositories=False, session_titles_enabled=False, slack_identity_linking_enabled=True))
    store = app.state.store
    app.state.connectors.save('slack', {'access_token': 'unused-user-token', 'kind': 'oauth',
        'bot': {'access_token': 'notification-bot-token', 'team': {'id': 'T12345678'},
                'bot_user_id': 'U99999999', 'scope': 'chat:write,im:write,users:read,users:read.email'}}, 'Test team')
    for person in ('alice', 'bob'):
        store.identity({'method': 'google', 'identity': {'sub': person, 'email': person + '@berri.ai', 'name': person}})
    requests = []

    def transport(request):
        assert request.headers['Authorization'] == 'Bearer notification-bot-token'
        requests.append(request)
        endpoint = request.url.path.rsplit('/', 1)[-1]
        if endpoint in {'users.lookupByEmail', 'users.info'}:
            return httpx.Response(200, json={'ok': True, 'user': {'id': 'U12345678', 'team_id': 'T12345678',
                'profile': {'email': 'alice@berri.ai', 'real_name': 'Alice'}}})
        if endpoint == 'conversations.open':
            assert json.loads(request.content)['users'] == 'U12345678'
            return httpx.Response(200, json={'ok': True, 'channel': {'id': 'D12345678'}})
        assert endpoint == 'chat.postMessage'
        return httpx.Response(200, json={'ok': True, 'ts': '1790719999.123456'})

    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(transport), **kw))
    yield app, requests
    store.close()


def create(app, owner='google:alice'):
    run = app.state.store.create_run('Investigate the checkout error', '', 'demo', [], chat_enabled=True, user_id=owner)
    message = app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running')
    return run['id'], message['id']


def finish(app, run_id, message_id, status='completed'):
    app.state.store.finish_message(run_id, message_id, 'Which rollout option would you prefer?', status)
    app.state.store.update_run(run_id, status='idle' if status == 'completed' else status)


def posts(requests):
    return [json.loads(r.content) for r in requests if r.url.path.endswith('/chat.postMessage')]


async def poll(app):
    await database(app.state.slack.notifications.collect)
    await app.state.slack.notifications.deliver_one()


async def test_web_owner_gets_one_dm_with_session_link_even_after_other_participant_replies(notifications):
    app, requests = notifications
    assert not app.state.store.rows('SELECT * FROM user_preferences')
    assert app.state.user_preferences.get('google:alice')['slack_notifications'] is True
    run_id, message_id = create(app)
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run_id,))
    finish(app, run_id, message_id)
    await asyncio.gather(poll(app), poll(app))
    assert len(posts(requests)) == 1
    sent = posts(requests)[0]
    assert sent['channel'] == 'D12345678'
    assert sent['blocks'][0]['text']['text'] == 'Response ready'
    assert sent['blocks'][3]['elements'][0]['url'].endswith('/#run=' + run_id)
    app.state.store.update_run(run_id, status='completed')
    await poll(app)
    assert len(posts(requests)) == 1
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'sent'
    app.state.store.enqueue_message(run_id, 'Use the first option', 'next', user_id='google:alice')
    message = app.state.store.claim_message(run_id)
    finish(app, run_id, message['id'])
    await poll(app)
    assert len(posts(requests)) == 2


async def test_no_historical_backfill_and_preference_is_personal(notifications):
    app, requests = notifications
    app.state.user_preferences.save('google:alice', {'slack_notifications': False})
    first, turn = create(app)
    finish(app, first, turn)
    app.state.user_preferences.save('google:alice', {'slack_notifications': True})
    app.state.user_preferences.save('google:bob', {'slack_notifications': False})
    other, turn = create(app, 'google:bob')
    finish(app, other, turn)
    await poll(app)
    assert not posts(requests)


@pytest.mark.parametrize('origin', ['thread', 'paused_thread', 'legacy_event'])
@pytest.mark.parametrize('state', ['ready', 'approval', 'failed'])
async def test_slack_conversations_do_not_get_extra_owner_dms(notifications, origin, state):
    app, requests = notifications
    run_id, turn = create(app)
    bind_slack(app, run_id, origin)
    if state == 'approval':
        app.state.store.execute("INSERT INTO approvals VALUES('approval1',?,'test','{}','pending',?,'')", (run_id, now()))
    else:
        finish(app, run_id, turn, 'completed' if state == 'ready' else state)
    await poll(app)
    assert not requests
    assert not app.state.store.rows('SELECT * FROM slack_notifications')


def bind_slack(app, run_id, origin):
    if origin == 'legacy_event':
        app.state.store.execute('''INSERT INTO slack_events
            (event_id,run_id,channel,thread_ts,user_id,created_at) VALUES(?,?,'C12345678','1.0','U12345678',?)''',
            ('event-' + run_id, run_id, now()))
    else:
        app.state.store.execute('''INSERT INTO slack_threads
            (team_id,channel,thread_ts,run_id,started_ts,paused) VALUES('T12345678','C12345678','1.0',?,'1.0',?)''',
            (run_id, int(origin == 'paused_thread')))


@pytest.mark.parametrize('origin', ['thread', 'legacy_event'])
async def test_slack_binding_added_during_delivery_suppresses_queued_dm(notifications, monkeypatch, origin):
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    original = app.state.connectors.request

    async def request(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if url.endswith('conversations.open'):
            bind_slack(app, run_id, origin)
        return result

    monkeypatch.setattr(app.state.connectors, 'request', request)
    await poll(app)
    assert not posts(requests)
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'skipped'


async def test_default_rollout_skips_old_events_but_restart_keeps_new_events(notifications):
    from app.slack_notifications import initialize_schema
    app, requests = notifications
    store = app.state.store
    old, turn = create(app)
    finish(app, old, turn)
    # Model migration of a pre-feature database that already contains an answer.
    store.execute('DROP TABLE slack_notification_state')
    initialize_schema(store)
    cutoff = store.rows('SELECT * FROM slack_notification_state')
    await poll(app)
    assert not requests
    current, turn = create(app)
    finish(app, current, turn)
    initialize_schema(store)  # A restart must not move the durable cutoff.
    assert store.rows('SELECT * FROM slack_notification_state') == cutoff
    await poll(app)
    assert len(posts(requests)) == 1
    assert posts(requests)[0]['blocks'][3]['elements'][0]['url'].endswith(current)


async def test_first_save_of_unrelated_preference_keeps_default_notifications(notifications):
    app, requests = notifications
    app.state.user_preferences.save('google:alice', {'send_immediately': True})
    run_id, turn = create(app)
    finish(app, run_id, turn)
    await poll(app)
    assert len(posts(requests)) == 1


async def test_explicit_opt_out_suppresses_new_web_events_and_survives_other_saves(notifications):
    app, requests = notifications
    app.state.user_preferences.save('google:alice', {'slack_notifications': False})
    app.state.user_preferences.save('google:alice', {'send_immediately': True})
    run_id, turn = create(app)
    finish(app, run_id, turn)
    await poll(app)
    assert not requests
    assert app.state.user_preferences.get('google:alice')['slack_notifications'] is False
    assert app.state.user_preferences.get('google:bob')['slack_notifications'] is True


@pytest.mark.parametrize('checked_at', [now(), '2000-01-01T00:00:00+00:00'])
async def test_linked_account_opt_out_never_falls_back_to_slack_default(notifications, checked_at):
    app, requests = notifications
    store = app.state.store
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute("""UPDATE users SET linked_user_id='google:alice',email='alice@berri.ai',
            profile_eligible=1,profile_checked_at=?,link_status='linked' WHERE id=?""", (checked_at, actor))
    app.state.user_preferences.save('google:alice', {'slack_notifications': False})
    run_id, turn = create(app, actor)
    finish(app, run_id, turn)
    await poll(app)
    assert not requests


async def test_later_linking_a_slack_owner_does_not_backfill_old_answers(notifications):
    app, requests = notifications
    store = app.state.store
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, 'T12345678', 'U12345678')
    old, turn = create(app, actor)
    finish(app, old, turn)
    app.state.user_preferences.save('google:alice', {'slack_notifications': False})
    app.state.user_preferences.save('google:alice', {'slack_notifications': True})
    store.execute("""UPDATE users SET linked_user_id='google:alice',email='alice@berri.ai',
        profile_eligible=1,profile_checked_at=?,link_status='linked' WHERE id=?""", (now(), actor))
    await poll(app)
    assert not posts(requests)
    current, turn = create(app, actor)
    finish(app, current, turn)
    await poll(app)
    assert len(posts(requests)) == 1


async def test_unsettled_answers_and_queued_work_do_not_ping_early(notifications):
    app, requests = notifications
    run_id, turn = create(app)
    with app.state.store.connect() as conn:
        app.state.store.save_answer_in(conn, run_id, turn, 'Partial answer', 'saving')
    app.state.store.update_run(run_id, status='saving')
    await poll(app)
    assert not posts(requests)
    finish(app, run_id, turn)
    app.state.store.enqueue_message(run_id, 'Continue', 'next', user_id='google:alice')
    await poll(app)
    assert not posts(requests)


@pytest.mark.parametrize('change', ['opt_out', 'resumed', 'deleted', 'policy', 'dm_off', 'allowlist', 'workspace', 'unlinked'])
async def test_stale_or_unauthorized_notifications_never_send(notifications, change):
    app, requests = notifications
    run_id, message_id = create(app)
    finish(app, run_id, message_id)
    app.state.slack.notifications.collect()
    if change == 'opt_out':
        app.state.user_preferences.save('google:alice', {'slack_notifications': False})
    elif change == 'resumed':
        app.state.store.enqueue_message(run_id, 'Continue', 'next', user_id='google:alice')
    elif change == 'deleted':
        app.state.store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), run_id))
    elif change == 'policy':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('slack',1)")
    elif change == 'dm_off':
        app.state.settings.slack_dm_enabled = False
    elif change == 'allowlist':
        app.state.settings.slack_session_users = 'U87654321'
    elif change == 'workspace':
        app.state.connectors.save('slack', {'bot': {'access_token': 'other', 'team': {'id': 'T87654321'}, 'scope': 'chat:write,im:write'}}, 'Other')
    else:
        app.state.settings.slack_identity_linking_enabled = False
    await poll(app)
    assert not posts(requests)


async def test_approval_resolved_before_delivery_is_skipped(notifications):
    app, requests = notifications
    run_id, _ = create(app)
    app.state.store.execute("INSERT INTO approvals VALUES('approval1',?,'test','{}','pending',?,'')", (run_id, now()))
    app.state.slack.notifications.collect()
    app.state.store.execute("UPDATE approvals SET status='denied' WHERE id='approval1'")
    await poll(app)
    assert not posts(requests)


@pytest.mark.parametrize('kind', ['approval', 'credential', 'failed', 'interrupted'])
async def test_attention_states_and_child_requests(notifications, kind):
    app, requests = notifications
    run_id, message_id = create(app)
    target = run_id
    if kind in {'approval', 'credential'}:
        target, child_turn = create(app)
        app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run_id, target))
        app.state.store.update_run(run_id, status='waiting_children')
        if kind == 'approval':
            app.state.store.execute("INSERT INTO approvals VALUES('approval1',?,'test','{}','pending',?,'')", (target, now()))
        else:
            app.state.store.execute("""INSERT INTO credential_requests
                (id,run_id,message_id,actor_id,provider,reason,request_key,created_at)
                VALUES('credential1',?,?,'google:alice','custom','Required for task','request1',?)""", (target, child_turn, now()))
    else:
        finish(app, run_id, message_id, kind)
    await poll(app)
    assert len(posts(requests)) == 1
    expected = {'approval': 'Approval needed', 'credential': 'Access needed'}.get(kind, 'Session needs attention')
    assert posts(requests)[0]['blocks'][0]['text']['text'] == expected
    assert posts(requests)[0]['blocks'][3]['elements'][0]['url'].endswith(target)
    await poll(app)
    assert len(posts(requests)) == 1


async def test_identity_or_preference_changes_during_dm_open_block_send(notifications, monkeypatch):
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    original = app.state.connectors.request

    async def request(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if url.endswith('conversations.open'):
            app.state.user_preferences.save('google:alice', {'slack_notifications': False})
            app.state.user_preferences.save('google:alice', {'slack_notifications': True})
        return result

    monkeypatch.setattr(app.state.connectors, 'request', request)
    await poll(app)
    assert not posts(requests)


async def test_ambiguous_post_is_never_replayed_and_preparation_can_retry(notifications, monkeypatch):
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    original = app.state.connectors.request
    fail_open = True

    async def request(method, url, **kwargs):
        nonlocal fail_open
        if url.endswith('conversations.open') and fail_open:
            fail_open = False
            raise TimeoutError()
        result = await original(method, url, **kwargs)
        if url.endswith('chat.postMessage'):
            raise TimeoutError('Response lost after Slack accepted message')
        return result

    monkeypatch.setattr(app.state.connectors, 'request', request)
    await poll(app)
    assert not posts(requests)
    app.state.store.execute('UPDATE slack_notifications SET retry_at=0')
    await poll(app)
    assert len(posts(requests)) == 1
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'uncertain'
    app.state.slack.notifications.recover()
    await poll(app)
    await app.state.slack.notifications.shutdown()
    assert len(posts(requests)) == 1


async def test_cancellation_during_confirmed_receipt_keeps_sent(notifications, monkeypatch):
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    entered, release = threading.Event(), threading.Event()
    original = app.state.store.execute

    def execute(sql, params=()):
        result = original(sql, params)
        if "SET status='sent',slack_ts=" in sql:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(app.state.store, 'execute', execute)
    task = asyncio.create_task(poll(app))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await poll(app)
    assert len(posts(requests)) == 1
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'sent'


async def test_graceful_shutdown_preserves_admitted_send_receipt(notifications, monkeypatch):
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.connectors.request

    async def request(method, url, **kwargs):
        if url.endswith('chat.postMessage'):
            entered.set()
            await release.wait()
        return await original(method, url, **kwargs)

    monkeypatch.setattr(app.state.connectors, 'request', request)
    worker = app.state.slack.notifications
    worker.recover()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        stopping = asyncio.create_task(worker.shutdown(graceful=True))
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
        await worker.shutdown(graceful=True)
    await stopping
    await poll(app)
    assert len(posts(requests)) == 1
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'sent'


async def test_collection_database_wait_does_not_block_loop(notifications):
    app, _ = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    ready, release, entered = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def hold_writer():
        try:
            with app.state.store.connect() as conn:
                conn.begin_write()
                conn.execute('UPDATE runs SET display_title=? WHERE id=?', ('Locked', run_id))
                ready.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)
        finally:
            ready.set()

    def collect():
        entered.set()
        return app.state.slack.notifications.collect()

    writer = threading.Thread(target=hold_writer)
    writer.start()
    task = None
    try:
        assert await asyncio.to_thread(ready.wait, 3)
        assert not errors
        task = asyncio.create_task(database(collect))
        assert await asyncio.to_thread(entered.wait, 3)
        # The collection waits on the real database writer while the loop stays usable.
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
        await asyncio.to_thread(writer.join, 3)
        if task is not None:
            await asyncio.wait_for(task, 3)
    assert not writer.is_alive() and not errors
    assert app.state.store.run(run_id)['display_title'] == 'Locked'


def test_personal_toggle_persists_without_overwriting_other_preferences(notifications):
    app, _ = notifications
    client = TestClient(app, base_url=app.state.settings.public_url)
    sign_in(app, client)
    assert client.put('/api/settings/preferences', json={'slack_notifications': False}).json()['slack_notifications'] is False
    assert client.put('/api/settings/preferences', json={'send_immediately': True}).status_code == 200
    saved = client.put('/api/settings/preferences', json={'slack_notifications': True})
    assert saved.status_code == 200
    assert saved.json()['slack_notifications'] is True and saved.json()['send_immediately'] is True
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/settings/preferences').json()['slack_notifications'] is True


def test_shared_login_cannot_enable_personal_notifications(notifications):
    app, _ = notifications
    actor = app.state.store.identity({'method': 'local', 'role': 'admin'})
    with pytest.raises(HTTPException, match='work account'):
        app.state.user_preferences.save(actor, {'slack_notifications': True})
    assert app.state.user_preferences.get(actor)['slack_notifications'] is False


def test_deletion_waits_for_inflight_notification_then_retires_pending(notifications):
    app, _ = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    app.state.slack.notifications.collect()
    app.state.store.execute("UPDATE slack_notifications SET status='sending' WHERE run_id=?", (run_id,))
    with pytest.raises(HTTPException) as exc:
        app.state.session_lifecycle.delete(run_id, 'google:alice', True)
    assert exc.value.status_code == 409
    app.state.store.execute("UPDATE slack_notifications SET status='pending' WHERE run_id=?", (run_id,))
    assert app.state.session_lifecycle.delete(run_id, 'google:alice', True)['deleted']
    assert app.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'skipped'
