"""Real PostgreSQL worker registrations correlated with process ownership."""
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from scripts.release.native_rolling_probe import piped_source, workers
from scripts.release.rolling_probe import snapshot
from test_rolling_probe import owners  # noqa: F401
from postgres_fixture import target  # noqa: F401


@pytest.fixture
def membership(owners, monkeypatch):
    url, schema, connections = owners
    native = 'nroll_' + uuid4().hex[:8]
    monkeypatch.setenv('MOYAI_NATIVE_SCHEMA', native)
    control = connections[0]
    control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(native)))
    control.execute(sql.SQL('''CREATE TABLE {}.moyai_product_drain_workers(
        id text PRIMARY KEY, owner_pid integer, owner_started text,
        requested_drain boolean, source_policy text, worker_policy text)''').format(sql.Identifier(native)))
    def register(connection, identity='worker', **changes):
        started = connection.execute(
            'SELECT backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()').fetchone()[0]
        values = dict(pid=connection.info.backend_pid, started=started.isoformat(),
                      drain=False, source='f' * 64, policy='b' * 64)
        values.update(changes)
        control.execute(sql.SQL('INSERT INTO {}.moyai_product_drain_workers VALUES(%s,%s,%s,%s,%s,%s)')
            .format(sql.Identifier(native)), (identity, *values.values()))
    register(connections[1])
    try:
        yield url, schema, connections, register
    finally:
        control.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(native)))


def observe(url, schema):
    with psycopg.connect(url) as reader:
        state = snapshot(reader, schema)
        return workers(reader, state)


def test_registered_worker_overlap_and_retirement_use_real_live_identities(membership):
    url, schema, connections, register = membership
    original = observe(url, schema)
    assert len(original) == 1 and original[0].startswith(str(connections[1].info.backend_pid) + '@')
    with psycopg.connect(url, autocommit=True) as replacement:
        replacement.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        # An admitted but not-yet-polling candidate is not a registered worker.
        assert observe(url, schema) == original
        register(replacement, 'replacement')
        overlap = observe(url, schema)
        assert len(overlap) == 2 and set(original) < set(overlap)
    # Historical registration rows survive, but retired processes are absent.
    assert observe(url, schema) == original


@pytest.mark.parametrize('fault', ['drain', 'cluster', 'contract', 'broker', 'duplicate', 'unowned'])
def test_ambiguous_or_incompatible_native_membership_is_rejected(membership, fault):
    url, schema, connections, register = membership
    connection = connections[2] if fault == 'broker' else connections[1]
    changes = {'drain': True} if fault == 'drain' else (
        {'source': 'e' * 64} if fault == 'cluster' else {'policy': 'e' * 64} if fault == 'contract' else {})
    if fault == 'unowned':
        connections[1].execute('SELECT pg_advisory_unlock_all()')
    else:
        register(connection, 'unexpected', **changes)
    with pytest.raises(ValueError):
        observe(url, schema)


def test_stale_pid_with_different_backend_start_does_not_register(membership):
    url, schema, connections, register = membership
    original = observe(url, schema)
    register(connections[1], 'stale', started='2000-01-01T00:00:00+00:00')
    assert observe(url, schema) == original


def test_worker_observation_stays_read_only(membership):
    url, schema, _, _ = membership
    with psycopg.connect(url) as reader:
        state = snapshot(reader, schema)
        assert workers(reader, state)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            reader.execute('DELETE FROM runtime_policy')


def test_piped_helpers_load_without_installed_release_package():
    namespace = {'__name__': 'piped_native_rolling'}
    exec(piped_source(), namespace)
    assert namespace['_native_api_probe'] is namespace['_helpers']['_native_api_probe']
    assert callable(namespace['snapshot']) and callable(namespace['probe'])
