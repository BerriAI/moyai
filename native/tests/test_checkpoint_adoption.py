"""PostgreSQL retirement fencing for the one-time unversioned handoff."""
from __future__ import annotations

from copy import deepcopy

import pytest
from psycopg import sql
from test_fleet_registry import proof
from test_worker_adoption import adoption as adoption_fixture
from test_worker_adoption import registry as registry_fixture

from moyai.fleet_registry import FleetAdmissionError
from moyai.worker_adoption import CHECKPOINT_PROTOCOL

registry = registry_fixture
adoption = adoption_fixture


@pytest.fixture
def checkpoint(adoption):
    registry, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    db = registry.connection
    db.execute(registry.query("UPDATE {schema}.worker_adoption SET phase='source_registered'"))
    db.execute(registry.query("UPDATE {schema}.roles SET role='worker'"))
    arguments["identity"]["role"] = "worker"
    registry.register(**arguments)
    db.execute(registry.query("""
        INSERT INTO {schema}.roles
        SELECT fleet,'green',role,'moyai-green-worker',service_id||'1',source_sha,cluster_policy,
            role_policy,'workers','green-build','preparing','',''
        FROM {schema}.roles WHERE generation='blue'
    """))
    # A real backend lifetime is required; this fixture intentionally reuses its
    # owner only to isolate the seal protocol. E2E uses separate app processes.
    db.execute(registry.query("""
        INSERT INTO {schema}.members
        SELECT fleet,'green',role,service_id||'1',instance_id,process_nonce,owner_pid,owner_started,
            identity_digest,drain_request,drained_request,drained_at,registered_at
        FROM {schema}.members WHERE generation='blue'
    """))
    db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(binding.native_schema)))
    db.execute(sql.SQL("""
        CREATE TABLE {}.moyai_product_drain_state (
            id int PRIMARY KEY,generation bigint,paused boolean,request_key text,
            request_fingerprint text,source_policy text
        )
    """).format(sql.Identifier(binding.native_schema)))
    db.execute(sql.SQL("INSERT INTO {}.moyai_product_drain_state VALUES(1,3,true,'pause-one',%s,%s)").format(
        sql.Identifier(binding.native_schema),
    ), ("c" * 64, binding.cluster_policy))
    claimed = proof(arguments, "worker")
    claimed["worker"] = {
        "protocol": CHECKPOINT_PROTOCOL, "generation": 3, "request_key": "pause-one",
        "request_fingerprint": "c" * 64, "target_generation": "green",
        "deployment": "workers", "build": "green-build", "routing_fingerprint": "f" * 64,
    }
    def seal(value=None):
        return registry.seal(
            fleet=binding.fleet, generation="blue", role="worker",
            service_id=arguments["service_id"], request_id="pause-one", proof=value or claimed,
        )
    try:
        yield registry, binding, claimed, seal
    finally:
        db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(binding.native_schema)))


def test_checkpoint_seal_captures_exact_owner_and_reconciles_lost_reply(checkpoint):
    registry, _, _, seal = checkpoint
    captured = seal()
    assert len(captured) == 1
    assert seal() == captured
    assert registry.connection.execute(registry.query(
        "SELECT state FROM {schema}.roles WHERE generation='blue'",
    )).fetchone() == ("draining",)


@pytest.mark.parametrize("change", [
    {"generation": 2}, {"request_key": "different-pause"}, {"request_fingerprint": "a" * 64},
    {"target_generation": "other"}, {"deployment": "other"}, {"build": "other"},
    {"protocol": "invented"}, {"routing_fingerprint": ""},
])
def test_stale_or_different_checkpoint_is_rejected_without_sealing(checkpoint, change):
    registry, _, claimed, seal = checkpoint
    changed = deepcopy(claimed)
    changed["worker"].update(change)
    with pytest.raises(FleetAdmissionError):
        seal(changed)
    assert registry.connection.execute(registry.query(
        "SELECT state FROM {schema}.roles WHERE generation='blue'",
    )).fetchone() == ("accepting",)


@pytest.mark.parametrize("fault", ["unpaused", "target_sealed", "target_missing", "unregistered"])
def test_pause_and_candidate_must_still_exist_at_the_seal(checkpoint, fault):
    registry, binding, _, seal = checkpoint
    db = registry.connection
    if fault == "unpaused":
        db.execute(sql.SQL("UPDATE {}.moyai_product_drain_state SET paused=false").format(
            sql.Identifier(binding.native_schema),
        ))
    elif fault == "target_sealed":
        db.execute(registry.query("UPDATE {schema}.roles SET state='draining' WHERE generation='green'"))
    elif fault == "target_missing":
        db.execute(registry.query("DELETE FROM {schema}.members WHERE generation='green'"))
    else:
        db.execute(registry.query("UPDATE {schema}.worker_adoption SET phase='prepared'"))
    with pytest.raises(FleetAdmissionError):
        seal()
