"""An explicit native transaction marker never removes the runtime fence."""
import psycopg
from psycopg import sql
import pytest
from uuid import uuid4

from scripts.release.rolling_probe import health_identity, snapshot
from test_rolling_probe import owners, response  # noqa: F401
from postgres_fixture import target  # noqa: F401


def observe(url, schema):
    with psycopg.connect(url) as connection:
        return snapshot(connection, schema)


def test_unmarked_native_or_unexpected_process_lock_remains_an_owner(owners):
    url, schema, _ = owners
    with psycopg.connect(url, autocommit=True) as child, child.transaction():
        child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        state = observe(url, schema)
        assert len(state['owners']) == 5 and 'native_transaction_fences' not in state


def test_bound_subordinate_is_reported_separately_and_still_blocks_exclusive_admission(owners):
    url, schema, parents = owners
    parent = parents[-1].info.backend_pid
    with psycopg.connect(url, autocommit=True) as child, psycopg.connect(url, autocommit=True) as migrator:
        # Session binding precedes every transaction's 726940 fence and remains
        # until connection retirement; it is not reacquired on every transaction.
        child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parent,))
        with child.transaction():
            child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
            state = observe(url, schema)
            assert len(state['owners']) == 4
            assert len(state['native_transaction_fences']) == 1
            fence = state['native_transaction_fences'][0]
            assert fence['connection'].startswith(str(child.info.backend_pid) + '@')
            assert fence['owner'].startswith(str(parent) + '@')
            for connection in parents:
                connection.execute('SELECT pg_advisory_unlock_all()')
            with pytest.raises(ValueError):
                observe(url, schema)  # An orphan cannot silently become zero owners.
            assert migrator.execute('SELECT pg_try_advisory_lock(726940,%s::regnamespace::oid::int)',
                                    (schema,)).fetchone() == (False,)
        assert migrator.execute('SELECT pg_try_advisory_lock(726940,%s::regnamespace::oid::int)',
                                (schema,)).fetchone() == (True,)


@pytest.mark.parametrize('fault', ['orphan', 'zero', 'self', 'chain', 'cycle', 'ambiguous',
                                 'exclusive_marker', 'role_owner'])
def test_malformed_or_role_owning_markers_are_not_hidden(owners, fault):
    url, schema, parents = owners
    parent = parents[-1].info.backend_pid
    with psycopg.connect(url, autocommit=True) as child, child.transaction():
        target_pid = (99999999 if fault == 'orphan' else 0 if fault == 'zero'
                      else child.info.backend_pid if fault == 'self' else parent)
        function = 'pg_advisory_lock' if fault == 'exclusive_marker' else 'pg_advisory_lock_shared'
        child.execute(f'SELECT {function}(726948,%s)', (target_pid,))
        child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        if fault == 'ambiguous':
            child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parents[0].info.backend_pid,))
        elif fault in {'chain', 'cycle'}:
            parents[-1].execute('SELECT pg_advisory_lock_shared(726948,%s)',
                (child.info.backend_pid if fault == 'cycle' else parents[0].info.backend_pid,))
        elif fault == 'role_owner':
            child.execute('SELECT pg_advisory_lock_shared(726944,%s::regnamespace::oid::int)', (schema,))
        with pytest.raises(ValueError):
            observe(url, schema)


def test_marker_must_refer_to_an_owner_in_the_same_canonical_schema(owners):
    url, schema, _ = owners
    foreign_schema = 'moyai_foreign_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as foreign, psycopg.connect(url, autocommit=True) as child:
        foreign.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(foreign_schema)))
        try:
            foreign.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (foreign_schema,))
            child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (foreign.info.backend_pid,))
            with child.transaction():
                child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
                with pytest.raises(ValueError):
                    observe(url, schema)
        finally:
            foreign.execute('SELECT pg_advisory_unlock_all()')
            foreign.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(foreign_schema)))


@pytest.mark.parametrize('on_parent', [False, True])
def test_one_argument_bigint_marker_is_not_a_valid_binding_or_unmarked_parent(owners, on_parent):
    url, schema, parents = owners
    with psycopg.connect(url, autocommit=True) as child, child.transaction():
        # Same high/low bits, wrong advisory-key namespace (objsubid=1).
        marker_connection = parents[-1] if on_parent else child
        marker_connection.execute('SELECT pg_advisory_lock_shared(%s::bigint)',
                                  ((726948 << 32) | parents[-1].info.backend_pid,))
        if on_parent:
            child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parents[-1].info.backend_pid,))
        child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        with pytest.raises(ValueError):
            observe(url, schema)


def test_role_lock_in_another_schema_also_forbids_subordinate_classification(owners):
    url, schema, parents = owners
    other = 'moyai_foreign_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as child:
        child.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(other)))
        try:
            child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parents[-1].info.backend_pid,))
            child.execute('SELECT pg_advisory_lock(726943,%s::regnamespace::oid::int)', (other,))
            with child.transaction():
                child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
                with pytest.raises(ValueError):
                    observe(url, schema)
        finally:
            child.execute('SELECT pg_advisory_unlock_all()')
            child.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(other)))


def test_idle_binding_is_not_an_owner_and_a_child_cannot_supply_role_health(owners):
    url, schema, parents = owners
    with psycopg.connect(url, autocommit=True) as child:
        child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parents[-1].info.backend_pid,))
        idle = observe(url, schema)
        assert len(idle['owners']) == 4 and 'native_transaction_fences' not in idle
        with child.transaction():
            child.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
            state = observe(url, schema)
            body = response('api')
            body.headers.replace_header('X-Moyai-Owner', str(child.info.backend_pid))
            with pytest.raises(ValueError):
                health_identity(body, {'sha': 'a' * 40, 'role': 'api'}, state)
        assert observe(url, schema)['owners'] == idle['owners']


def test_online_guard_uses_the_same_binding_rules_and_excludes_only_its_own_fence(owners):
    from scripts.release.online_migration import check_runtime
    url, schema, parents = owners
    expected = observe(url, schema)
    with psycopg.connect(url, autocommit=True) as child, psycopg.connect(url, autocommit=True) as migrator:
        child.execute('SELECT pg_advisory_lock_shared(726948,%s)', (parents[-1].info.backend_pid,))
        with child.transaction(), migrator.transaction():
            for connection in (child, migrator):
                connection.execute('SELECT pg_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
            check_runtime(migrator, schema, expected)
            with psycopg.connect(url, autocommit=True) as stranger:
                stranger.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
                with pytest.raises(ValueError):
                    check_runtime(migrator, schema, expected)
