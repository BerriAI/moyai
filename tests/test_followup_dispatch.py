"""A follow-up's durable wake must not freeze the API while its writer waits."""
import asyncio
import threading

import httpx

from app.config import Settings
from app.main import create_app


async def test_followup_dispatch_writer_wait_keeps_health_responsive(tmp_path, monkeypatch):
    # Exercise the real Temporal manager's database wake without starting a
    # Temporal client, execution worker, model request, or sandbox.
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, public_url='http://127.0.0.1:8787', temporal_enabled=True,
                  auto_prepare_repositories=False, session_titles_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    store, manager = app.state.store, app.state.manager
    run = store.create_run('Follow-up dispatch contention', '', 'demo', [], chat_enabled=True)
    submit = manager.submit
    begin, locked, release, released = (threading.Event() for _ in range(4))
    errors = []

    def hold_writer():
        try:
            assert begin.wait(5), 'Follow-up did not reach durable dispatch'
            with store.connect() as conn:
                conn.begin_write()
                locked.set()
                release.wait(5)  # Bound failure when the old code blocks the loop.
        except BaseException as exc:
            errors.append(exc)
            locked.set()
        finally:
            released.set()

    def contended_submit(row):
        begin.set()
        assert locked.wait(5), 'Test writer did not acquire the lock'
        return submit(row)

    monkeypatch.setattr(manager, 'submit', contended_submit)
    holder = threading.Thread(target=hold_writer)
    holder.start()
    sending = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=values['public_url']) as client:
            session = (await client.get('/api/session')).json()
            client.headers.update({'Origin': values['public_url'], 'X-CSRF-Token': session['csrf']})
            path = f"/api/runs/{run['id']}/messages"
            payload = {'content': 'One follow-up saved through contention', 'client_id': 'followup-dispatch-1'}
            sending = asyncio.create_task(client.post(path, json=payload))
            assert await asyncio.to_thread(locked.wait, 5)
            assert not errors
            health = await asyncio.wait_for(client.get('/health'), 2)
            assert health.status_code == 200
            assert not released.is_set(), 'Health must finish while dispatch still waits for the writer'
            assert not sending.done(), 'The follow-up must wait for its durable wake to commit'
            release.set()
            response = await asyncio.wait_for(sending, 5)
            assert response.status_code == 202, response.text
            assert response.json()['created'] is True
            assert store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run['id'],)) == [{'revision': 1}]

            # A lost acknowledgement can retry with the same id without adding
            # another input; it still durably wakes the previously saved input.
            retried = await client.post(path, json=payload)
            assert retried.status_code == 202, retried.text
            assert retried.json()['id'] == response.json()['id']
            assert retried.json()['created'] is False
            messages = store.rows('SELECT id FROM messages WHERE run_id=? AND client_id=?',
                                  (run['id'], payload['client_id']))
            assert messages == [{'id': response.json()['id']}]
            assert store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run['id'],)) == [{'revision': 2}]
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 6)
        if sending:
            await asyncio.gather(sending, return_exceptions=True)
        await asyncio.to_thread(store.close)
    assert not holder.is_alive()
    assert not errors
