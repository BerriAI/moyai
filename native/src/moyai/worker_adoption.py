"""Durable authorization for joining an unversioned runtime contract.

Provisioning is an explicit operator action. Runtime reads use the existing
fleet-registry connection; this module creates no pools and changes no routing.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

from psycopg import sql
from psycopg.types.json import Jsonb

from .fleet_registry import FleetAdmissionError, FleetRegistry, fingerprint

PROTOCOL = "moyai-worker-adoption-v1"
CHECKPOINT_PROTOCOL = "moyai-adoption-checkpoint-v1"
NAME = r"[a-z][a-z0-9_-]{0,62}"
SCHEMA = r"[a-z][a-z0-9_]{0,62}"
VERSION = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


def verify_bridge_before_runtime(config, connection) -> None:
    """Use the existing read-only schema probe before canonical initialization."""
    source = config.legacy_settings()
    if not source.moyai_worker_adoption_bridge:
        return
    from .fleet_admission import service_identity
    from .product_deployment import candidate_role_fingerprint

    service_id, _, service_name = service_identity(source.moyai_build_sha, local_rehearsal=config.local_rehearsal)
    registry = FleetRegistry(connection)
    scope = registry.scope(source.moyai_database_schema)
    if scope is None or not scope["enabled"]:
        raise FleetAdmissionError("worker_adoption_registry_not_enabled")
    generation = os.environ.get("MOYAI_RELEASE_GENERATION", "")
    role = registry.role(fleet=scope["fleet"], service_id=service_id,
                         service_name=service_name, generation=generation)
    if role is None or role["state"] not in {"preparing", "accepting"}:
        raise FleetAdmissionError("worker_adoption_role_not_authorized")
    if role["service_name"] != service_name or role["service_id"] not in {None, service_id}:
        raise FleetAdmissionError("worker_adoption_service_changed")
    policy = connection.execute(sql.SQL("SELECT fingerprint FROM {}.runtime_policy WHERE id=1").format(
        sql.Identifier(source.moyai_database_schema),
    )).fetchone()
    if policy is None:
        raise FleetAdmissionError("worker_adoption_canonical_policy_missing")
    registry.validate(
        scope, role, identity={"role": config.role, "schema": source.moyai_database_schema,
                              "build": source.moyai_build_sha, "cluster_policy": policy[0],
                              "role_policy": candidate_role_fingerprint(config)},
        native_schema=config.native_schema, queue=config.native_task_queue,
        deployment=source.moyai_worker_deployment, build=source.moyai_worker_build_id,
        generation=generation, bridge=True,
    )


@dataclass(frozen=True)
class AdoptionBinding:
    fleet: str
    source_schema: str
    native_schema: str
    queue: str
    source_generation: str
    target_generation: str
    source_sha: str
    deployment: str
    build: str
    cluster_policy: str
    configuration_digest: str

    def __post_init__(self) -> None:
        patterns = {
            "fleet": NAME, "source_schema": SCHEMA, "native_schema": SCHEMA,
            "source_generation": NAME, "target_generation": NAME,
            "source_sha": r"[a-f0-9]{40}", "deployment": VERSION, "build": VERSION,
            "cluster_policy": r"[a-f0-9]{64}",
            "configuration_digest": r"[a-f0-9]{64}",
        }
        if (
            any(not isinstance(value := getattr(self, field), str) or not re.fullmatch(pattern, value)
                for field, pattern in patterns.items())
            or self.source_schema == self.native_schema or self.source_generation == self.target_generation
            or not isinstance(self.queue, str) or not self.queue or len(self.queue) > 200
        ):
            raise FleetAdmissionError("worker_adoption_binding_invalid")

    @property
    def digest(self) -> str:
        return fingerprint(asdict(self))


class WorkerAdoption:
    def __init__(self, registry: FleetRegistry):
        self.registry, self.connection = registry, registry.connection

    def installed(self) -> bool:
        return self.connection.execute(
            "SELECT to_regclass(%s)", (self.registry.schema + ".worker_adoption",),
        ).fetchone()[0] is not None

    def initialize(self) -> None:
        """Explicit extension of the admission schema; never called at startup."""
        with self.connection.transaction():
            if not self.registry.installed():
                raise FleetAdmissionError("worker_adoption_registry_missing")
            self.connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (
                self.registry.schema + ":" + PROTOCOL,
            ))
            if self.installed():
                kind = self.connection.execute(
                    "SELECT relkind FROM pg_class WHERE oid=%s::regclass",
                    (self.registry.schema + ".worker_adoption",),
                ).fetchone()
                columns = dict(self.connection.execute("""
                    SELECT column_name,udt_name FROM information_schema.columns
                    WHERE table_schema=%s AND table_name='worker_adoption'
                """, (self.registry.schema,)))
                if kind != ("r",) or columns != {
                    "fleet": "text", "protocol": "text", "request_id": "text",
                    "binding": "jsonb", "binding_digest": "bpchar", "phase": "text",
                }:
                    raise FleetAdmissionError("worker_adoption_schema_unqualified")
                return
            self.connection.execute(self.registry.query("""
                CREATE TABLE {schema}.worker_adoption (
                    fleet text PRIMARY KEY REFERENCES {schema}.fleets(fleet),
                    protocol text NOT NULL, request_id text NOT NULL UNIQUE,
                    binding jsonb NOT NULL, binding_digest char(64) NOT NULL,
                    phase text NOT NULL CHECK(phase IN ('prepared','source_registered','active','cancelled'))
                )
            """))

    def load(self, fleet: str, *, lock: bool = False) -> tuple[AdoptionBinding, str, str] | None:
        if not self.installed():
            return None
        row = self.connection.execute(self.registry.query("""
            SELECT protocol,request_id,binding,binding_digest,phase
            FROM {schema}.worker_adoption WHERE fleet=%s
        """ + (" FOR SHARE" if lock else "")), (fleet,)).fetchone()
        if row is None:
            return None
        try:
            binding = AdoptionBinding(**row[2])
            if (
                row[0] != PROTOCOL or row[3] != binding.digest or binding.fleet != fleet
                or row[4] not in {"prepared", "source_registered", "active", "cancelled"}
            ):
                raise ValueError()
        except (TypeError, ValueError):
            raise FleetAdmissionError("worker_adoption_receipt_invalid") from None
        return binding, row[4], row[1]

    def prepare(self, binding: AdoptionBinding, *, request_id: str) -> None:
        """Persist an immutable permit before any candidate can start."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,199}", request_id):
            raise FleetAdmissionError("worker_adoption_request_invalid")
        with self.connection.transaction():
            scope = self.registry.scope(binding.source_schema, lock=True)
            if scope is None or any(scope[key] != value for key, value in {
                "fleet": binding.fleet, "native_schema": binding.native_schema, "task_queue": binding.queue,
            }.items()):
                raise FleetAdmissionError("worker_adoption_scope_changed")
            self.connection.execute(self.registry.query("""
                INSERT INTO {schema}.worker_adoption
                    (fleet,protocol,request_id,binding,binding_digest,phase)
                VALUES(%s,%s,%s,%s,%s,'prepared') ON CONFLICT(fleet) DO NOTHING
            """), (binding.fleet, PROTOCOL, request_id, Jsonb(asdict(binding)), binding.digest))
            saved = self.load(binding.fleet, lock=True)
            if saved is None or saved[0] != binding or saved[2] != request_id:
                raise FleetAdmissionError("worker_adoption_intent_changed")
            if saved[1] == "cancelled":
                raise FleetAdmissionError("worker_adoption_cancelled")

    def require(
        self, scope: dict, role: dict, *, identity: dict, deployment: str, build: str,
        bridge: bool, lock: bool = False,
    ) -> None:
        saved = self.load(scope["fleet"], lock=lock)
        if saved is None:
            if deployment and not bridge:
                return  # Existing named-contract fleets do not use this bridge.
            raise FleetAdmissionError("worker_adoption_authorization_required")
        binding, phase, _ = saved
        if phase == "cancelled":
            if deployment and not bridge:
                return  # A cancelled preparation does not govern a normal named-contract fleet.
            raise FleetAdmissionError("worker_adoption_cancelled")
        if (
            scope["source_schema"] != binding.source_schema or scope["native_schema"] != binding.native_schema
            or scope["task_queue"] != binding.queue or identity["cluster_policy"] != binding.cluster_policy
        ):
            raise FleetAdmissionError("worker_adoption_scope_changed")
        if not deployment and not build:
            allowed = (
                not bridge and role["generation"] == binding.source_generation
                and identity["build"] == binding.source_sha
            )
        else:
            allowed = bridge and deployment == binding.deployment and (
                phase == "active" and role["generation"] != binding.source_generation
                or phase in {"prepared", "source_registered"} and role["generation"] == binding.target_generation
                and build == binding.build and identity["build"] == binding.source_sha
            )
        if not allowed:
            raise FleetAdmissionError("worker_adoption_runtime_not_authorized")

    def activate(self, binding: AdoptionBinding, *, request_id: str) -> None:
        """Finalize only after source retirement and target acceptance.

        The controller separately proves routing/history/activity safety. This
        metadata operation cannot itself adopt or drain unversioned workflows.
        """
        with self.connection.transaction():
            self.connection.execute(self.registry.query("""
                SELECT fleet FROM {schema}.fleets WHERE fleet=%s FOR UPDATE
            """), (binding.fleet,)).fetchone()
            saved = self.load(binding.fleet, lock=True)
            if saved is None or saved[0] != binding or saved[2] != request_id:
                raise FleetAdmissionError("worker_adoption_intent_changed")
            if saved[1] == "active":
                return
            if saved[1] != "source_registered":
                raise FleetAdmissionError("worker_adoption_source_unregistered")
            rows = self.connection.execute(self.registry.query("""
                SELECT generation,role,state,source_sha,worker_deployment,worker_build
                FROM {schema}.roles WHERE fleet=%s AND generation=ANY(%s) FOR SHARE
            """), (binding.fleet, [binding.source_generation, binding.target_generation])).fetchall()
            expected = {
                (generation, role, state, binding.source_sha, deployment, build)
                for generation, state, deployment, build in (
                    (binding.source_generation, "retired", "", ""),
                    (binding.target_generation, "accepting", binding.deployment, binding.build),
                )
                for role in ("api", "broker", "coordinator", "worker")
            }
            if set(rows) != expected:
                raise FleetAdmissionError("worker_adoption_retirement_unconfirmed")
            self.connection.execute(self.registry.query("""
                UPDATE {schema}.worker_adoption SET phase='active'
                WHERE fleet=%s AND binding_digest=%s AND request_id=%s
            """), (binding.fleet, binding.digest, request_id))

    def verify_checkpoint_seal(
        self, *, fleet: str, generation: str, source_sha: str, request_id: str, proof: dict,
    ) -> None:
        """Qualify initial retirement without inventing Temporal version drainage.

        Called inside the registry seal transaction. The native pause row orders
        the seal against resume; resume checks the resulting live sealed members.
        The runtime independently checks saved-work safety before stopping polls.
        """
        saved = self.load(fleet, lock=True)
        if saved is None or saved[1] != "source_registered":
            raise FleetAdmissionError("worker_adoption_checkpoint_not_registered")
        binding = saved[0]
        if (
            generation != binding.source_generation or source_sha != binding.source_sha
            or proof["target_generation"] != binding.target_generation
            or proof["deployment"] != binding.deployment or proof["build"] != binding.build
            or proof["request_key"] != request_id
        ):
            raise FleetAdmissionError("worker_adoption_checkpoint_binding_changed")
        scope = self.registry.scope(binding.source_schema, lock=True)
        if scope is None or not scope["enabled"] or any(scope[key] != value for key, value in {
            "fleet": fleet, "native_schema": binding.native_schema, "task_queue": binding.queue,
        }.items()):
            raise FleetAdmissionError("worker_adoption_checkpoint_scope_changed")
        paused = self.connection.execute(sql.SQL("""
            SELECT generation,paused,request_key,request_fingerprint,source_policy
            FROM {}.moyai_product_drain_state WHERE id=1 FOR SHARE
        """).format(sql.Identifier(binding.native_schema))).fetchone()
        if paused != (
            proof["generation"], True, request_id, proof["request_fingerprint"], binding.cluster_policy,
        ):
            raise FleetAdmissionError("worker_adoption_checkpoint_pause_changed")
        self.connection.execute("SELECT pg_stat_clear_snapshot()")
        target = self.connection.execute(self.registry.query("""
            SELECT 1 FROM {schema}.roles r JOIN {schema}.members m
                ON (r.fleet,r.generation,r.role)=(m.fleet,m.generation,m.role)
            JOIN pg_stat_activity a ON a.pid=m.owner_pid AND a.backend_start=m.owner_started
            WHERE r.fleet=%s AND r.generation=%s AND r.role='worker'
                AND r.state IN ('preparing','accepting') AND r.source_sha=%s
                AND r.worker_deployment=%s AND r.worker_build=%s AND r.cluster_policy=%s
                AND m.service_id=r.service_id
        """), (fleet, binding.target_generation, binding.source_sha,
               binding.deployment, binding.build, binding.cluster_policy)).fetchone()
        if target is None:
            raise FleetAdmissionError("worker_adoption_checkpoint_target_missing")
