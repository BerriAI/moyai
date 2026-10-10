"""Real Slack polling contention and ownership at the new thread boundaries."""
import asyncio
import json
import threading

import pytest

from app.db import database
from scripts.slack_polling_probe import probe
from test_slack import slack_app  # noqa: F401
from test_slack_chat import start


@pytest.mark.parametrize('lane', ['both', 'text', 'media', 'activity'])
@pytest.mark.parametrize('contention', ['writer', 'pool'])
async def test_slack_database_wait_keeps_request_loop_responsive(tmp_path, request, lane, contention):
    postgres = request.config.getoption('--postgres-backend')
    if contention == 'pool' and not postgres:
        pytest.skip('Pool contention requires --postgres-backend.')
    result = await probe(tmp_path, hold_seconds=.7, contention=contention, lane=lane)
    assert result['backend'] == ('postgresql' if postgres else 'sqlite')
    assert result['elapsed_ms'] >= 650
    assert result['max_event_loop_lag_ms'] < 300
    assert result['health_requests'] > 0 and result['health_ok']
    if contention == 'writer':
        assert result['health_requests_during_wait'] > 0
        assert result['max_health_ms'] < 300
    else:
        # A health check needs a DB connection, but waiting for that connection
        # must still let other async requests, timers and heartbeats run.
        assert result['max_health_ms'] >= 500
    assert result['sent_answers'] == (0 if lane == 'activity' else 1)
    assert result['uncertain_answers'] == 0
    assert result['external_provider_calls'] == 0


def pending_answer(slack_app):
    app, client, _, _ = slack_app
    client.portal.call(app.state.slack.chat.shutdown)
    _, _, run_id = start(slack_app)
    store = app.state.store
    message = store.claim_message(run_id)
    store.finish_message(run_id, message['id'], 'One durable answer.')
    store.update_run(run_id, status='idle')
    return app, client, run_id


@pytest.mark.parametrize('failed', [False, True])
def test_shutdown_drains_collection_before_returning(slack_app, monkeypatch, failed):
    app, client, run_id = pending_answer(slack_app)
    chat, store = app.state.slack.chat, app.state.store
    entered, release = threading.Event(), threading.Event()
    collect_answers = chat.collect_answers_in

    def blocked_collection(conn, binding, allowed):
        entered.set()
        assert release.wait(5)
        if failed:
            raise RuntimeError('Collection failed during shutdown')
        return collect_answers(conn, binding, allowed)

    monkeypatch.setattr(chat, 'collect_answers_in', blocked_collection)

    async def scenario():
        chat.watcher = asyncio.create_task(chat.watch())
        shutdown = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            shutdown = asyncio.create_task(chat.shutdown())
            await asyncio.sleep(.05)
            assert not shutdown.done()  # The transaction still owns its connection.
        finally:
            release.set()
            if shutdown:
                await asyncio.wait_for(shutdown, 2)
            else:
                await chat.shutdown()
        assert chat.watcher.cancelled()
        rows = await database(store.rows, "SELECT status FROM slack_outbox WHERE run_id=? AND kind='answer'", (run_id,))
        assert rows == ([] if failed else [{'status': 'pending'}])
        monkeypatch.setattr(chat, 'collect_answers_in', collect_answers)
        await database(chat.collect)
        await chat.deliver_one()
        rows = await database(store.rows, "SELECT status FROM slack_outbox WHERE run_id=? AND kind='answer'", (run_id,))
        assert rows == [{'status': 'sent'}]

    client.portal.call(scenario)
    assert sum(item.get('text', '').startswith('One durable answer.') for item in slack_app[3]) == 1


@pytest.mark.parametrize('kind', ['answer', 'ack', 'reaction', 'credential', 'updated_credential'])
@pytest.mark.parametrize('outcome', ['sent', 'uncertain'])
def test_cancellation_drains_delivery_receipts_without_changing_outcome(slack_app, monkeypatch, kind, outcome):
    app, client, run_id = pending_answer(slack_app)
    chat, store = app.state.slack.chat, app.state.store
    metadata = {'credential_request_id': 'local-test'} if 'credential' in kind else {}
    with store.connect() as conn:
        chat.queue(conn, run_id, 'cancellation-receipt', 'answer' if metadata else kind,
                   '1790719000.123456' if kind == 'reaction' else 'One durable answer.', metadata)
    row = store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='cancellation-receipt'")[0]
    store.execute('INSERT INTO slack_activity(run_id,status,refreshed_at,retry_at) VALUES(?,?,123,456)',
                  (run_id, 'is working…'))
    attempts = []
    original_request = app.state.connectors.request

    async def request(*args, **kwargs):
        attempts.append(1)
        if outcome == 'uncertain':
            raise ConnectionError('Slack response lost')
        return await original_request(*args, **kwargs)

    async def credential_card(source, outbox_id):
        attempts.append(1)
        if outcome == 'uncertain':
            raise ConnectionError('Slack response lost')
        if kind == 'updated_credential':
            await database(store.execute, 'UPDATE slack_outbox SET metadata=? WHERE id=?',
                           (json.dumps({**metadata, 'revision': 2}), outbox_id))
        return '1790719999.123456', row['metadata']

    monkeypatch.setattr(app.state.connectors, 'request', request)
    monkeypatch.setattr(app.state.slack.channel, 'credential_card', credential_card)
    entered, release = threading.Event(), threading.Event()
    execute = store.execute

    def blocked_receipt(sql, *args, **kwargs):
        result = execute(sql, *args, **kwargs)
        if (sql.startswith('UPDATE slack_outbox SET status=') and 'WHERE id=?' in sql and f"'{outcome}'" in sql
                and not entered.is_set()):
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(store, 'execute', blocked_receipt)

    async def scenario():
        sender = asyncio.create_task(chat.deliver_one())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            sender.cancel()
            await asyncio.sleep(.05)
            assert not sender.done() and row['id'] in chat.delivering
        finally:
            release.set()
            results = await asyncio.gather(sender, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert not chat.delivering
        saved = (await database(store.rows, 'SELECT status,slack_ts FROM slack_outbox WHERE id=?', (row['id'],)))[0]
        expected = 'pending' if outcome == 'sent' and kind == 'updated_credential' else outcome
        assert saved['status'] == expected
        if outcome == 'sent':
            assert saved['slack_ts']
        if kind in {'ack', 'reaction'}:
            assert await database(store.rows, 'SELECT reply_status FROM slack_events WHERE run_id=?', (run_id,)) == [
                {'reply_status': outcome}]
        notices = await database(store.rows, "SELECT message FROM events WHERE run_id=? AND message LIKE '%could not be confirmed%'", (run_id,))
        assert len(notices) == (1 if outcome == 'uncertain' else 0)
        if outcome == 'sent' and kind != 'reaction':
            assert await database(store.rows, 'SELECT refreshed_at,retry_at FROM slack_activity WHERE run_id=?', (run_id,)) == [
                {'refreshed_at': 0, 'retry_at': 0}]
        if expected != 'pending':
            chat.last_post.clear()
            await chat.deliver_one()
            assert len(attempts) == 1  # A later poll cannot replay this delivery.

    client.portal.call(scenario)


@pytest.mark.parametrize('cancel', [False, True])
def test_concurrent_lanes_preserve_claim_ownership_during_database_yield(slack_app, monkeypatch, cancel):
    app, client, run_id = pending_answer(slack_app)
    chat, store = app.state.slack.chat, app.state.store
    chat.collect()
    entered, release = threading.Event(), threading.Event()
    execute = store.execute

    def blocked_claim(sql, *args, **kwargs):
        result = execute(sql, *args, **kwargs)
        if sql.startswith("UPDATE slack_outbox SET status='sending'") and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(store, 'execute', blocked_claim)

    async def scenario():
        sender = asyncio.create_task(chat.deliver_one())
        competitor = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            competitor = asyncio.create_task(chat.deliver_one())
            if cancel:
                sender.cancel()
            await asyncio.sleep(.05)
            assert not sender.done() and not competitor.done()
            assert chat.delivering
        finally:
            release.set()
            results = await asyncio.gather(sender, *([competitor] if competitor else []), return_exceptions=True)
        if cancel:
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] is None
        else:
            assert results == [None, None]
        assert not chat.delivering
        rows = await database(store.rows, "SELECT status FROM slack_outbox WHERE run_id=? AND kind='answer'", (run_id,))
        assert rows == [{'status': 'sent'}]

    client.portal.call(scenario)
    assert sum(item.get('text', '').startswith('One durable answer.') for item in slack_app[3]) == 1
