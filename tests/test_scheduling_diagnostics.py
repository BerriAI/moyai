import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

import httpx
import pytest

from app import scheduling_diagnostics as diagnostics
from app.db import Store
from app.message_queue import MessageQueue
from app.security import digest
from scripts.scheduling_stall_demo import probe
from test_workspace import workspace  # noqa: F401


@pytest.mark.parametrize('steering', [False, True])
async def test_control_poll_does_not_block_event_loop_during_sqlite_write(tmp_path, steering):
    result = await probe(tmp_path, steering=steering, hold_seconds=0.7)
    assert result['http_status'] == 200
    assert result['correction_delivered'] is steering
    # The old endpoint stalls for the entire lock hold (~700 ms). Allow ample
    # scheduling jitter while still detecting a synchronous writer-lock wait.
    assert result['max_event_loop_lag_ms'] < 300
    if not steering:
        assert result['control_ms'] < 500


def test_poll_hint_rechecks_turn_before_locking_message(tmp_path, monkeypatch):
    store = Store(tmp_path)
    run = store.create_run('Private prompt', '', 'modal', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    store.update_run(run['id'], status='running')
    target, _ = store.enqueue_message(run['id'], 'Correction', 'correction', send_now=True)
    rows = store.rows
    def race(sql, params=(), **kwargs):
        result = rows(sql, params, **kwargs)
        if sql.startswith('SELECT 1 FROM runs r JOIN messages'):
            store.update_run(run['id'], status='stopping')
        return result
    monkeypatch.setattr(store, 'rows', race)
    assert MessageQueue(store).live_control(run['id'], first['id'], []) == {'steer_message_id': None}
    assert not rows('SELECT queue_locked FROM messages WHERE id=?', (target['id'],))[0]['queue_locked']


async def test_control_revalidates_capability_after_body_read(workspace):
    app, _ = workspace
    store = app.state.store
    run = store.create_run('Private prompt', '', 'modal', [], chat_enabled=True)
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('test-capability'))
    target, _ = store.enqueue_message(run['id'], 'Correction', 'correction', send_now=True)
    async def body():
        store.update_run(run['id'], token_hash='')
        yield b'{"version":2,"applied":[]}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 50000)),
                                base_url=app.state.settings.public_url) as client:
        response = await client.post(f"/broker/{run['id']}/control", content=body(),
                                     headers={'Authorization': 'Bearer test-capability'})
    assert response.status_code == 401
    assert not store.rows('SELECT queue_locked FROM messages WHERE id=?', (target['id'],))[0]['queue_locked']


async def test_database_stall_logs_locations_without_sql_or_values(tmp_path, caplog, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(diagnostics, '_database_warning_at', 0)
    caplog.set_level('INFO', logger=diagnostics.log.name)
    with store.connect() as conn:
        conn.execute("SELECT 'private-sql-value'")
        time.sleep(0.12)
    with store.connect():
        time.sleep(0.12)
    records = [json.loads(r.message) for r in caplog.records if r.name == diagnostics.log.name]
    assert len(records) == 1  # Repeated slow operations are rate limited.
    assert records[0]['on_event_loop'] and records[0]['duration_ms'] >= 100
    assert any(frame['file'] == 'test_scheduling_diagnostics.py' for frame in records[0]['stack'])
    assert 'private-sql-value' not in json.dumps(records)


async def test_event_loop_lag_is_reported_and_watcher_cancels(caplog):
    caplog.set_level('INFO', logger=diagnostics.log.name)
    watcher = asyncio.create_task(diagnostics.watch_event_loop(interval=0.01, threshold=0.04))
    await asyncio.sleep(0)
    time.sleep(0.1)
    await asyncio.sleep(0.03)
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    records = [json.loads(r.message) for r in caplog.records if r.name == diagnostics.log.name]
    assert any(r['event'] == 'event_loop_lag' and r['lag_ms'] >= 40 and r['process_cpu_ms'] >= 0 for r in records)


def test_claim_timing_includes_followups_and_omits_content(tmp_path, caplog):
    store = Store(tmp_path)
    run = store.create_run('private-first-message', '', 'modal', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    store.finish_message(run['id'], first['id'], 'private-answer')
    followup, _ = store.enqueue_message(run['id'], 'private-followup', 'followup')
    store.execute('UPDATE messages SET created_at=? WHERE id=?',
                  ((datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat(), followup['id']))
    caplog.set_level('INFO', logger=diagnostics.log.name)
    store.claim_message(run['id'])
    record = json.loads(next(r.message for r in caplog.records if r.name == diagnostics.log.name))
    assert record['message_id'] == followup['id'] and record['queue_wait_ms'] >= 12000
    assert 'private-' not in json.dumps(record)
