import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from app.db import now
from app.session_maintenance import SessionMaintenance
from test_cloudflare_login import login  # noqa: F401
from test_cloudflare_access import access_key  # noqa: F401


async def drain(maintenance):
    if maintenance.worker:
        await maintenance.worker


@contextmanager
def held_schema_writer(store):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            # Competing reads must not inherit the writer's connection scope.
            with store.connect() as writer:
                writer.begin_write()
                ready.set()
                assert release.wait(10), 'The test did not release its schema writer.'
        except BaseException as exc:
            errors.append(exc)
        finally:
            ready.set()

    holder = threading.Thread(target=hold, name='session-bootstrap-test-writer')
    holder.start()
    try:
        assert ready.wait(5), 'Schema writer did not acquire its lock.'
        assert not errors, errors
        yield
    finally:
        release.set()
        holder.join(5)
        assert not holder.is_alive(), 'Schema writer did not release its connection.'
        assert not errors, errors


def test_conversation_bootstrap_does_not_wait_for_writer_but_preserves_accounting(login, monkeypatch):
    app, client = login
    store, maintenance = app.state.store, app.state.session_maintenance
    first = client.get('/api/session').json()
    client.portal.call(drain, maintenance)
    actor = first['user_id']
    with store.connect() as conn:
        slack_id = store.slack_identity_in(conn, 'TTEST', 'UTEST')
    store.execute('''UPDATE users SET email='tin@berri.ai',profile_eligible=1,
        profile_checked_at=? WHERE id=?''', (now(), slack_id))
    old = '2000-01-01T00:00:00+00:00'
    store.execute('UPDATE users SET updated_at=? WHERE id=?', (old, actor))
    started = threading.Event()
    touch = store.touch_access_identity

    def observed_touch(*args):
        started.set()
        return touch(*args)

    monkeypatch.setattr(store, 'touch_access_identity', observed_touch)
    # Real PostgreSQL exclusive advisory writer lock / SQLite WAL writer lock.
    with ThreadPoolExecutor(max_workers=1) as requests, held_schema_writer(store):
        response = requests.submit(client.get, '/api/session').result(timeout=2)
        assert response.status_code == 200
        assert response.json() == first
        assert started.wait(2)
        assert store.rows('SELECT updated_at FROM users WHERE id=?', (actor,))[0]['updated_at'] == old
        assert store.rows('SELECT linked_user_id FROM users WHERE id=?', (slack_id,))[0]['linked_user_id'] is None
    client.portal.call(drain, maintenance)
    assert store.rows('SELECT updated_at FROM users WHERE id=?', (actor,))[0]['updated_at'] > old
    assert store.rows('SELECT linked_user_id FROM users WHERE id=?', (slack_id,))[0]['linked_user_id'] == actor


def test_conversation_bootstrap_checks_current_role_once_and_does_not_reenroll(login, monkeypatch):
    app, client = login
    assert client.get('/api/session').json()['role'] == 'admin'
    client.portal.call(drain, app.state.session_maintenance)
    app.state.store.execute("INSERT INTO user_roles VALUES('tin@berri.ai','member',1,'test',?)", (now(),))
    role = app.state.user_roles.role
    calls = []

    def current_role(email):
        calls.append(email)
        return role(email)

    def unexpected_enrollment(*args):
        raise AssertionError('Enrolled bootstrap must not enroll or synchronously maintain identity')

    monkeypatch.setattr(app.state.user_roles, 'role', current_role)
    monkeypatch.setattr(app.state.store, 'identity', unexpected_enrollment)
    monkeypatch.setattr(app.state.store, 'access_identity', unexpected_enrollment)
    assert client.get('/api/session').json()['role'] == 'member'
    assert calls == ['tin@berri.ai']


def test_first_conversation_bootstrap_persists_enrollment_before_authenticating(login, monkeypatch):
    app, client = login
    store = app.state.store
    started = threading.Event()
    enroll = store.access_identity

    def observed_enrollment(identity):
        started.set()
        return enroll(identity)

    monkeypatch.setattr(store, 'access_identity', observed_enrollment)
    with ThreadPoolExecutor(max_workers=1) as requests:
        with held_schema_writer(store):
            pending = requests.submit(client.get, '/api/session')
            assert started.wait(2)
            assert not pending.done()
            assert not store.rows('SELECT * FROM access_identities')
        response = pending.result(timeout=2)
    assert response.status_code == 200
    assert response.json()['authenticated'] is True
    assert store.rows('SELECT user_id FROM access_identities')[0]['user_id'] == response.json()['user_id']


@pytest.mark.parametrize('change', [
    'DELETE FROM access_identities',
    "UPDATE access_identities SET email='reassigned@berri.ai'",
    "UPDATE users SET email='reassigned@berri.ai' WHERE id='google:original-google-tin'",
    "UPDATE users SET kind='shared' WHERE id='google:original-google-tin'",
    "UPDATE access_identities SET user_id='google:other'",
])
def test_conversation_bootstrap_maintenance_revalidates_after_writer_wait(login, monkeypatch, change):
    app, client = login
    store, maintenance = app.state.store, app.state.session_maintenance
    first = client.get('/api/session').json()
    client.portal.call(drain, maintenance)
    store.identity({'method': 'google', 'identity': {'sub': 'other', 'email': 'tin@berri.ai'}})
    old = '2000-01-01T00:00:00+00:00'
    store.execute('UPDATE users SET updated_at=?', (old,))
    started = threading.Event()
    touch = store.touch_access_identity

    def observed_touch(*args):
        started.set()
        return touch(*args)

    monkeypatch.setattr(store, 'touch_access_identity', observed_touch)
    with ThreadPoolExecutor(max_workers=1) as requests, store.connect() as writer:
        writer.begin_write()
        response = requests.submit(client.get, '/api/session').result(timeout=2)
        assert response.json()['user_id'] == first['user_id']
        assert started.wait(2)
        # The response was authorized against the pre-change committed mapping;
        # queued accounting must revalidate after it acquires this writer lock.
        writer.execute(change)
    client.portal.call(drain, maintenance)
    assert all(row['updated_at'] == old for row in store.rows('SELECT updated_at FROM users'))
    if change.startswith('DELETE'):
        assert not store.rows('SELECT * FROM access_identities')


def test_conversation_bootstrap_maintenance_keeps_newer_last_seen(login):
    app, client = login
    session = client.get('/api/session').json()
    client.portal.call(drain, app.state.session_maintenance)
    info = app.state.security.signer.loads(client.cookies['workspace_session'])
    newer = '2030-01-01T00:00:00+00:00'
    app.state.store.execute('UPDATE users SET updated_at=? WHERE id=?', (newer, session['user_id']))
    app.state.store.touch_access_identity(info['identity'], session['user_id'], '2000-01-01T00:00:00+00:00')
    assert app.state.store.rows('SELECT updated_at FROM users WHERE id=?', (session['user_id'],))[0]['updated_at'] == newer


async def test_conversation_bootstrap_coalesces_and_bounds_pending_maintenance(monkeypatch):
    from types import SimpleNamespace
    import app.session_maintenance as module

    started, release = threading.Event(), threading.Event()
    calls = []

    def touch(identity, actor, stamp):
        calls.append((identity, actor, stamp))
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(module, 'MAX_PENDING_ACCOUNTS', 2)
    maintenance = SessionMaintenance(SimpleNamespace(touch_access_identity=touch))
    identity = {'issuer': 'verified', 'sub': 'first', 'email': 'one@example.com'}
    maintenance.schedule(identity, 'first')
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(50):
            maintenance.schedule(identity, 'first')
        maintenance.schedule({**identity, 'sub': 'second'}, 'second')
        maintenance.schedule({**identity, 'sub': 'third'}, 'third')
        assert len(calls) == 1
        assert len(maintenance.pending) == 2
        latest_stamp = maintenance.pending[('verified', 'first')][2]
    finally:
        release.set()
        await maintenance.close()
    assert [actor for _, actor, _ in calls] == ['first', 'first', 'second']
    assert calls[1][2] == latest_stamp
    maintenance.schedule(identity, 'first')
    assert not maintenance.pending


async def test_conversation_bootstrap_maintenance_failure_does_not_prevent_future_updates(caplog):
    from types import SimpleNamespace

    calls = []

    def touch(identity, actor, stamp):
        calls.append(actor)
        if len(calls) == 1:
            raise RuntimeError('sensitive database message')

    maintenance = SessionMaintenance(SimpleNamespace(touch_access_identity=touch))
    identity = {'issuer': 'verified', 'sub': 'first', 'email': 'one@example.com'}
    maintenance.schedule(identity, 'first')
    await drain(maintenance)
    assert 'sensitive database message' not in caplog.text
    assert 'next session read will retry' in caplog.text
    maintenance.schedule(identity, 'first')
    await maintenance.close()
    assert calls == ['first', 'first']


async def test_conversation_bootstrap_shutdown_drains_active_write_and_discards_queue(monkeypatch):
    from types import SimpleNamespace
    import app.session_maintenance as module

    started, release = threading.Event(), threading.Event()
    calls = []

    def touch(identity, actor, stamp):
        calls.append(actor)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(module, 'SHUTDOWN_GRACE_SECONDS', .01)
    maintenance = SessionMaintenance(SimpleNamespace(touch_access_identity=touch))
    identity = {'issuer': 'verified', 'sub': 'first', 'email': 'one@example.com'}
    maintenance.schedule(identity, 'first')
    assert await asyncio.to_thread(started.wait, 2)
    maintenance.schedule({**identity, 'sub': 'second'}, 'second')
    closing = asyncio.create_task(maintenance.close())
    try:
        await asyncio.sleep(.05)
        assert not closing.done(), 'In-flight transaction must finish before the database closes'
        assert not maintenance.pending
    finally:
        release.set()
        await closing
    assert calls == ['first']
    assert maintenance.worker is None
