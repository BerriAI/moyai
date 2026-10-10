"""Real PostgreSQL ownership overlap and signed ingress; no live providers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import threading
import time
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest

from app import main, runtime_compatibility, schema_migrations
from app.coordinator_handoff import coordinator_owner
from app.database import DatabaseError
from app.db import database
from test_api_lifecycle import client_for
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401
from test_slack import event, signed, wait_for


@pytest.fixture
def handoff_settings(split_settings):
    settings = split_settings.model_copy(update={
        'moyai_schema_mode': 'verify', 'moyai_coordinator_handoff': True,
        'slack_signing_secret': 'slack-test-signing-secret'})
    schema_migrations.migrate(settings)
    return settings


def wait_leader(app):
    wait_for(lambda: app.state.coordinator_handoff.status == 'leader')


def policies(settings):
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        return [conn.execute('SELECT * FROM ' + table).fetchall()
                for table in ('runtime_policy', 'runtime_api_policy', 'runtime_coordinator_policy')]


def test_successor_is_ready_before_leader_exits_and_keeps_cluster_policy(handoff_settings, monkeypatch):
    first = main.create_app(handoff_settings)
    calls = []
    async def handle(payload, missing):
        calls.append(('first', payload['event_id']))
    monkeypatch.setattr(first.state.slack, 'receive_payload', handle)
    with ExitStack() as lifetime:
        old = lifetime.enter_context(client_for(first))
        wait_leader(first)
        before = policies(handoff_settings)
        second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
        async def handle_new(payload, missing):
            calls.append(('second', payload['event_id']))
        monkeypatch.setattr(second.state.slack, 'receive_payload', handle_new)
        with client_for(second) as new:
            assert new.get('/health').status_code == 200
            assert new.get('/health').headers['X-Moyai-Coordinator-State'] == 'standby'
            assert new.post('/api/runs', json={'prompt': 'Must use API'}).status_code == 503
            # A signed event sent only to the ready replacement is picked up by
            # the old owner. Render does not need a globally idle application.
            for i in range(5):
                assert new.post('/hooks/slack/events', **signed(event('EvBefore' + str(i)))).status_code == 200
            wait_for(lambda: len(calls) == 5)
            assert all(owner == 'first' for owner, _ in calls)
            assert second.state.manager.dispatch_task is None
            assert policies(handoff_settings) == before
            lifetime.close()
            wait_leader(second)
            for i in range(5):
                assert new.post('/hooks/slack/events', **signed(event('EvAfter' + str(i)))).status_code == 200
            wait_for(lambda: len(calls) == 10)
            assert all(owner == 'second' for owner, _ in calls[5:])
            assert len(set(key for _, key in calls)) == 10
            assert policies(handoff_settings) == before
            assert new.get('/health').headers['X-Moyai-Coordinator-State'] == 'leader'


def test_pending_ingress_survives_replacement_and_signature_expiry(handoff_settings, monkeypatch):
    # The constructor holds the first owner's lock, but intentionally does not
    # start background recovery. Standby can persist events while it is down.
    first = main.create_app(handoff_settings)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    calls = []
    async def processed(payload, missing):
        calls.append(payload)
    monkeypatch.setattr(second.state.slack, 'receive_payload', processed)
    try:
        with client_for(second) as client:
            assert client.post('/hooks/slack/events', json=event()).status_code == 401
            assert client.post('/hooks/slack/events', **signed(event(), int(time.time()) - 601)).status_code == 401
            payload = signed(event())
            with ThreadPoolExecutor(max_workers=4) as pool:
                assert list(pool.map(lambda _: client.post('/hooks/slack/events', **payload).status_code, range(8))) == [200] * 8
            assert second.state.store.rows('SELECT COUNT(*) AS count FROM coordinator_hooks')[0]['count'] == 1
            assert client.post('/hooks/slack/events', **signed({'type': 'url_verification', 'challenge': 'challenge'})).json() == {'challenge': 'challenge'}
            assert not calls
            # Consumption must use saved verified data, never the now-expired
            # HTTP signature and never an unsigned HTTP bypass.
            monkeypatch.setattr(second.state.slack, 'verify_signature', Mock(side_effect=AssertionError('Revalidated saved signature')))
            first.state.store.close()
            wait_leader(second)
            wait_for(lambda: len(calls) == 1)
            assert calls[0]['event_id'] == 'EvTest1'
            assert second.state.store.rows('SELECT payload,status FROM coordinator_hooks')[0] == {'payload': '', 'status': 'done'}
    finally:
        first.state.store.close()


def test_old_owner_retains_fence_until_shutdown_finishes(handoff_settings, monkeypatch):
    first = main.create_app(handoff_settings)
    started, release = threading.Event(), threading.Event()
    close = first.state.slack.shutdown
    async def slow_close(**kwargs):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        await close(**kwargs)
    monkeypatch.setattr(first.state.slack, 'shutdown', slow_close)
    old = client_for(first)
    old.__enter__()
    wait_leader(first)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    recover = second.state.slack.recover
    monkeypatch.setattr(second.state.slack, 'recover', Mock(wraps=recover))
    with client_for(second) as new, ThreadPoolExecutor(max_workers=1) as pool:
        shutdown = pool.submit(old.__exit__, None, None, None)
        try:
            assert started.wait(3)
            assert new.get('/health').headers['X-Moyai-Coordinator-State'] == 'standby'
            second.state.slack.recover.assert_not_called()
            assert new.post('/hooks/slack/events', **signed(event())).status_code == 200
        finally:
            release.set()
            shutdown.result(timeout=5)
        wait_leader(second)
        second.state.slack.recover.assert_called_once()


@pytest.mark.parametrize('cancellations,drain_failure', [(1, False), (3, False), (3, True)])
async def test_cancelled_lifespan_drains_admitted_send_before_handoff(handoff_settings, monkeypatch, cancellations, drain_failure):
    first = main.create_app(handoff_settings)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    first_stop, second_stop = asyncio.Event(), asyncio.Event()
    draining, finish_send, receipt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close = first.state.slack.shutdown
    monkeypatch.setattr(first.state.store, 'close', Mock(wraps=first.state.store.close))
    monkeypatch.setattr(second.state.slack, 'recover', Mock(wraps=second.state.slack.recover))

    async def shutdown(**kwargs):
        draining.set()
        await close(**kwargs)
        if drain_failure:
            raise RuntimeError('Cleanup failed after receipt')
    monkeypatch.setattr(first.state.slack, 'shutdown', shutdown)

    async def lifetime(app, stop):
        async with app.router.lifespan_context(app):
            await stop.wait()

    async def leader(app):
        async with asyncio.timeout(5):
            while app.state.coordinator_handoff.status != 'leader':
                await asyncio.sleep(.01)

    old = asyncio.create_task(lifetime(first, first_stop))
    new = asyncio.create_task(lifetime(second, second_stop))
    send = None
    try:
        await leader(first)
        store = first.state.store
        run = await database(store.create_run, 'Admitted Slack send', '', 'demo', [])
        await database(store.execute,
            "INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at,reply_status) "
            "VALUES('cancelled-handoff',?,'fixture-channel','1.1','fixture-user','2026-01-01','sending')", (run['id'],))

        async def admitted_send():
            # Stand in for an already admitted Slack request. Its normal job
            # shutdown must finish the response and saved receipt, not cancel it.
            await finish_send.wait()
            await database(store.execute,
                "UPDATE slack_events SET reply_status='sent' WHERE event_id='cancelled-handoff'")
            receipt.set()

        send = asyncio.create_task(admitted_send())
        first.state.slack.jobs.add(send)
        first_stop.set()
        await asyncio.wait_for(draining.wait(), 5)
        for _ in range(cancellations):
            old.cancel()
            await asyncio.sleep(.02)
            assert not old.done()
            assert first.state.coordinator_handoff.task.cancelling() == 0
            assert not send.done()
            assert first.state.store.database.coordinator_owner
            first.state.store.close.assert_not_called()
            assert second.state.coordinator_handoff.status == 'standby'
            second.state.slack.recover.assert_not_called()
        finish_send.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await old
        if drain_failure:
            assert isinstance(error.value.__cause__, RuntimeError)
            assert str(error.value.__cause__) == 'Cleanup failed after receipt'
        assert receipt.is_set() and not send.cancelled()
        first.state.store.close.assert_called_once()
        await leader(second)
        second.state.slack.recover.assert_called_once()
        assert await database(second.state.store.rows,
            "SELECT reply_status FROM slack_events WHERE event_id='cancelled-handoff'") == [{'reply_status': 'sent'}]
    finally:
        finish_send.set()
        first_stop.set()
        second_stop.set()
        await asyncio.gather(old, new, return_exceptions=True)
        if send:
            await asyncio.gather(send, return_exceptions=True)


@pytest.mark.parametrize('response', [{'ok': True}, {'response_action': 'errors', 'errors': {'score': 'Please select a score'}}])
def test_interactions_are_synchronous_and_deduped_across_replicas(handoff_settings, monkeypatch, response):
    first = main.create_app(handoff_settings)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    calls, entered, release = [], threading.Event(), threading.Event()
    async def action(payload):
        calls.append(payload)
        entered.set()
        await asyncio.to_thread(release.wait, 3)
        return response
    monkeypatch.setattr(first.state.slack.feedback, 'receive_payload', action)
    monkeypatch.setattr(second.state.slack.feedback, 'receive_payload', action)
    with client_for(first) as old, client_for(second) as new, ThreadPoolExecutor(max_workers=1) as pool:
        payload = signed({'type': 'block_actions', 'trigger_id': 'single-use'}, form=True)
        request = pool.submit(old.post, '/hooks/slack/interactions', **payload)
        try:
            assert entered.wait(3)
            duplicate = new.post('/hooks/slack/interactions', **payload)
            assert duplicate.status_code == 409
            assert not request.done()
        finally:
            release.set()
        assert request.result(timeout=5).json() == response
        assert new.post('/hooks/slack/interactions', **payload).json() == response
        assert len(calls) == 1


def test_ambiguous_interaction_is_not_replayed(handoff_settings, monkeypatch):
    app = main.create_app(handoff_settings)
    action = AsyncMock(side_effect=RuntimeError('External send response lost'))
    monkeypatch.setattr(app.state.slack.feedback, 'receive_payload', action)
    with client_for(app) as client:
        payload = signed({'type': 'block_actions', 'trigger_id': 'single-use'}, form=True)
        with pytest.raises(RuntimeError, match='External send'):
            client.post('/hooks/slack/interactions', **payload)
        assert client.post('/hooks/slack/interactions', **payload).status_code == 409
        action.assert_awaited_once()
        assert app.state.store.rows('SELECT status FROM coordinator_hooks')[0]['status'] == 'uncertain'


def test_live_coordinator_contract_revocation_fences_ingress(handoff_settings):
    app = main.create_app(handoff_settings)
    with pytest.raises(DatabaseError, match='Coordinator compatibility'):
        with client_for(app) as client:
            wait_leader(app)
            with psycopg.connect(handoff_settings.moyai_database_url) as conn:
                conn.execute(f'DELETE FROM "{handoff_settings.moyai_database_schema}".runtime_coordinator_policy')
            assert client.get('/health').status_code == 503
            with pytest.raises(DatabaseError, match='Coordinator compatibility'):
                app.state.store.rows('SELECT 1')
    

def test_incompatible_successor_cannot_run_components(handoff_settings, monkeypatch):
    first = main.create_app(handoff_settings)
    try:
        before = policies(handoff_settings)
        monkeypatch.setattr(runtime_compatibility, 'COORDINATOR_PROTOCOL_REVISION', 999)
        monkeypatch.setattr(main, '_create_app', Mock(side_effect=AssertionError('Constructed incompatible candidate')))
        with pytest.raises(DatabaseError, match='Another coordinator'):
            main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
        assert policies(handoff_settings) == before
    finally:
        first.state.store.close()


def test_leader_context_fences_standby_transactions(handoff_settings):
    first = main.create_app(handoff_settings)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    token = coordinator_owner.set((second.state.store.database.schema, second.state.store.database.owner_pid))
    try:
        with pytest.raises(DatabaseError, match='Coordinator ownership'):
            second.state.store.rows('SELECT 1')
    finally:
        coordinator_owner.reset(token)
        second.state.store.close()
        first.state.store.close()


async def test_cancelled_database_failure_remains_cancellation():
    from app.db import database
    entered, release = threading.Event(), threading.Event()
    def operation():
        entered.set()
        assert release.wait(5)
        raise DatabaseError('Owner revoked during shutdown')
    task = asyncio.create_task(database(operation))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert isinstance(error.value.__cause__, DatabaseError)


def test_inbox_bounds_and_failed_event_does_not_block_followups(handoff_settings, monkeypatch):
    from app import coordinator_handoff
    first = main.create_app(handoff_settings)
    second = main.create_app(handoff_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    monkeypatch.setattr(coordinator_handoff, 'MAX_PENDING_EVENTS', 2)
    calls = []
    async def process(payload, missing):
        if payload['event_id'] == 'EvFail':
            raise RuntimeError('Temporary handler failure')
        calls.append(payload['event_id'])
    monkeypatch.setattr(second.state.slack, 'receive_payload', process)
    try:
        with client_for(second) as client:
            assert client.post('/hooks/slack/events', content=b'x' * (262144 + 1)).status_code == 413
            assert client.post('/hooks/slack/events', **signed(event('EvFail'))).status_code == 200
            assert client.post('/hooks/slack/events', **signed(event('EvGood'))).status_code == 200
            assert client.post('/hooks/slack/events', **signed(event('EvFull'))).status_code == 503
            assert client.post('/hooks/slack/events', **signed(event('EvGood'))).status_code == 200
            first.state.store.close()
            wait_leader(second)
            wait_for(lambda: calls == ['EvGood'])
            assert second.state.store.rows("SELECT status FROM coordinator_hooks WHERE id='event:T12345678:EvFail'")[0]['status'] == 'pending'
    finally:
        first.state.store.close()
