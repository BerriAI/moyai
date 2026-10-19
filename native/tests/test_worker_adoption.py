"""Real PostgreSQL authorization for the initial same-image worker bridge."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.native_contract import core_contract
from app.runtime_compatibility import (
    api_fingerprint,
    broker_fingerprint,
    coordinator_fingerprint,
    worker_fingerprint,
)
from psycopg import sql
from psycopg.types.json import Jsonb
from test_fleet_registry import proof
from test_fleet_registry import registry as registry_fixture
from test_product_deployment import configured
from test_product_deployment import environment as product_environment

from moyai.fleet_admission import FleetAdmission
from moyai.fleet_registry import FleetAdmissionError
from moyai.worker_adoption import AdoptionBinding, WorkerAdoption

registry = registry_fixture
environment = product_environment


@pytest.fixture
def adoption(registry):
    store, _, _, arguments = registry
    arguments = deepcopy(arguments)
    arguments.update(deployment="", build="")
    store.connection.execute(store.query("""
        UPDATE {schema}.roles SET worker_deployment='',worker_build=''
        WHERE fleet='production' AND generation='blue'
    """))
    binding = AdoptionBinding(
        fleet="production", source_schema=arguments["source_schema"], native_schema=arguments["native_schema"],
        queue=arguments["queue"], source_generation="blue", target_generation="green",
        source_sha="a" * 40, deployment="workers", build="green-build", cluster_policy="b" * 64,
        configuration_digest="d" * 64,
    )
    return store, WorkerAdoption(store), binding, arguments


def candidate(store, arguments, *, sha="a" * 40, generation="green", build="green-build", bridge=True):
    value = deepcopy(arguments)
    value.update(generation=generation, deployment="workers", build=build, bridge=bridge)
    value["identity"]["build"] = sha
    store.connection.execute(store.query("""
        UPDATE {schema}.roles SET generation=%s,source_sha=%s,worker_deployment='workers',worker_build=%s
        WHERE fleet='production'
    """), (generation, sha, build))
    return value


def test_only_explicit_bridge_keeps_the_unversioned_role_contracts(environment):
    before = configured(environment).legacy_settings()
    versioned_env = {
        **environment, "MOYAI_WORKER_DEPLOYMENT": "workers", "MOYAI_WORKER_BUILD_ID": "green",
    }
    named = configured(versioned_env).legacy_settings()
    bridge = configured({**versioned_env, "MOYAI_WORKER_ADOPTION_BRIDGE": "true"})
    source = bridge.legacy_settings()
    assert bridge.native_overrides()["worker_adoption_bridge"] is True
    assert core_contract(source) == core_contract(before)
    assert core_contract(named) != core_contract(before)
    for fingerprint in (api_fingerprint, worker_fingerprint, broker_fingerprint, coordinator_fingerprint):
        assert fingerprint(source) == fingerprint(before)
        assert fingerprint(named) != fingerprint(before)


@pytest.mark.parametrize("change", [
    {"MOYAI_WORKER_DEPLOYMENT": "", "MOYAI_WORKER_BUILD_ID": ""},
    {"MOYAI_RUNTIME_ROLE": "standalone"},
])
def test_invalid_bridge_configuration_is_rejected(environment, change):
    with pytest.raises(ValueError):
        configured({
            **environment, "MOYAI_WORKER_DEPLOYMENT": "workers", "MOYAI_WORKER_BUILD_ID": "green",
            "MOYAI_WORKER_ADOPTION_BRIDGE": "true", **change,
        })


def test_bridge_cannot_disable_required_admission(monkeypatch):
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setenv("MOYAI_FLEET_REQUIRED", "false")
    holder = SimpleNamespace(runtime=SimpleNamespace(
        settings=SimpleNamespace(worker_adoption_bridge=True),
        product=SimpleNamespace(settings=SimpleNamespace(moyai_build_sha="a" * 40)),
    ))
    assert FleetAdmission(holder).required


def test_bridge_coordinator_cannot_publish_a_policy_when_no_owner_remains(registry, environment, tmp_path):
    from app.database import DatabaseError, PostgresDatabase
    from app.db import Store

    store, dsn, _, _ = registry
    schema = "moyai_adoption_" + uuid4().hex[:12]
    operator = store.connection
    operator.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    initial = Store(tmp_path / "unowned", database_url=dsn, database_schema=schema,
                    database_initialize=True, database_pool_size=1)
    initial.close()
    settings = configured({
        **environment, "MOYAI_DATABASE_SCHEMA": schema, "MOYAI_RUNTIME_ROLE": "coordinator",
        "MOYAI_WORKER_DEPLOYMENT": "workers", "MOYAI_WORKER_BUILD_ID": "green",
        "MOYAI_WORKER_ADOPTION_BRIDGE": "true",
    }).legacy_settings()
    joined = None
    try:
        with pytest.raises(DatabaseError, match="cannot establish"):
            joined = PostgresDatabase(
                dsn, schema, application_instance=True, runtime_role="coordinator",
                runtime_settings=settings, schema_mode="verify", pool_size=1,
            )
            joined.configure_runtime(settings)
        assert operator.execute(sql.SQL("SELECT count(*) FROM {}.runtime_policy").format(
            sql.Identifier(schema),
        )).fetchone() == (0,)
    finally:
        if joined is not None:
            joined.close()
        operator.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_unversioned_owner_needs_an_explicit_binding(adoption):
    store, permit, binding, arguments = adoption
    with pytest.raises(FleetAdmissionError, match="authorization_required"):
        store.register(**arguments)
    assert not permit.installed()
    permit.initialize()
    with pytest.raises(FleetAdmissionError, match="authorization_required"):
        store.register(**arguments)
    permit.prepare(binding, request_id="adoption-one")
    assert store.register(**arguments)["generation"] == "blue"
    # Explicit registry verification accepts the installed optional extension.
    store.initialize()


def test_same_image_candidate_is_authorized_but_no_routing_or_retirement_is_performed(adoption):
    store, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    target = candidate(store, arguments)
    assert store.register(**target)["generation"] == "green"
    assert permit.load(binding.fleet) == (binding, "prepared", "adoption-one")
    with pytest.raises(FleetAdmissionError, match="source_unregistered"):
        permit.activate(binding, request_id="adoption-one")
    assert permit.load(binding.fleet)[1] == "prepared"


def test_unversioned_worker_cannot_use_fabricated_version_drainage(adoption):
    store, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    arguments["identity"]["role"] = "worker"
    store.connection.execute(store.query("UPDATE {schema}.roles SET role='worker'"))
    store.register(**arguments)
    claimed = proof(arguments, "worker")
    claimed["worker"].update(deployment="", build="")
    with pytest.raises(FleetAdmissionError, match="seal_proof_invalid"):
        store.seal(
            fleet=binding.fleet, generation=binding.source_generation, role="worker",
            service_id=arguments["service_id"], request_id="unqualified-drain", proof=claimed,
        )
    assert store.connection.execute(store.query("SELECT state FROM {schema}.roles")).fetchone() == ("accepting",)


@pytest.mark.parametrize("changes", [
    {"sha": "c" * 40}, {"generation": "unreviewed"}, {"build": "unreviewed"}, {"bridge": False},
])
def test_prepared_bridge_rejects_unreviewed_image_generation_build_or_mode(adoption, changes):
    store, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    with pytest.raises(FleetAdmissionError, match="runtime_not_authorized"):
        store.register(**candidate(store, arguments, **changes))
    assert store.connection.execute(store.query("SELECT count(*) FROM {schema}.members")).fetchone() == (0,)


def test_binding_recovery_is_idempotent_and_cannot_change_the_approved_identity(adoption):
    store, permit, binding, _ = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    WorkerAdoption(store).prepare(binding, request_id="adoption-one")
    with pytest.raises(FleetAdmissionError, match="intent_changed"):
        permit.prepare(replace(binding, source_sha="c" * 40), request_id="adoption-one")
    with pytest.raises(FleetAdmissionError, match="intent_changed"):
        permit.prepare(binding, request_id="different-request")
    assert permit.load(binding.fleet) == (binding, "prepared", "adoption-one")


def test_relabelled_binding_without_matching_receipt_is_not_authority(adoption):
    store, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    document = {**binding.__dict__, "source_sha": "c" * 40}
    store.connection.execute(store.query("""
        UPDATE {schema}.worker_adoption SET binding=%s WHERE fleet=%s
    """), (Jsonb(document), binding.fleet))
    with pytest.raises(FleetAdmissionError, match="receipt_invalid"):
        store.register(**arguments)


def test_cancelled_preparation_cannot_be_revived_or_block_a_normal_named_contract(adoption):
    store, permit, binding, arguments = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    store.connection.execute(store.query("UPDATE {schema}.worker_adoption SET phase='cancelled'"))
    with pytest.raises(FleetAdmissionError, match="cancelled"):
        permit.prepare(binding, request_id="adoption-one")
    target = candidate(store, arguments)
    with pytest.raises(FleetAdmissionError, match="cancelled"):
        store.register(**target)
    # Normal named-contract adoption has its own canonical compatibility guard.
    # An abandoned unversioned preparation must not authorize or constrain it.
    target["bridge"] = False
    assert store.register(**target)["generation"] == "green"


def test_activation_requires_all_source_roles_retired_and_target_roles_accepted(adoption):
    store, permit, binding, _ = adoption
    permit.initialize()
    permit.prepare(binding, request_id="adoption-one")
    with pytest.raises(FleetAdmissionError, match="source_unregistered"):
        permit.activate(binding, request_id="adoption-one")
    store.connection.execute(store.query("UPDATE {schema}.worker_adoption SET phase='source_registered'"))
    store.connection.execute(store.query("DELETE FROM {schema}.roles"))
    for generation, state, deployment, build in (
        ("blue", "retired", "", ""), ("green", "accepting", "workers", "green-build"),
    ):
        for role in ("api", "broker", "coordinator", "worker"):
            store.connection.execute(store.query("""
                INSERT INTO {schema}.roles (
                    fleet,generation,role,service_name,source_sha,cluster_policy,
                    role_policy,worker_deployment,worker_build,state
                ) VALUES('production',%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """), (generation, role, generation + "-" + role, "a" * 40, "b" * 64, "c" * 64,
                   deployment, build, state))
    store.connection.execute(store.query("""
        UPDATE {schema}.roles SET state='draining' WHERE generation='blue' AND role='worker'
    """))
    with pytest.raises(FleetAdmissionError, match="retirement_unconfirmed"):
        permit.activate(binding, request_id="adoption-one")
    store.connection.execute(store.query("""
        UPDATE {schema}.roles SET state='retired' WHERE generation='blue' AND role='worker'
    """))
    permit.activate(binding, request_id="adoption-one")
    assert permit.load(binding.fleet)[1] == "active"
    # A lost activation reply can reconcile even after a later fleet retired
    # the original target. The immutable activation receipt remains authority.
    store.connection.execute(store.query("UPDATE {schema}.roles SET state='retired' WHERE generation='green'"))
    permit.activate(binding, request_id="adoption-one")


@pytest.mark.parametrize("field,value", [
    ("source_sha", "latest"), ("deployment", ""), ("target_generation", "blue"),
    ("native_schema", "same"), ("cluster_policy", "bad"),
])
def test_binding_rejects_ambiguous_or_mutable_identity(field, value):
    fields = {
        "fleet": "fleet", "source_schema": "same", "native_schema": "native", "queue": "native-q",
        "source_generation": "blue", "target_generation": "green", "source_sha": "a" * 40,
        "deployment": "workers", "build": "green", "cluster_policy": "b" * 64,
        "configuration_digest": "d" * 64, field: value,
    }
    with pytest.raises(FleetAdmissionError, match="binding_invalid"):
        AdoptionBinding(**fields)
