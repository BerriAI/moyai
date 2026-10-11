"""Durable lifecycle capture, bounded consumption and real worker wakeups."""
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import now
from app.session_events import BATCH_SIZE, SessionEvents
from test_slack_notifications import notifications, create, finish, posts


def consume(store, consumer='slack_notifications', handle=lambda run, event: None):
    with store.connect() as conn:
        conn.begin_write()
        return store.session_events.consume_in(conn, consumer, handle)


def inbox(store, consumer='slack_notifications'):
    return store.rows('SELECT * FROM session_event_inbox WHERE consumer=? ORDER BY id', (consumer,))


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_capture_and_local_wakeup_share_commit_and_rollback(notifications):
    app, _ = notifications
    store = app.state.store
    run_id, _ = create(app)
    consume(store)
    wake = asyncio.Event()
    async with store.session_events.wake_on_commit(wake):
        await store.session_events.listening.wait()
        await asyncio.sleep(0.02)
        wake.clear()
        with pytest.raises(ValueError), store.connect() as conn:
            conn.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
            assert conn.execute('SELECT COUNT(*) FROM session_event_inbox').fetchone()[0] == 1
            await asyncio.sleep(0.02)
            assert not wake.is_set()
            raise ValueError('rollback')
        await asyncio.sleep(0.02)
        assert not wake.is_set() and not inbox(store)
        store.update_run(run_id, status='failed')
        await asyncio.wait_for(wake.wait(), 2)
        assert len(inbox(store)) == 1
    assert not store.commit_listeners


def test_coalesced_bounded_batches_and_independent_consumers(notifications, monkeypatch):
    app, _ = notifications
    store = app.state.store
    first, turn = create(app)
    with store.connect() as conn:
        SessionEvents.subscribe_in(conn, 'audit')
        template = dict(conn.execute('SELECT * FROM runs WHERE id=?', (first,)).fetchone())
        columns = ','.join(template)
        placeholders = ','.join('?' for _ in template)
        for index in range(BATCH_SIZE + 2):
            template['id'] = f'batch-{index:04}'
            conn.execute(f'INSERT INTO runs({columns}) VALUES({placeholders})', tuple(template.values()))
    finish(app, first, turn)
    assert len(inbox(store)) == BATCH_SIZE + 3
    resolved = []
    original = store.session_events.attention_in
    def attention(conn, run):
        resolved.append(run['id'])
        return original(conn, run)
    monkeypatch.setattr(store.session_events, 'attention_in', attention)
    assert consume(store) == BATCH_SIZE
    assert len(set(resolved)) == BATCH_SIZE
    assert len(inbox(store, 'audit')) == BATCH_SIZE + 3
    # A noisy session goes behind older work, instead of starving it by ID.
    store.update_run(first, status='completed')
    assert consume(store) == 4
    resolved.clear()
    assert consume(store) == 0
    assert not resolved
    with store.connect() as conn:
        SessionEvents.subscribe_in(conn, 'slack_notifications')
    assert not inbox(store)  # Restarts never snapshot historical roots again.


def test_enqueue_and_ack_rollback_together(notifications, monkeypatch):
    app, _ = notifications
    store, worker = app.state.store, app.state.slack.notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    original = worker.queue_in
    def fail(*args):
        original(*args)
        raise ValueError('failed after queueing')
    monkeypatch.setattr(worker, 'queue_in', fail)
    with pytest.raises(ValueError):
        worker.collect()
    assert len(inbox(store)) == 1
    assert not store.rows('SELECT * FROM slack_notifications')
    monkeypatch.setattr(worker, 'queue_in', original)
    worker.collect()
    assert not inbox(store)
    assert len(store.rows('SELECT * FROM slack_notifications')) == 1


@pytest.mark.parametrize('kind', ['approval', 'credential'])
def test_child_request_changes_resolve_only_the_root(notifications, monkeypatch, kind):
    app, _ = notifications
    store = app.state.store
    root, _ = create(app)
    child, turn = create(app)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root, child))
    store.update_run(root, status='waiting_children')
    consume(store)
    if kind == 'approval':
        store.execute("INSERT INTO approvals VALUES('request',?,'test','{}','pending',?,'')", (child, now()))
    else:
        store.execute("""INSERT INTO credential_requests
            (id,run_id,message_id,actor_id,provider,reason,request_key,created_at)
            VALUES('request',?,?,'google:alice','custom','Needed','key',?)""", (child, turn, now()))
    seen = []
    consume(store, handle=lambda run, event: seen.append((run['id'], event)))
    assert len(seen) == 1 and seen[0][0] == root
    assert seen[0][1].target_run_id == child
    if kind == 'credential':
        store.execute("UPDATE credential_requests SET generation=generation+1 WHERE id='request'")
        consume(store, handle=lambda run, event: seen.append((run['id'], event)))
        assert seen[0][1].key != seen[1][1].key
    table = 'approvals' if kind == 'approval' else 'credential_requests'
    store.execute(f"DELETE FROM {table} WHERE id='request'")
    assert consume(store, handle=lambda *args: pytest.fail('Resolved request emitted attention')) == 1


async def test_worker_wakes_without_periodic_poll_and_replays_stopped_events(notifications, monkeypatch):
    app, requests = notifications
    worker = app.state.slack.notifications
    monkeypatch.setattr('app.slack_notifications.RECONCILE_SECONDS', 3600)
    worker.recover()
    try:
        await until(lambda: app.state.store.session_events.listening.is_set())
        await until(lambda: not inbox(app.state.store))
        first, turn = create(app)
        finish(app, first, turn)
        await until(lambda: len(posts(requests)) == 1)
        await worker.shutdown(graceful=True)
        second, turn = create(app)
        finish(app, second, turn)
        assert len(inbox(app.state.store)) == 1
        worker.recover()
        await until(lambda: len(posts(requests)) == 2)
        await until(lambda: not inbox(app.state.store))
    finally:
        await worker.shutdown(graceful=True)
    worker.recover()
    try:
        await asyncio.sleep(0.15)
        assert len(posts(requests)) == 2
    finally:
        await worker.shutdown(graceful=True)


async def test_postgres_external_commit_wakes_and_rollback_stays_quiet(notifications):
    app, _ = notifications
    store = app.state.store
    if not store.database:
        pytest.skip('PostgreSQL LISTEN/NOTIFY contract')
    import psycopg
    from psycopg import sql
    run_id, _ = create(app)
    consume(store)
    wake = asyncio.Event()
    async with store.session_events.wake_on_commit(wake):
        await asyncio.wait_for(store.session_events.listening.wait(), 3)
        await asyncio.sleep(0.02)
        wake.clear()
        async with await psycopg.AsyncConnection.connect(store.database.pool.conninfo) as conn:
            await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(store.database.schema)))
            await conn.execute("UPDATE runs SET status='failed' WHERE id=%s", (run_id,))
            await conn.rollback()
            await asyncio.sleep(0.1)
            assert not wake.is_set() and not inbox(store)
            await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(store.database.schema)))
            await conn.execute("UPDATE runs SET status='failed' WHERE id=%s", (run_id,))
            await conn.commit()
            await asyncio.wait_for(wake.wait(), 3)
            assert len(inbox(store)) == 1


def test_postgres_external_producer_is_not_lost_during_ack(notifications):
    app, _ = notifications
    store = app.state.store
    if not store.database:
        pytest.skip('External PostgreSQL transaction race')
    import psycopg
    from psycopg import sql
    run_id, turn = create(app)
    finish(app, run_id, turn)
    entered = threading.Event()
    def write():
        with psycopg.connect(store.database.pool.conninfo) as conn:
            conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(store.database.schema)))
            entered.set()
            conn.execute("UPDATE runs SET status='failed' WHERE id=%s", (run_id,))
    with ThreadPoolExecutor(1) as pool:
        pending = []
        def handle(run, event):
            pending.append(pool.submit(write))
            assert entered.wait(3)
            # The producer must wait until acknowledgment commits, then enqueue.
            with pytest.raises(TimeoutError):
                pending[0].result(timeout=0.05)
        consume(store, handle=handle)
        pending[0].result(timeout=3)
    seen = []
    consume(store, handle=lambda run, event: seen.append(event.kind))
    assert seen == ['session.failed']


async def test_listener_reconnect_replays_missed_wakeup(notifications):
    app, _ = notifications
    store = app.state.store
    if not store.database:
        pytest.skip('PostgreSQL listener reconnect contract')
    import psycopg
    from psycopg import sql
    run_id, _ = create(app)
    consume(store)
    wake = asyncio.Event()
    async with store.session_events.wake_on_commit(wake):
        await asyncio.wait_for(store.session_events.listening.wait(), 3)
        async with await psycopg.AsyncConnection.connect(store.database.pool.conninfo, autocommit=True) as conn:
            # Match only this test's dedicated LISTEN connection.
            channel = 'moyai_session_events_' + hashlib.md5(store.database.schema.encode()).hexdigest()
            pids = await (await conn.execute('SELECT pid FROM pg_stat_activity WHERE query LIKE %s', ('LISTEN "' + channel + '"',))).fetchall()
            assert len(pids) == 1
            await conn.execute('SELECT pg_terminate_backend(%s)', (pids[0][0],))
            await until(lambda: not store.session_events.listening.is_set())
            wake.clear()
            await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(store.database.schema)))
            await conn.execute("UPDATE runs SET status='failed' WHERE id=%s", (run_id,))
            await asyncio.wait_for(store.session_events.listening.wait(), 4)
            assert wake.is_set()
            assert consume(store) == 1


async def test_process_restart_replays_pending_lifecycle_change_once(notifications):
    from app.main import create_app
    app, requests = notifications
    run_id, turn = create(app)
    finish(app, run_id, turn)
    app.state.store.close()
    restarted = create_app(app.state.settings)
    try:
        worker = restarted.state.slack.notifications
        worker.recover()
        await until(lambda: len(posts(requests)) == 1)
        await worker.shutdown(graceful=True)
        assert not inbox(restarted.state.store)
        assert restarted.state.store.rows('SELECT status FROM slack_notifications')[0]['status'] == 'sent'
    finally:
        await restarted.state.slack.notifications.shutdown()
        restarted.state.store.close()


def test_upgrade_snapshots_pending_events_once(notifications):
    from app.database_schema import SESSION_EVENT_TRIGGERS
    from app.session_events import initialize_schema
    from app.slack_notifications import initialize_schema as initialize_slack
    app, _ = notifications
    store = app.state.store
    # Model the previous release: notification receipts/cutoff, no shared inbox.
    with store.connect() as conn:
        for name in SESSION_EVENT_TRIGGERS:
            suffix = ' ON ' + name.removeprefix('session_event_').rsplit('_', 1)[0] if store.database else ''
            conn.execute('DROP TRIGGER ' + name + suffix)
        conn.execute('DROP TABLE session_event_inbox')
        conn.execute('DROP TABLE session_event_consumers')
    run_id, turn = create(app)
    finish(app, run_id, turn)
    initialize_schema(store)
    initialize_slack(store)
    assert len(inbox(store)) == 1
    app.state.slack.notifications.collect()
    assert len(store.rows('SELECT * FROM slack_notifications')) == 1
    initialize_slack(store)
    assert not inbox(store)
