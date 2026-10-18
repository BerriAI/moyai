"""Real PostgreSQL process-admission/seal races; no provider calls."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from moyai.fleet_registry import PROTOCOL, FleetAdmissionError, FleetRegistry


@pytest.fixture
def registry():
    dsn = os.environ.get("MOYAI_TEST_POSTGRES")
    if not dsn:
        pytest.skip("Requires isolated PostgreSQL.")
    assert conninfo_to_dict(dsn)["host"] in {"127.0.0.1", "localhost", "::1"}
    suffix = uuid4().hex[:16]
    schema, source = "moyai_fleet_admission_" + suffix, "source_fleet_" + suffix
    with psycopg.connect(dsn, autocommit=True) as connection, psycopg.connect(dsn, autocommit=True) as owner:
        store = FleetRegistry(connection, schema=schema)
        store.initialize()
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(source)))
        owner.execute("SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)", (source,))
        pid, started = owner.execute("SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()").fetchone()
        service = "srv-" + suffix + "0000"
        store.connection.execute(store.query("""
            INSERT INTO {schema}.fleets VALUES('production',%s,%s,'native-queue',true)
        """), (source, source + "_n"))
        store.connection.execute(store.query("""
            INSERT INTO {schema}.roles (
                fleet,generation,role,service_name,service_id,source_sha,cluster_policy,
                role_policy,worker_deployment,worker_build,state
            ) VALUES('production','blue','coordinator','moyai-blue-coordinator',%s,%s,%s,%s,
                     'workers','blue-build','accepting')
        """), (service, "a" * 40, "b" * 64, "c" * 64))
        identity = {
            "protocol": "moyai-role-drain-v1", "instance": uuid4().hex, "process_pid": 123,
            "build": "a" * 40, "schema": source, "role": "coordinator",
            "owner_pid": pid, "owner_started": started.astimezone(UTC).isoformat(),
            "cluster_policy": "b" * 64, "role_policy": "c" * 64,
        }
        arguments = {
            "source_schema": source, "service_id": service, "instance_id": service + "-abcde",
            "service_name": "moyai-blue-coordinator", "generation": "blue",
            "identity": identity, "native_schema": source + "_n", "queue": "native-queue",
            "deployment": "workers", "build": "blue-build",
        }
        try:
            yield store, dsn, schema, arguments
        finally:
            owner.close()
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(source)))


def proof(arguments, role="coordinator"):
    value = {
        "generation": "blue", "role": role, "service_id": arguments["service_id"],
        "source_sha": "a" * 40, "ingress_revision": 8, "ingress_digest": "d" * 64,
        "ingress_instances": 2, "ingress_observation_digest": "e" * 64, "worker": None,
    }
    if role == "worker":
        value["worker"] = {
            "deployment": "workers", "build": "blue-build", "temporal_drained": True,
            "running": [], "drainage": "VERSION_DRAINAGE_STATUS_DRAINED",
            "drainage_checked": "2026-10-18T00:01:00+00:00",
            "routing_changed": "2026-10-18T00:00:00+00:00",
            "routing_fingerprint": "f" * 64, "evidence_digest": "a" * 64,
        }
    return value


def seal(store, arguments, **changes):
    return store.seal(**{
        "fleet": "production", "generation": "blue", "role": arguments["identity"]["role"],
        "service_id": arguments["service_id"], "request_id": "release-one:coordinator",
        "proof": proof(arguments, arguments["identity"]["role"]),
        **changes,
    })


def test_new_process_cannot_undo_an_existing_service_drain(registry):
    store, _, _, arguments = registry
    admitted = store.register(**arguments)
    assert admitted["state"] == "accepting"
    captured = seal(store, arguments)
    assert len(captured) == 1 and captured[0]["process_nonce"] == arguments["identity"]["instance"]
    assert store.register(**arguments)["state"] == "draining"  # Same owner may reconcile its registration reply.
    restarted = deepcopy(arguments)
    restarted["identity"]["instance"] = uuid4().hex
    restarted["identity"]["process_pid"] += 1
    with pytest.raises(FleetAdmissionError, match="service_sealed"):
        store.register(**restarted)
    assert seal(store, arguments) == captured
    with pytest.raises(FleetAdmissionError, match="seal_identity_changed"):
        seal(store, arguments, request_id="different-release")


def test_registration_commit_and_role_seal_have_a_database_order(registry):
    store, dsn, schema, arguments = registry
    entered, finished = threading.Event(), threading.Event()

    def sealing():
        with psycopg.connect(dsn, autocommit=True) as connection:
            other = FleetRegistry(connection, schema=schema)
            entered.set()
            result = seal(other, arguments)
            finished.set()
            return result

    with ThreadPoolExecutor(1) as executor:
        with store.connection.transaction():
            store.register(**arguments)
            pending = executor.submit(sealing)
            assert entered.wait(2)
            assert not finished.wait(.1)  # The admitted owner still holds its shared role-row lock.
        captured = pending.result(timeout=5)
    assert len(captured) == 1 and captured[0]["owner_pid"] == arguments["identity"]["owner_pid"]
    with pytest.raises(FleetAdmissionError, match="service_sealed"):
        store.register(**{**arguments, "identity": {**arguments["identity"], "instance": uuid4().hex}})


def test_ordinary_active_service_restart_does_not_need_a_running_controller(registry):
    store, _, _, arguments = registry
    store.register(**arguments)
    replacement = deepcopy(arguments)
    replacement["instance_id"] = arguments["service_id"] + "-fghij"
    replacement["identity"]["instance"] = uuid4().hex
    store.register(**replacement)
    assert len(seal(store, arguments)) == 2


def test_sealed_owner_capture_survives_process_retirement(registry):
    store, dsn, _, arguments = registry
    store.register(**arguments)
    replacement = deepcopy(arguments)
    with psycopg.connect(dsn, autocommit=True) as other:
        other.execute(
            "SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)", (arguments["source_schema"],),
        )
        pid, started = other.execute(
            "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
        ).fetchone()
        replacement["identity"].update(owner_pid=pid, owner_started=started.isoformat(), instance=uuid4().hex)
        replacement["instance_id"] = arguments["service_id"] + "-fghij"
        store.register(**replacement)
        captured = seal(store, arguments)
    assert len(captured) == 2
    assert seal(store, arguments) == captured


def test_seal_refreshes_stats_after_a_new_owner_registers(registry):
    store, dsn, schema, arguments = registry
    store.register(**arguments)
    with store.connection.transaction():
        store.connection.execute("SELECT count(*) FROM pg_stat_activity").fetchone()
        with psycopg.connect(dsn, autocommit=True) as owner, psycopg.connect(dsn, autocommit=True) as registrar:
            owner.execute(
                "SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)", (arguments["source_schema"],),
            )
            pid, started = owner.execute(
                "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
            ).fetchone()
            replacement = deepcopy(arguments)
            replacement["identity"].update(owner_pid=pid, owner_started=started.isoformat(), instance=uuid4().hex)
            replacement["instance_id"] = arguments["service_id"] + "-fghij"
            FleetRegistry(registrar, schema=schema).register(**replacement)
            assert len(seal(store, arguments)) == 2


@pytest.mark.parametrize("field,value", [
    ("build", "different-build"), ("deployment", "different-fleet"),
    ("native_schema", "different_schema"), ("queue", "different-queue"),
    ("generation", "different-generation"), ("service_id", "srv-unknown00000000000"),
])
def test_unknown_scope_cannot_register_for_polling(registry, field, value):
    store, _, _, arguments = registry
    with pytest.raises(FleetAdmissionError):
        store.register(**{**arguments, field: value})


def test_owner_pid_without_its_exact_backend_lifetime_is_not_admission(registry):
    store, _, _, arguments = registry
    changed = deepcopy(arguments)
    changed["identity"]["owner_started"] = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    with pytest.raises(FleetAdmissionError, match="owner_missing"):
        store.register(**changed)


def test_legacy_absent_registry_is_not_created_by_reading(registry):
    store, _, _, arguments = registry
    absent = FleetRegistry(store.connection, schema=store.schema + "_absent")
    assert absent.scope(arguments["source_schema"]) is None
    assert store.connection.execute("SELECT to_regnamespace(%s)", (absent.schema,)).fetchone()[0] is None
    assert store.installed() and PROTOCOL == "moyai-fleet-admission-v1"


@pytest.mark.parametrize("fault", ["missing", "wrong-build", "active-workflow", "stale", "single-ingress"])
def test_worker_seal_requires_matching_temporal_and_ingress_evidence(registry, fault):
    store, _, _, arguments = registry
    store.connection.execute(store.query("UPDATE {schema}.roles SET role='worker'"))
    arguments = deepcopy(arguments)
    arguments["identity"]["role"] = "worker"
    arguments["service_name"] = "moyai-blue-coordinator"  # Bound provider name is unchanged in this isolated case.
    store.register(**arguments)
    evidence = proof(arguments, "worker")
    if fault == "missing":
        evidence["worker"] = None
    elif fault == "wrong-build":
        evidence["worker"]["build"] = "other-build"
    elif fault == "active-workflow":
        evidence["worker"]["running"] = ["still-running"]
    elif fault == "stale":
        evidence["worker"]["drainage_checked"] = "2026-10-17T00:00:00+00:00"
    else:
        evidence["ingress_instances"] = 1
    with pytest.raises(FleetAdmissionError, match="seal_proof_invalid"):
        seal(store, arguments, proof=evidence)
    # Rejection leaves the generation open; no false drain receipt was saved.
    assert store.register(**arguments)["state"] == "accepting"
