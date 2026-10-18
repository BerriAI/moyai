"""Source-owned admission schema for registered service generations.

Provisioning and role-state writes belong to the private release controller.
Runtime startup only reads admission and registers its exact owner lifetime.
No schema is created, adopted or repaired implicitly by an application process.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, LiteralString

from psycopg import sql

PROTOCOL = "moyai-fleet-admission-v1"
SCHEMA = "moyai_fleet_admission"
ROLES = {"api", "broker", "coordinator", "worker"}


class FleetAdmissionError(RuntimeError):
    """Fixed classifications without database URLs or provider data."""


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FleetRegistry:
    def __init__(self, connection: Any, *, schema: str = SCHEMA):
        if not re.fullmatch(r"moyai_fleet_admission(?:_[a-z0-9_]{1,32})?", schema):
            raise FleetAdmissionError("fleet_admission_schema_invalid")
        self.connection, self.schema = connection, schema

    def query(self, statement: LiteralString):
        return sql.SQL(statement).format(schema=sql.Identifier(self.schema))

    def installed(self) -> bool:
        row = self.connection.execute(
            "SELECT to_regclass(%s)", (self.schema + ".schema_version",),
        ).fetchone()
        if row[0] is None:
            return False
        version = self.connection.execute(self.query(
            "SELECT protocol FROM {schema}.schema_version WHERE id=1",
        )).fetchone()
        if version is None or version[0] != PROTOCOL:
            raise FleetAdmissionError("fleet_admission_protocol_unsupported")
        return True

    def initialize(self):
        """Explicit operator provisioning in a separate metadata schema."""
        with self.connection.transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.schema,),
            )
            if self.connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.schema,),
            ).fetchone():
                tables = {row[0] for row in self.connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=%s", (self.schema,),
                )}
                if tables != {"schema_version", "fleets", "roles", "members"} or not self.installed():
                    raise FleetAdmissionError("fleet_admission_schema_adoption_refused")
                return
            self.connection.execute(self.query("CREATE SCHEMA {schema}"))
            self.connection.execute(self.query("""
                CREATE TABLE {schema}.schema_version (
                    id integer PRIMARY KEY CHECK(id=1), protocol text NOT NULL
                )
            """))
            self.connection.execute(self.query("INSERT INTO {schema}.schema_version VALUES(1,%s)"), (PROTOCOL,))
            self.connection.execute(self.query("""
                CREATE TABLE {schema}.fleets (
                    fleet text PRIMARY KEY, source_schema text NOT NULL UNIQUE,
                    native_schema text NOT NULL, task_queue text NOT NULL,
                    enabled boolean NOT NULL DEFAULT false
                )
            """))
            self.connection.execute(self.query("""
                CREATE TABLE {schema}.roles (
                    fleet text NOT NULL REFERENCES {schema}.fleets(fleet),
                    generation text NOT NULL, role text NOT NULL,
                    service_name text NOT NULL, service_id text,
                    source_sha char(40) NOT NULL, cluster_policy char(64) NOT NULL,
                    role_policy char(64) NOT NULL, worker_deployment text NOT NULL,
                    worker_build text NOT NULL,
                    state text NOT NULL CHECK(state IN ('preparing','accepting','draining','retired')),
                    drain_request text NOT NULL DEFAULT '', drain_proof char(64),
                    PRIMARY KEY(fleet,generation,role),
                    UNIQUE(fleet,generation,service_name)
                )
            """))
            self.connection.execute(self.query("""
                CREATE UNIQUE INDEX active_service ON {schema}.roles(service_id)
                WHERE state!='retired'
            """))
            self.connection.execute(self.query("""
                CREATE TABLE {schema}.members (
                    fleet text NOT NULL, generation text NOT NULL, role text NOT NULL,
                    service_id text NOT NULL, instance_id text NOT NULL,
                    process_nonce char(32) NOT NULL, owner_pid integer NOT NULL,
                    owner_started timestamptz NOT NULL, identity_digest char(64) NOT NULL,
                    drain_request text NOT NULL DEFAULT '',
                    registered_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY(fleet,generation,role,instance_id,process_nonce),
                    FOREIGN KEY(fleet,generation,role) REFERENCES {schema}.roles(fleet,generation,role)
                )
            """))

    def scope(self, source_schema: str, *, lock=False) -> dict | None:
        if not self.installed():
            return None
        row = self.connection.execute(self.query("""
            SELECT fleet,source_schema,native_schema,task_queue,enabled FROM {schema}.fleets
            WHERE source_schema=%s
        """ + (" FOR SHARE" if lock else "")), (source_schema,)).fetchone()
        return dict(zip(("fleet", "source_schema", "native_schema", "task_queue", "enabled"), row)) if row else None

    def role(self, *, fleet: str, service_id: str, service_name: str, generation: str, lock=False) -> dict | None:
        rows = self.connection.execute(self.query("""
            SELECT fleet,generation,role,service_name,service_id,source_sha,cluster_policy,
                role_policy,worker_deployment,worker_build,state,drain_request
            FROM {schema}.roles WHERE fleet=%s AND (
                (service_id=%s AND state!='retired')
                OR (service_id IS NULL AND service_name=%s AND generation=%s AND state='preparing')
            )
        """ + (" FOR SHARE" if lock else "")), (fleet, service_id, service_name, generation)).fetchall()
        if len(rows) > 1:
            raise FleetAdmissionError("fleet_admission_service_ambiguous")
        return dict(zip((
            "fleet", "generation", "role", "service_name", "service_id", "source_sha", "cluster_policy",
            "role_policy", "worker_deployment", "worker_build", "state", "drain_request",
        ), rows[0])) if rows else None

    @staticmethod
    def validate(scope: dict, role: dict, *, identity: dict, native_schema: str,
                 queue: str, deployment: str, build: str, generation: str) -> None:
        if (
            scope["native_schema"] != native_schema or scope["task_queue"] != queue
            or identity["role"] not in ROLES or identity["schema"] != scope["source_schema"]
            or role["role"] != identity["role"] or role["source_sha"] != identity["build"]
            or role["cluster_policy"] != identity["cluster_policy"] or role["role_policy"] != identity["role_policy"]
            or role["worker_deployment"] != deployment or role["worker_build"] != build
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", deployment)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", build)
            or (generation and role["generation"] != generation)
        ):
            raise FleetAdmissionError("fleet_admission_contract_changed")

    def register(self, *, source_schema: str, service_id: str, instance_id: str,
                 service_name: str, generation: str, identity: dict, native_schema: str,
                 queue: str, deployment: str, build: str) -> dict:
        """Caller commits this transaction before allowing any role polling.

        The shared role-row lock orders registration before or after an operator's
        exclusive seal. A seal captures all earlier members; later starts fail.
        """
        if (
            not re.fullmatch(r"srv-[a-z0-9]{10,40}", service_id)
            or not instance_id.startswith(service_id + "-")
            or not re.fullmatch("[a-z0-9]{5}", instance_id.removeprefix(service_id + "-"))
            or not re.fullmatch("[a-f0-9]{32}", identity["instance"])
            or type(identity["owner_pid"]) is not int or identity["owner_pid"] <= 0
        ):
            raise FleetAdmissionError("fleet_admission_process_invalid")
        with self.connection.transaction():
            scope = self.scope(source_schema, lock=True)
            if scope is None or not scope["enabled"]:
                raise FleetAdmissionError("fleet_admission_not_enabled")
            role = self.role(fleet=scope["fleet"], service_id=service_id,
                             service_name=service_name, generation=generation, lock=True)
            if role is None or role["service_id"] != service_id or role["service_name"] != service_name:
                raise FleetAdmissionError("fleet_admission_service_unbound")
            self.validate(scope, role, identity=identity, native_schema=native_schema, queue=queue,
                          deployment=deployment, build=build, generation=generation)
            started = datetime.fromisoformat(identity["owner_started"])
            if started.tzinfo is None:
                raise FleetAdmissionError("fleet_admission_owner_invalid")
            # Exact backend lifetime and the canonical ownership lock, not age.
            self.connection.execute("SELECT pg_stat_clear_snapshot()")
            owned = self.connection.execute("""
                SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid
                WHERE a.pid=%s AND a.backend_start=%s AND l.locktype='advisory'
                    AND l.classid=726940 AND l.objid=%s::regnamespace::oid
                    AND l.objsubid=2 AND l.granted
            """, (identity["owner_pid"], started, source_schema)).fetchone()
            if owned is None:
                raise FleetAdmissionError("fleet_admission_owner_missing")
            key = (role["fleet"], role["generation"], role["role"], instance_id, identity["instance"])
            previous = self.connection.execute(self.query("""
                SELECT service_id,owner_pid,owner_started,identity_digest FROM {schema}.members
                WHERE fleet=%s AND generation=%s AND role=%s AND instance_id=%s AND process_nonce=%s
            """), key).fetchone()
            expected = (service_id, identity["owner_pid"], started, fingerprint(identity))
            if previous is not None:
                if tuple(previous) != expected:
                    raise FleetAdmissionError("fleet_admission_member_changed")
                # Reconcile a lost registration reply for this same still-live
                # owner, including when its role was sealed after the commit.
                # A restarted process has a new nonce/backend lifetime.
                return role
            if role["state"] not in {"preparing", "accepting"}:
                raise FleetAdmissionError("fleet_admission_service_sealed")
            self.connection.execute(self.query("""
                INSERT INTO {schema}.members (
                    fleet,generation,role,service_id,instance_id,process_nonce,owner_pid,owner_started,identity_digest
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(fleet,generation,role,instance_id,process_nonce) DO NOTHING
            """), (role["fleet"], role["generation"], role["role"], service_id, instance_id,
                  identity["instance"], identity["owner_pid"], started, fingerprint(identity)))
            row = self.connection.execute(self.query("""
                SELECT service_id,owner_pid,owner_started,identity_digest FROM {schema}.members
                WHERE fleet=%s AND generation=%s AND role=%s AND instance_id=%s AND process_nonce=%s
            """), key).fetchone()
            if tuple(row) != expected:
                raise FleetAdmissionError("fleet_admission_member_changed")
            return role

    def sealed_members(self, *, fleet: str, generation: str, role: str) -> list[dict]:
        rows = self.connection.execute(self.query("""
            SELECT m.service_id,m.instance_id,m.process_nonce,m.owner_pid,m.owner_started,m.identity_digest
            FROM {schema}.members m JOIN {schema}.roles r
                ON (r.fleet,r.generation,r.role)=(m.fleet,m.generation,m.role)
            WHERE m.fleet=%s AND m.generation=%s AND m.role=%s
                AND m.drain_request=r.drain_request AND m.drain_request!=''
            ORDER BY m.instance_id,m.process_nonce
        """), (fleet, generation, role)).fetchall()
        result = []
        for row in rows:
            value = dict(zip(("service_id", "instance_id", "process_nonce", "owner_pid", "owner_started", "identity_digest"), row))
            value["owner_started"] = value["owner_started"].isoformat()
            result.append(value)
        return result

    def seal(self, *, fleet: str, generation: str, role: str, service_id: str,
             request_id: str, proof: dict) -> list[dict]:
        """Operator-only operation; proof is already qualified by the controller."""
        if role not in ROLES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,199}", request_id):
            raise FleetAdmissionError("fleet_admission_seal_invalid")
        if not proof or not isinstance(proof, dict):
            raise FleetAdmissionError("fleet_admission_seal_proof_missing")
        with self.connection.transaction():
            row = self.connection.execute(self.query("""
                SELECT service_id,state,drain_request,drain_proof,worker_deployment,worker_build,source_sha
                FROM {schema}.roles
                WHERE fleet=%s AND generation=%s AND role=%s FOR UPDATE
            """), (fleet, generation, role)).fetchone()
            if row is None or row[0] != service_id:
                raise FleetAdmissionError("fleet_admission_seal_target_changed")
            self._seal_proof(
                proof, generation=generation, role=role, service_id=service_id,
                source_sha=row[6], deployment=row[4], build=row[5],
            )
            if row[1] in {"draining", "retired"}:
                if row[2] != request_id or row[3] != fingerprint(proof):
                    raise FleetAdmissionError("fleet_admission_seal_identity_changed")
            else:
                self.connection.execute("SELECT pg_stat_clear_snapshot()")
                self.connection.execute(self.query("""
                    UPDATE {schema}.roles SET state='draining',drain_request=%s,drain_proof=%s
                    WHERE fleet=%s AND generation=%s AND role=%s
                """), (request_id, fingerprint(proof), fleet, generation, role))
                # Capture an immutable set while the exclusive role lock
                # excludes new admissions. Owner disappearance after commit
                # cannot erase the evidence needed by a recovering controller.
                self.connection.execute(self.query("""
                    UPDATE {schema}.members m SET drain_request=%s
                    WHERE m.fleet=%s AND m.generation=%s AND m.role=%s
                        AND EXISTS(SELECT 1 FROM pg_stat_activity a
                            WHERE a.pid=m.owner_pid AND a.backend_start=m.owner_started)
                """), (request_id, fleet, generation, role))
            return self.sealed_members(fleet=fleet, generation=generation, role=role)

    @staticmethod
    def _seal_proof(proof: dict, *, generation, role, service_id, source_sha, deployment, build):
        try:
            if (
                set(proof) != {
                    "generation", "role", "service_id", "source_sha", "ingress_revision",
                    "ingress_digest", "ingress_instances", "ingress_observation_digest", "worker",
                }
                or proof["generation"] != generation or proof["role"] != role
                or proof["service_id"] != service_id or proof["source_sha"] != source_sha
                or type(proof["ingress_revision"]) is not int or proof["ingress_revision"] < 1
                or type(proof["ingress_instances"]) is not int or proof["ingress_instances"] < 2
                or not all(re.fullmatch("[a-f0-9]{64}", proof[key]) for key in (
                    "ingress_digest", "ingress_observation_digest",
                ))
            ):
                raise ValueError()
            worker = proof["worker"]
            if role != "worker":
                if worker is not None:
                    raise ValueError()
                return
            if (
                not isinstance(worker, dict)
                or set(worker) != {
                    "deployment", "build", "temporal_drained", "running", "drainage",
                    "drainage_checked", "routing_changed", "routing_fingerprint", "evidence_digest",
                }
                or worker["deployment"] != deployment or worker["build"] != build
                or worker["temporal_drained"] is not True or worker["running"] != []
                or worker["drainage"] != "VERSION_DRAINAGE_STATUS_DRAINED"
                or not all(re.fullmatch("[a-f0-9]{64}", worker[key]) for key in (
                    "routing_fingerprint", "evidence_digest",
                ))
            ):
                raise ValueError()
            checked = datetime.fromisoformat(worker["drainage_checked"])
            changed = datetime.fromisoformat(worker["routing_changed"])
            if checked.tzinfo is None or changed.tzinfo is None or checked < changed:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise FleetAdmissionError("fleet_admission_seal_proof_invalid") from None
