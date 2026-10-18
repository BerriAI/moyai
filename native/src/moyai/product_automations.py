"""Opt-in canonical automation migration and native Temporal admission.

No legacy executor is instantiated. The product definition and occurrence
receipts remain canonical. A native binding stores routing/authority only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, cast
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from app.automation_definitions import Definition, Trigger
    from app.connector_errors import ConnectorError
    from cryptography.fernet import InvalidToken
    from fastapi import APIRouter, HTTPException, Query, Request
    from pydantic import BaseModel, ConfigDict, Field, field_validator
    from sqlalchemy import select, text, update
    from sqlalchemy.ext.asyncio import AsyncSession
    from temporalio.api.common.v1 import Payload
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleState,
        ScheduleUpdate,
    )
    from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
    from temporalio.service import RPCError, RPCStatusCode

    from .contracts import RunScope
    from .models import Effect, Message, Session, Turn, User, now
    from .product_automation_context_models import ProductAutomationExecutionContext as ExecutionContext
    from .product_automation_models import (
        ProductAutomationBinding as Binding,
    )
    from .product_automation_models import (
        ProductAutomationOccurrence as Occurrence,
    )
    from .product_automation_models import (
        ProductAutomationOperation as Operation,
    )
    from .product_config import CORE_IDENTITY
    from .product_continuation_models import ProductContinuation
    from .product_identity import ProductGrant

MAX_TRIGGERS = 16
RPC_TIMEOUT = timedelta(seconds=3)
TERMINAL = ("completed", "failed", "stopped", "included")
OLD_TERMINAL = ("idle", "completed", "failed", "cancelled", "interrupted")
AUTOMATION_ID = r"^[0-9a-f]{32}$"
REQUEST_KEY = r"^[A-Za-z0-9_-]{8,80}$"
Action = Literal["create", "update", "migrate", "enable", "pause", "retire"]
AUTOMATION_MUTATIONS = frozenset({
    f"automation_{action}" for action in (
        "create", "update", "migrate", "enable", "pause", "retire", "grant", "self_update", "claim_item", "webhook_setup",
    )
})


class AutomationDefinition(Definition):
    prompt: str = Field(min_length=3, max_length=500_000)
    triggers: list[Trigger] = Field(
        min_length=1, max_length=MAX_TRIGGERS,
        description="Every trigger must supply a stable id; do not generate different IDs on retries.",
    )

    @field_validator("triggers", mode="before")
    @classmethod
    def explicit_trigger_ids(cls, value):
        # Reject before Trigger's random default factory can run in a workflow.
        if isinstance(value, list) and any(isinstance(item, dict) and not item.get("id") for item in value):
            raise ValueError("Every trigger requires an explicit stable id.")
        return value


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=0)
    request_key: str = Field(pattern=REQUEST_KEY)


class Save(Mutation):
    definition: AutomationDefinition


class Migration(Mutation):
    activate: bool = False


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def definition_digest(definition: Definition) -> str:
    return _digest(definition.model_dump(mode="json"))


def _definition(row: dict[str, Any]) -> Definition:
    try:
        raw = json.loads(row["definition"])
        if "triggers" in raw and any(not item.get("id") for item in raw["triggers"]):
            raise ValueError("Missing stable trigger identity.")
        return Definition.model_validate(raw)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, "The saved definition needs validation in the product editor.") from None


def native_schedule_id(automation_id: str, trigger_id: str) -> str:
    return f"moyai-native-automation-{automation_id}-{trigger_id}"


def legacy_schedule_id(automation_id: str, trigger_id: str) -> str:
    return f"moyai-automation-{automation_id}" + ("" if trigger_id == "default" else f"-{trigger_id}")


def once_id(automation_id: str, trigger: Trigger) -> str:
    assert trigger.schedule is not None and trigger.schedule.run_at is not None
    return f"once:{automation_id}:{trigger.id}:{trigger.schedule.run_at.isoformat()}"


class ProductAutomationService:
    """Dependency-injected adapter. Construction performs no I/O or activation."""

    def __init__(
        self, database: Any, identity: Any, settings: Any, connect: Callable[[], Awaitable[Client]],
        *, connections: Any = None, workspace: Any = None, credentials: Any = None,
    ):
        if database.engine.dialect.name != "postgresql":
            raise ValueError("Product automation mutation requires PostgreSQL admission fencing.")
        schema = identity.schema
        if not schema or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("Select the canonical product schema.")
        self.database, self.identity, self.settings, self.connect = database, identity, settings, connect
        self.schema = schema
        self.queue = database.settings.temporal_queue
        self.connections, self.workspace = connections, workspace
        self.credentials = credentials
        self.events: Any = None
        from .product_automation_access import ProductAutomationAccess
        self.access = ProductAutomationAccess(self)

    @classmethod
    def from_runtime(cls, runtime: Any) -> ProductAutomationService | None:
        if runtime.product_identity is None:
            return None
        service = cls(
            runtime.database, runtime.product_identity, runtime.product_settings, runtime.connect,
            connections=getattr(runtime, "product_connections", None),
            workspace=getattr(runtime, "product_workspace", None),
            credentials=getattr(getattr(runtime, "product_secrets", None), "execution", None),
        )
        from .product_automation_events import ProductAutomationEvents
        service.events = ProductAutomationEvents(
            service, continuations=getattr(runtime, "product_continuations", None),
        )
        return service

    def _table(self, name: str) -> str:
        return f'"{self.schema}"."{name}"'

    async def _writer(self, db: AsyncSession) -> None:
        # Acquire exclusive before authorize_admission's shared lock. Never
        # upgrade shared->exclusive while a competing mutation can do the same.
        await db.execute(text(
            "SELECT pg_advisory_xact_lock(726941, CAST(to_regnamespace(:schema) AS oid)::int)"
        ), {"schema": self.schema})

    async def _row(self, db: AsyncSession, identity: str) -> dict[str, Any]:
        if not re.fullmatch(AUTOMATION_ID, identity):
            raise HTTPException(404, "Automation not found.")
        row = (await db.execute(text(
            f"SELECT * FROM {self._table('automations')} WHERE id=:id"
        ), {"id": identity})).mappings().first()
        if row is None:
            raise HTTPException(404, "Automation not found.")
        return dict(row)

    @staticmethod
    def _owner(row: dict[str, Any], principal: Any, *, pausing: bool = False) -> None:
        if row["owner_id"] != principal.id and not (pausing and principal.role == "admin"):
            raise HTTPException(403, "Only the owner may manage this automation. Administrators may pause it.")

    async def _old_active(self, db: AsyncSession, identity: str) -> bool:
        return bool(await db.scalar(text(
            f"SELECT 1 FROM {self._table('automation_runs')} a "
            f"JOIN {self._table('run_ancestry')} tree ON tree.ancestor_id=a.run_id "
            f"JOIN {self._table('runs')} r ON r.id=tree.run_id "
            "WHERE a.automation_id=:id AND r.status NOT IN "
            "('idle','completed','failed','cancelled','interrupted') LIMIT 1"
        ), {"id": identity}))

    async def blockers(
        self, db: AsyncSession, row: dict[str, Any], definition: Definition, *, principal: Any = None,
    ) -> list[str]:
        result = []
        project_grants = []
        if definition.mode != "modal":
            result.append("execution_mode_not_supported")
        if len(definition.triggers) > MAX_TRIGGERS:
            result.append("too_many_triggers")
        if any(trigger.event is not None for trigger in definition.triggers):
            if self.events is None:
                result.append("event_trigger_adapter_required")
            else:
                result.extend(await self.events.blockers(db, row, definition))
        if definition.plugins:
            if self.connections is None:
                result.append("connection_mapping_required")
            else:
                try:
                    project_grants = await self.connections.resolve_grants(
                        row["owner_id"], definition.plugins, shared_session=True, surface="automation", db=db,
                    )
                except (HTTPException, ValueError):
                    result.append("selected_connections_require_onboarding_or_authorization")
        if self.workspace is None:
            if definition.repo_url or definition.github_repository_id:
                result.append("repository_bootstrap_adapter_required")
            if definition.environment_id != "none":
                result.append("environment_adapter_required_choose_none_explicitly")
        else:
            workspace_blockers = await self.workspace.automation_blockers(db, row["owner_id"], definition)
            result.extend(workspace_blockers)
            if not workspace_blockers and principal is not None:
                try:
                    candidate = await self.workspace.capture_workspace_context(db, principal, {
                        "repo_url": definition.repo_url, "github_repository_id": definition.github_repository_id,
                        "environment_id": definition.environment_id,
                    })
                    if not self._repository_identity_known(candidate):
                        result.append("repository_identity_requires_verified_metadata")
                    else:
                        self.workspace.authorize_project_grants(candidate, project_grants)
                except HTTPException:
                    result.append("selected_environment_requires_its_current_repository_connection")
        if len(definition.prompt) > 500_000:
            result.append("prompt_exceeds_native_limit")
        try:
            model = self.settings.resolve_model(definition.model or None)
            if model not in self.database.settings.models:
                result.append("model_not_enabled_in_native_core")
        except ValueError:
            result.append("model_not_enabled")
        if await self._old_active(db, row["id"]):
            result.append("legacy_occurrences_must_finish")
        for trigger in definition.triggers:
            timing = trigger.schedule
            if timing and timing.frequency == "once":
                used = await db.scalar(text(
                    f"SELECT 1 FROM {self._table('automation_runs')} WHERE occurrence=:key"
                ), {"key": once_id(row["id"], trigger)})
                if not used and (timing.run_at is None or timing.run_at <= datetime.now(UTC)):
                    result.append(f"once_requires_future_time:{trigger.id}")
        return result

    def normalize_definition(self, definition: Definition, previous: Definition | None = None) -> Definition:
        """Preserve the canonical save contract without the old harness selector."""
        try:
            result = definition.model_copy(update={
                "model": self.settings.resolve_model(definition.model or None),
                "plugins": sorted(set(definition.plugins)),
            })
        except ValueError:
            raise HTTPException(422, "Choose an enabled product model.") from None
        old = {trigger.id: trigger for trigger in previous.triggers} if previous else {}
        for trigger in result.triggers:
            timing = trigger.schedule
            if timing and timing.frequency == "once" and timing.run_at is None:
                raise HTTPException(422, "A one-time trigger requires its execution time.")
            if timing and timing.frequency == "once" and timing.run_at and timing.run_at <= datetime.now(UTC):
                prior = old.get(trigger.id)
                if not prior or not prior.schedule or prior.schedule.frequency != "once" or prior.schedule.run_at != timing.run_at:
                    raise HTTPException(422, "Choose a future date and time for a one-time trigger.")
        return result

    @staticmethod
    def _context_fingerprint(
        identity: str, generation: int, owner: str, authority: str, connections: list[str], workspace: dict,
    ) -> str:
        return _digest({
            "automation_id": identity, "generation": generation, "owner_id": owner,
            "authority_version": authority, "connection_ids": connections, "workspace_context": workspace,
        })

    @staticmethod
    def _repository_identity_known(workspace: dict) -> bool:
        identity = workspace.get("repository_id")
        return not workspace.get("repo_url") or (type(identity) is int and identity > 0)

    async def _capture_context(
        self, db: AsyncSession, binding: Binding, definition: Definition, principal: Any,
        *, capture: bool, previous_generation: int,
    ) -> None:
        if capture:
            connections = []
            grants = []
            if definition.plugins:
                grants = await self.connections.resolve_grants(
                    binding.owner_id, definition.plugins, shared_session=True, surface="automation", db=db,
                )
                connections = sorted({grant.id for grant in grants})
            workspace = await self.workspace.capture_workspace_context(db, principal, {
                "repo_url": definition.repo_url, "github_repository_id": definition.github_repository_id,
                "environment_id": definition.environment_id,
            }) if self.workspace else {}
            if not self._repository_identity_known(workspace):
                raise HTTPException(409, "Verify the repository's permanent identity before enabling this automation.")
            if self.workspace is not None:
                self.workspace.authorize_project_grants(workspace, grants)
            if any(trigger.event for trigger in definition.triggers):
                workspace = {
                    "format": "moyai-automation-context-v2", "workspace": workspace,
                    "events": await self.events.capture_authority(db, binding, definition),
                }
        else:
            previous = await db.get(ExecutionContext, (binding.id, previous_generation))
            if previous is None:
                return  # Pausing an old binding never invents missing authority.
            connections, workspace = json.loads(previous.connection_ids), json.loads(previous.workspace_context)
        db.add(ExecutionContext(
            automation_id=binding.id, generation=binding.generation, owner_id=binding.owner_id,
            authority_version=binding.authority_version, connection_ids=json.dumps(connections),
            workspace_context=json.dumps(workspace),
            fingerprint=self._context_fingerprint(
                binding.id, binding.generation, binding.owner_id, binding.authority_version, connections, workspace,
            ),
        ))

    async def _execution_context(
        self, db: AsyncSession, binding: Binding, principal: Any, definition: Definition,
    ) -> tuple[list[str], dict]:
        saved = await db.get(ExecutionContext, (binding.id, binding.generation))
        if saved is None:
            raise HTTPException(409, "Execution selections require explicit owner reauthorization.")
        connections, workspace = json.loads(saved.connection_ids), json.loads(saved.workspace_context)
        if (
            saved.owner_id != binding.owner_id or saved.authority_version != binding.authority_version
            or saved.fingerprint != self._context_fingerprint(
                binding.id, binding.generation, binding.owner_id, binding.authority_version, connections, workspace,
            )
        ):
            raise HTTPException(409, "The saved execution authority is inconsistent.")
        event_authority = {}
        if workspace.get("format") == "moyai-automation-context-v2":
            event_authority, workspace = workspace["events"], workspace["workspace"]
        if not self._repository_identity_known(workspace):
            raise HTTPException(409, "This saved repository selection requires explicit owner reauthorization.")
        if any(trigger.event for trigger in definition.triggers):
            if self.events is None:
                raise HTTPException(409, "The canonical event adapter is unavailable.")
            await self.events.validate_authority(db, binding, definition, event_authority)
        current = []
        if connections:
            if self.connections is None:
                raise HTTPException(409, "The canonical connection adapter is unavailable.")
            current = await self.connections.resolve_grants(
                binding.owner_id, connections, shared_session=True, surface="automation", db=db,
            )
            if sorted({grant.id for grant in current}) != connections:
                raise HTTPException(409, "Selected connection authority changed.")
        if self.workspace is not None:
            workspace = await self.workspace.validate_workspace_context(db, principal, {
                "repo_url": definition.repo_url, "github_repository_id": definition.github_repository_id,
                "environment_id": definition.environment_id,
            }, workspace)
            self.workspace.authorize_project_grants(workspace, current)
        elif workspace:
            raise HTTPException(409, "The canonical workspace adapter is unavailable.")
        return connections, workspace

    @staticmethod
    def _binding_view(binding: Binding | None) -> dict[str, Any] | None:
        if binding is None:
            return None
        return {
            "core": CORE_IDENTITY, "generation": binding.generation, "state": binding.state,
            "sync_state": binding.sync_state, "synced_generation": binding.synced_generation,
            "source_revision": binding.source_revision, "last_error": binding.last_error,
        }

    async def preview(self, grant: ProductGrant, identity: str, *, shared_library: bool = False) -> dict[str, Any]:
        principal = await self.identity.require_current(grant)
        async with self.database.sessions() as db:
            row = await self._row(db, identity)
            owner = row["owner_id"] == principal.id
            if not shared_library:
                self._owner(row, principal)
            definition = _definition(row)
            binding = await db.get(Binding, identity)
            blockers = await self.blockers(db, row, definition, principal=principal) if owner else ["owner_action_required"]
            result = {
                "id": identity, "owner_id": row["owner_id"], "revision": row["revision"],
                "definition": definition.model_dump(mode="json"), "legacy_paused": bool(row["paused"]),
                "native": self._binding_view(binding), "blockers": blockers,
                "supported": not blockers, "activation": "explicit_owner_action_required",
                "target_core": CORE_IDENTITY, "overlap": "ALLOW_ALL", "catchup_seconds": 900,
                "execution_visibility": "shared",
                "sharing_notice": "Canonical definitions and scheduled sessions are shared product resources.",
                "legacy_fence": "pause_and_revision_plus_native_harness_marker",
                "migration_changes": ["pause_legacy", "set_native_harness", "capture_current_owner_grant"],
                "capabilities": {
                    "update": owner, "migrate": not blockers and (
                        binding is None or binding.sync_state == "blocked" or binding.state == "deleted"
                    ),
                    "enable": not blockers and binding is not None and binding.state != "deleted",
                    "pause": (owner or principal.role == "admin") and (
                        not row["paused"] or (binding is not None and binding.state == "active")
                    ),
                    "retire": owner and binding is not None and binding.state != "deleted",
                    "run": owner and binding is not None and binding.state != "deleted"
                    and binding.sync_state == "ready" and binding.source_revision == row["revision"],
                    "webhook": owner, "test_event": owner,
                },
            }
        await self.identity.require_current(grant)
        return result

    async def listing(
        self, grant: ProductGrant, *, limit: int = 50, offset: int = 0, shared_library: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50 or offset < 0:
            raise HTTPException(422, "Choose a bounded page.")
        principal = await self.identity.require_current(grant)
        async with self.database.sessions() as db:
            rows = (await db.execute(text(
                f"SELECT * FROM {self._table('automations')} WHERE (:shared OR owner_id=:owner) "
                "ORDER BY updated_at DESC,id LIMIT :limit OFFSET :offset"
            ), {"owner": principal.id, "shared": shared_library, "limit": limit + 1, "offset": offset})).mappings().all()
            values = []
            for row in rows[:limit]:
                definition = _definition(dict(row))
                values.append({
                    "id": row["id"], "revision": row["revision"], "name": definition.name,
                    "owner_id": row["owner_id"], "owned": row["owner_id"] == principal.id,
                    "legacy_paused": bool(row["paused"]),
                    "triggers": [trigger.model_dump(mode="json") for trigger in definition.triggers],
                    "native": self._binding_view(await db.get(Binding, row["id"])),
                })
        await self.identity.require_current(grant)
        from app.automation_sources import EVENT_CHOICES
        return {"automations": values, "next_offset": offset + limit if len(rows) > limit else None,
                "source": "canonical_product", "migration": "opt_in", "event_choices": EVENT_CHOICES,
                "capabilities": {"create": True, "read": True}}

    async def _confirm_effect(
        self, db: AsyncSession, effect: str | None, scope: RunScope | None, action: str,
        attempt: str | None,
    ) -> None:
        if effect is None:
            return
        row = await db.get(Effect, effect)
        if scope is None or row is None or (
            row.actor_id != scope.actor_id or row.turn_id != scope.turn_id
            or row.session_id != scope.session_id or row.tool_name != f"automation_{action}"
            or row.state not in {"pending", "confirmed"}
        ):
            raise HTTPException(409, "The automation mutation has no matching native effect reservation.")
        if row.state == "pending" and (
            attempt is None or not row.resolution or json.loads(
                self.database.security.unseal(row.resolution)
            ) != {"automation_phase": "checking", "attempt": attempt}
        ):
            raise HTTPException(409, "This tool attempt does not own the automation reservation.")
        row.state, row.settled_at = "confirmed", now()

    async def claim_tool_call(self, scope: RunScope, effect: str, name: str) -> str | None:
        """One activity attempt may enter. Never reset an earlier unknown attempt."""
        if name not in AUTOMATION_MUTATIONS:
            return None
        async with self.database.sessions.begin() as db:
            row = await db.scalar(select(Effect).where(Effect.id == effect).with_for_update())
            if row is None or (
                row.actor_id != scope.actor_id or row.session_id != scope.session_id
                or row.turn_id != scope.turn_id or row.tool_name != name
                or row.state != "pending" or row.resolution is not None
            ):
                return None
            attempt = uuid4().hex
            row.resolution = self.database.security.seal({
                "automation_phase": "checking", "attempt": attempt,
            })
        return attempt

    async def refused_tool_call(
        self, scope: RunScope, effect: str, name: str, attempt: str,
    ) -> Literal["not_dispatched", "confirmed", "unknown"]:
        """Only call after a trusted preflight refusal or acknowledged rollback.

        No current-authority check is needed to settle existing evidence. The
        ownership/attempt comparison forbids clearing another call or an earlier
        unknown attempt. A postcommit denial retains the atomic confirmed receipt.
        """
        async with self.database.sessions.begin() as db:
            row = await db.scalar(select(Effect).where(Effect.id == effect).with_for_update())
            if row is None or (
                row.actor_id != scope.actor_id or row.session_id != scope.session_id
                or row.turn_id != scope.turn_id or row.tool_name != name
            ):
                return "unknown"
            if row.state == "confirmed":
                return "confirmed"
            marker = json.loads(self.database.security.unseal(row.resolution)) if row.resolution else None
            if row.state not in {"pending", "not_dispatched"} or marker != {
                "automation_phase": "checking", "attempt": attempt,
            }:
                return "unknown"
            row.state, row.settled_at = "not_dispatched", now()
            return "not_dispatched"

    async def refused_before_tool(
        self, scope: RunScope, effect: str, name: str,
    ) -> Literal["not_dispatched", "confirmed", "unknown"]:
        """A trusted inbound interceptor rejected before this body was invoked."""
        if name not in AUTOMATION_MUTATIONS:
            return "unknown"
        async with self.database.sessions.begin() as db:
            row = await db.scalar(select(Effect).where(Effect.id == effect).with_for_update())
            if row is None or (
                row.actor_id != scope.actor_id or row.session_id != scope.session_id
                or row.turn_id != scope.turn_id or row.tool_name != name
            ):
                return "unknown"
            if row.state == "confirmed":
                return "confirmed"
            if row.state not in {"pending", "not_dispatched"} or row.resolution is not None:
                return "unknown"  # A prior body's unknown attempt is not this refusal.
            row.state, row.settled_at = "not_dispatched", now()
            return "not_dispatched"

    async def authorize_tool(
        self, scope: RunScope, run_id: str | None, *, db: AsyncSession | None = None,
    ) -> Any:
        """Preserve the original direct-chat and persisted private-sink rules."""
        if run_id != scope.root_run_id or scope.delegation_generated or scope.product_grant is None:
            raise HTTPException(403, "Manage automations from an authenticated direct user chat.")
        if db is None:
            async with self.database.sessions.begin() as transaction:
                await self._writer(transaction)
                return await self.authorize_tool(scope, run_id, db=transaction)
        await self.authorize_scope_in(db, scope)
        principal = await self.management_principal(db, scope)
        await self._tool_source(db, scope, lock=True)
        return principal

    async def management_principal(self, db: AsyncSession, scope: RunScope) -> Any:
        """Capture an individual only during an authenticated tool interaction."""
        if scope.product_grant is None:
            raise HTTPException(403, "A saved product requester is required.")
        principal = await self.identity.authorize_admission(db, scope.product_grant)
        if (principal.kind == "slack") != (scope.surface == "slack"):
            raise HTTPException(403, "The requester and persisted chat surface do not match.")
        allowed_local = principal.id == "shared:local:admin" and self.identity.legacy_security.local_preview()
        if not allowed_local:
            principal = await self.identity.individual_for_admission(db, scope.product_grant)
        if not allowed_local and (
            not principal.individual or not self.settings.person_login_enabled()
            or principal.email.rpartition("@")[2] not in self.settings.google_domains()
        ):
            raise HTTPException(403, "Automation tools require an individual Google or Cloudflare identity.")
        return principal

    async def check_tool_caller(self, scope: RunScope, run_id: str | None, grant: ProductGrant) -> None:
        if (await self.authorize_tool(scope, run_id)).grant != grant:
            raise HTTPException(403, "The verified automation owner changed.")

    async def authorize_scope_in(self, db: AsyncSession, scope: RunScope) -> None:
        """Use the caller's fenced transaction; never acquire a second lock holder."""
        actor = await db.scalar(select(User).where(User.id == scope.actor_id).with_for_update())
        turn = await db.get(Turn, scope.turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise PermissionError("The execution requester is no longer authorized.")
        await self.database._check_product_turn(db, turn, scope.product_grant)
        cast(Any, actor).product_grant = scope.product_grant
        session = await self.database.allowed_session(db, scope.session_id, actor)
        if session.active_turn != turn.id or turn.status != "running" or turn.actor_id != actor.id:
            raise PermissionError("The execution was stopped or superseded.")
        current = await self.database.scope_grants(db, scope, session)
        if [(grant.id, grant.revision) for grant in current] != [(grant.id, grant.revision) for grant in scope.grants]:
            raise PermissionError("Selected connection authority changed.")

    async def _tool_source(self, db: AsyncSession, scope: RunScope, *, lock: bool) -> None:
        query = select(Session).where(Session.id == scope.session_id)
        session = await db.scalar(query.with_for_update() if lock else query)
        turn = await db.get(Turn, scope.turn_id)
        if (
            session is None or session.deleted or session.visibility != "shared"
            or session.surface not in {"web", "slack"} or session.surface != scope.surface
            or session.active_turn != scope.turn_id
            or turn is None or turn.status != "running" or turn.actor_id != scope.actor_id
            or turn.parent_turn is not None
        ):
            raise HTTPException(403, "Private, delegated and automated work cannot export into shared automations.")
        if await self.automated_session(db, session.id) or await self.human_origin(db, scope) is None:
            raise HTTPException(403, "Private, delegated and automated work cannot export into shared automations.")

    async def automated_session(self, db: AsyncSession, session_id: str) -> bool:
        if await db.scalar(select(Occurrence.occurrence).where(Occurrence.session_id == session_id).limit(1)):
            return True
        original = await db.scalar(select(ProductContinuation.source_session_id).where(
            ProductContinuation.native_session_id == session_id, ProductContinuation.source_schema == self.schema,
        ))
        return bool(original and await db.scalar(text(
            f"SELECT 1 FROM {self._table('automation_runs')} a WHERE a.run_id=:id OR EXISTS ("
            f"SELECT 1 FROM {self._table('run_ancestry')} tree WHERE tree.run_id=:id AND tree.ancestor_id=a.run_id) LIMIT 1"
        ), {"id": original}))

    async def human_origin(self, db: AsyncSession, scope: RunScope) -> str | None:
        """Follow native persisted resume evidence, never a client request-key prefix."""
        current = await db.get(Turn, scope.turn_id)
        seen = set()
        for _ in range(32):
            if current is None or current.id in seen or (
                current.session_id != scope.session_id or current.actor_id != scope.actor_id
                or current.parent_turn is not None
            ):
                return None
            seen.add(current.id)
            if await self.database._saved_product_grant(db, current) != scope.product_grant:
                return None
            authored = await db.scalar(select(Message.id).where(
                Message.turn_id == current.id, Message.role == "user", Message.author_id == scope.actor_id,
                Message.identity == f"user:{current.id}",
            ).limit(1))
            if authored is not None:
                return current.id
            if not current.deferred_results:
                return None
            try:
                results = json.loads(self.database.security.unseal(current.deferred_results))
            except (ValueError, TypeError, UnicodeError, RecursionError, InvalidToken):
                return None
            candidates = list(await db.scalars(select(Turn.id).where(
                Turn.session_id == scope.session_id, Turn.actor_id == scope.actor_id,
                Turn.status == "resumed", Turn.created_at <= current.created_at,
            ).order_by(Turn.created_at.desc()).limit(100)))
            previous = next((identity for identity in candidates if hashlib.sha256(json.dumps(
                {"previous": identity, "results": results}, sort_keys=True,
            ).encode()).hexdigest() == current.request_fingerprint), None)
            current = await db.get(Turn, previous) if previous else None
        return None

    async def repository_preflight(
        self, grant: ProductGrant, action: Action, identity: str | None, body: Mutation,
        operation_scope: str, fingerprint: str, scope: RunScope | None,
    ) -> tuple[Definition | None, set[str]]:
        """Resolve stable IDs before the writer fence, never during an occurrence."""
        resolver = getattr(self.connections, "resolve_repository", None)
        if action not in {"create", "update", "migrate", "enable"} or not callable(resolver):
            return None, set()
        if isinstance(body, Save) and not (
            body.definition.repo_url or body.definition.github_repository_id
            or any(t.event and t.event.provider == "github" for t in body.definition.triggers)
        ):
            return None, set()
        principal = await self.identity.require_current(grant)
        if scope is not None:
            await self.check_tool_caller(scope, scope.turn_id, grant)
        async with self.database.sessions() as db:
            previous = await db.get(Operation, (principal.id, operation_scope, body.request_key))
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise HTTPException(409, "This request key belongs to a different action.")
                # The fenced mutation recovers the recorded outcome. A retry
                # never depends on a fresh provider lookup after a known commit.
                return None, set()
            old = None
            if identity is not None:
                row = await self._row(db, identity)
                self._owner(row, principal)
                if row["revision"] != body.revision:
                    raise HTTPException(409, "The automation changed. Read its current revision.")
                old = _definition(row)
            if isinstance(body, Save):
                if action == "create" and body.revision != 0:
                    raise HTTPException(409, "Creation requires a definition at revision zero.")
                definition = self.normalize_definition(
                    Definition.model_validate(body.definition.model_dump()), old,
                )
            elif old is not None:
                definition = old
            else:
                raise HTTPException(422, "Select an automation.")
        required: set[str] = set()

        async def resolve(repository: str, repository_id: int | None, *, selected: bool) -> dict:
            try:
                value = await self.connections.resolve_repository(
                    grant, repository, repository_id, selected=selected,
                )
            except (ConnectorError, TimeoutError, OSError):
                raise HTTPException(409, "The repository identity or selected GitHub access could not be verified.") from None
            target = value.get("repository_id")
            if type(target) is not int or target <= 0:
                raise HTTPException(409, "GitHub did not confirm a permanent repository identity.")
            if repository_id is not None and target != repository_id:
                raise HTTPException(409, "The permanent repository identity changed.")
            if selected:
                connection_id = value.get("connection_id")
                if not isinstance(connection_id, str) or not connection_id:
                    raise HTTPException(409, "The repository has no current selected GitHub authority.")
                required.add(connection_id)
            return value

        if definition.repo_url or definition.github_repository_id:
            repository = definition.repo_url.removeprefix("https://github.com/").removesuffix(".git")
            value = await resolve(repository, definition.github_repository_id, selected="github" in definition.plugins)
            definition.repo_url, definition.github_repository_id = value["repo_url"], value["repository_id"]
        for trigger in definition.triggers:
            if trigger.event and trigger.event.provider == "github":
                event = trigger.event
                value = await resolve(event.repository, event.repository_id, selected=True)
                event.repository, event.repository_id = value["repository"], value["repository_id"]
        await self.identity.require_current(grant)
        if scope is not None:
            await self.check_tool_caller(scope, scope.turn_id, grant)
        return Definition.model_validate(definition.model_dump()), required

    async def mutate(
        self, grant: ProductGrant, action: Action, identity: str | None, body: Mutation,
        *, operation_scope: str = "http", scope: RunScope | None = None, effect: str | None = None,
        effect_attempt: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", operation_scope):
            raise HTTPException(422, "Invalid operation scope.")
        fingerprint = _digest({"action": action, "id": identity, "body": body.model_dump(mode="json")})
        identified, repository_grants = await self.repository_preflight(
            grant, action, identity, body, operation_scope, fingerprint, scope,
        )
        touched_binding = False
        async with self.database.sessions.begin() as db:
            await self._writer(db)
            principal = await self.identity.authorize_admission(db, grant)
            if scope is not None:
                caller = await self.authorize_tool(scope, scope.turn_id, db=db)
                if caller.grant != grant:
                    raise HTTPException(403, "The automation caller changed.")
            await self.identity.project_native_user(db, principal)
            prior = await db.get(Operation, (principal.id, operation_scope, body.request_key))
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise HTTPException(409, "This request key belongs to a different action.")
                row = await self._row(db, prior.automation_id)
                self._owner(row, principal, pausing=action == "pause" and scope is None)
                if row["revision"] != prior.source_revision:
                    raise HTTPException(409, "This action succeeded earlier but the automation has since changed.")
                result = json.loads(prior.result)
                identity = prior.automation_id
                touched_binding = await db.get(Binding, identity) is not None
                await self._confirm_effect(db, effect, scope, action, effect_attempt)
            else:
                if repository_grants:
                    current_grants = await self.connections.resolve_grants(
                        principal.id, ["github"], shared_session=True, surface="automation", db=db,
                    )
                    if not repository_grants.issubset({item.id for item in current_grants}):
                        raise HTTPException(409, "The selected GitHub authority changed before the automation was saved.")
                if action == "create":
                    if not isinstance(body, Save) or body.revision != 0:
                        raise HTTPException(409, "Creation requires a definition at revision zero.")
                    identity = uuid4().hex
                    definition = identified or self.normalize_definition(Definition.model_validate(body.definition.model_dump()))
                    stamp = now()
                    await db.execute(text(
                        f"INSERT INTO {self._table('automations')} "
                        "(id,owner_id,definition,revision,paused,synced_revision,synced_schedule_version,"
                        "sync_error,created_at,updated_at) VALUES (:id,:owner,:definition,1,1,0,0,'',:stamp,:stamp)"
                    ), {"id": identity, "owner": principal.id, "definition": definition.model_dump_json(), "stamp": stamp})
                    row = await self._row(db, identity)
                else:
                    if identity is None:
                        raise HTTPException(422, "Select an automation.")
                    row = await self._row(db, identity)
                    self._owner(row, principal, pausing=action == "pause" and scope is None)
                    if row["revision"] != body.revision:
                        raise HTTPException(409, "The automation changed. Read its current revision.")
                    definition = identified or _definition(row)
                assert identity is not None
                binding = await db.get(Binding, identity)
                previous_harness = _definition(row).harness
                if action == "update":
                    if not isinstance(body, Save):
                        raise HTTPException(422, "An update requires a complete definition.")
                    definition = identified or self.normalize_definition(Definition.model_validate(body.definition.model_dump()), definition)
                    definition.harness = CORE_IDENTITY if binding else previous_harness
                    if self.events is not None:
                        await self.events.remove_receivers(db, identity, definition)
                if action in {"migrate", "enable"}:
                    blockers = await self.blockers(db, row, definition, principal=principal)
                    if blockers:
                        raise HTTPException(409, {"message": "Migration is blocked.", "blockers": blockers})
                    if action == "enable" and binding is None:
                        raise HTTPException(409, "Preview and explicitly migrate this automation before enabling.")
                    if action == "enable" and binding is not None and binding.state == "deleted":
                        raise HTTPException(409, "Explicitly migrate this retired binding before enabling it again.")
                    definition.harness = CORE_IDENTITY
                    # Projection uses this authenticated interaction, never a
                    # scheduler occurrence, to establish the canonical native ID.
                    await self.identity.project_native_user(db, principal)
                    if binding is None:
                        binding = Binding(
                            id=identity, owner_id=principal.id, authority_version=grant.authority_version,
                            source_revision=row["revision"] + 1, generation=0,
                            definition_digest=definition_digest(definition),
                        )
                        db.add(binding)
                    binding.authority_version = grant.authority_version
                revision = row["revision"] + (action != "create")
                if action != "create":
                    await db.execute(text(
                        f"UPDATE {self._table('automations')} SET definition=:definition,paused=1,"
                        "revision=:revision,updated_at=:stamp,sync_error='' WHERE id=:id"
                    ), {"definition": definition.model_dump_json(), "revision": revision, "stamp": now(), "id": identity})
                if binding is not None:
                    if binding.owner_id != row["owner_id"]:
                        raise HTTPException(409, "The migration binding owner changed.")
                    previous_generation = binding.generation or 0
                    binding.generation = previous_generation + 1
                    binding.source_revision = revision
                    binding.definition_digest = definition_digest(definition)
                    binding.state = (
                        "active" if action == "enable" or (action == "migrate" and isinstance(body, Migration) and body.activate)
                        else "deleted" if action == "retire" or (
                            binding.state == "deleted" and action in {"pause", "update"}
                        ) else "paused"
                    )
                    binding.sync_state, binding.last_error, binding.updated_at = "pending", "", now()
                    await self._capture_context(
                        db, binding, definition, principal,
                        capture=action in {"migrate", "enable"}, previous_generation=previous_generation,
                    )
                    if self.events is not None:
                        await self.events.capture_activation(db, binding, definition)
                    touched_binding = True
                elif action == "retire":
                    raise HTTPException(409, "This automation has no native migration to retire.")
                result = {
                    "id": identity, "revision": revision, "outcome": "saved",
                    "legacy_paused": True, "native_state": binding.state if binding else "not_migrated",
                    "sync_state": "pending" if binding else "not_migrated",
                    "previous_harness": previous_harness if action == "migrate" else None,
                }
                db.add(Operation(
                    owner_id=principal.id, operation_scope=operation_scope, request_key=body.request_key,
                    fingerprint=fingerprint, automation_id=identity, source_revision=revision,
                    result=json.dumps(result),
                ))
                await self._confirm_effect(db, effect, scope, action, effect_attempt)
        # A saved canonical mutation remains successful when Temporal is down.
        # Pending intent is recovered by dispatch_pending on the existing owner.
        assert identity is not None
        if touched_binding:
            await self.request_sync(identity)
        await self.identity.require_current(grant)
        return result

    async def request_sync(self, identity: str) -> bool:
        try:
            client = await self.connect()
            await client.start_workflow(
                ProductAutomationSyncWorkflow.run, identity,
                id=f"moyai-product-automation-sync-{identity}", task_queue=self.queue,
                start_signal="changed", rpc_timeout=RPC_TIMEOUT,
            )
            return True
        except (RPCError, WorkflowAlreadyStartedError, TimeoutError, OSError):
            return False

    async def dispatch_pending(self, *, limit: int = 20) -> int:
        """Call from the existing coordinator dispatcher; this adds no timer."""
        async with self.database.sessions() as db:
            identities = list(await db.scalars(select(Binding.id).where(
                Binding.sync_state == "pending",
            ).order_by(Binding.updated_at).limit(min(limit, 100))))
        count = sum([await self.request_sync(identity) for identity in identities])
        if self.events is not None:
            await self.events.capture_sessions(limit=limit)
            count += await self.events.dispatch_pending(limit=limit)
            await self.events.reconcile_dispatched(limit=limit)
        return count

    @asynccontextmanager
    async def _sync_lock(self, identity: str):
        # Transaction-level remote-operation lock survives across RPC awaits and
        # is automatically released on connection loss. Mutations remain short.
        async with self.database.engine.begin() as connection:
            acquired = await connection.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
            ), {"key": f"moyai-product-automation-sync:{self.schema}:{identity}"})
            if not acquired:
                raise RuntimeError("Another automation synchronization is in progress.")
            yield

    async def _pause_remote(self, client: Client, identity: str) -> None:
        handle = client.get_schedule_handle(identity)
        try:
            await handle.pause(note="Canonical native migration fence", rpc_timeout=RPC_TIMEOUT)
            description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
            if not description.schedule.state.paused:
                raise RuntimeError("Schedule pause is not confirmed.")
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise

    async def reconcile(self, identity: str) -> dict[str, Any]:
        client = await self.connect()
        async with self._sync_lock(identity):
            for _ in range(3):
                async with self.database.sessions() as db:
                    row = await self._row(db, identity)
                    binding = await db.get(Binding, identity)
                    if binding is None:
                        return {"status": "unbound"}
                    definition = _definition(row)
                    generation, desired = binding.generation, binding.state
                    original = ProductGrant(binding.owner_id, binding.authority_version)
                    old_ids = list((await db.execute(text(
                        f"SELECT schedule_id FROM {self._table('automation_schedules')} WHERE automation_id=:id"
                    ), {"id": identity})).scalars())
                    old_ids += [legacy_schedule_id(identity, trigger.id) for trigger in definition.triggers if trigger.schedule]
                    previous_ids = json.loads(binding.schedule_ids)
                    consumed_once = {
                        trigger.id for trigger in definition.triggers
                        if trigger.schedule and trigger.schedule.frequency == "once"
                        and await db.scalar(text(
                            f"SELECT 1 FROM {self._table('automation_runs')} WHERE occurrence=:key"
                        ), {"key": once_id(identity, trigger)})
                    }
                    valid = (
                        row["owner_id"] == binding.owner_id
                        and bool(row["paused"]) and row["revision"] == binding.source_revision
                        and definition.harness == CORE_IDENTITY
                        and definition_digest(definition) == binding.definition_digest
                    )
                authorized = True
                try:
                    await self.identity.require_current(original)
                except HTTPException:
                    authorized = False
                if not valid or not authorized:
                    for schedule_id in previous_ids:
                        await self._pause_remote(client, schedule_id)
                    async with self.database.sessions.begin() as db:
                        await db.execute(update(Binding).where(
                            Binding.id == identity, Binding.generation == generation,
                        ).values(sync_state="blocked", last_error="Definition or original authority changed.", updated_at=now()))
                    return {"status": "blocked"}
                # Both the durable canonical fence and remote pause confirmation
                # precede any native unpause. A stale old coordinator still sees
                # paused/native-harness at its own admission transaction.
                for schedule_id in sorted(set(old_ids)):
                    await self._pause_remote(client, schedule_id)
                planned_ids = [
                    native_schedule_id(identity, trigger.id) for trigger in definition.triggers
                    if trigger.schedule and desired != "deleted"
                ]
                # Track intent BEFORE remote creation. A later generation can
                # delete a schedule even if creation succeeded but its ack was
                # lost, or an update removed its trigger during the RPC.
                async with self.database.sessions.begin() as db:
                    tracked = await db.scalar(select(Binding).where(Binding.id == identity).with_for_update())
                    assert tracked is not None
                    tracked.schedule_ids = json.dumps(sorted(set(json.loads(tracked.schedule_ids)) | set(planned_ids)))
                desired_ids = []
                for trigger in definition.triggers:
                    if trigger.schedule is None:
                        continue
                    schedule_id = native_schedule_id(identity, trigger.id)
                    if desired == "deleted":
                        continue
                    desired_ids.append(schedule_id)
                    once = trigger.schedule.frequency == "once"
                    used = trigger.id in consumed_once
                    schedule = Schedule(
                        action=ScheduleActionStartWorkflow(
                            ProductAutomationWorkflow.run,
                            args=[identity, generation, row["revision"], trigger.id],
                            id=f"moyai-native-automation-tick-{identity}-{trigger.id}",
                            task_queue=self.queue, execution_timeout=timedelta(minutes=20),
                        ),
                        spec=trigger.schedule.spec(),
                        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.ALLOW_ALL, catchup_window=timedelta(minutes=15)),
                        state=ScheduleState(
                            paused=desired != "active" or used, limited_actions=once and not used,
                            remaining_actions=1 if once and not used else 0,
                        ),
                    )
                    try:
                        await client.create_schedule(schedule_id, schedule, rpc_timeout=RPC_TIMEOUT)
                    except ScheduleAlreadyRunningError:
                        await client.get_schedule_handle(schedule_id).update(
                            lambda _, schedule=schedule: ScheduleUpdate(schedule), rpc_timeout=RPC_TIMEOUT,
                        )
                for stale in sorted(set(previous_ids) - set(desired_ids)):
                    try:
                        await client.get_schedule_handle(stale).delete(rpc_timeout=RPC_TIMEOUT)
                    except RPCError as error:
                        if error.status != RPCStatusCode.NOT_FOUND:
                            raise
                # Read back actual state. Only this ack can mark the intent ready.
                for schedule_id in desired_ids:
                    description = await client.get_schedule_handle(schedule_id).describe(rpc_timeout=RPC_TIMEOUT)
                    used = any(schedule_id == native_schedule_id(identity, key) for key in consumed_once)
                    if description.schedule.state.paused != (desired != "active" or used):
                        raise RuntimeError("Native schedule state is not confirmed.")
                    remote = description.schedule.action
                    if (
                        not isinstance(remote, ScheduleActionStartWorkflow)
                        or remote.workflow != "MoyaiProductAutomationV1" or remote.task_queue != self.queue
                    ):
                        raise RuntimeError("Native schedule action is not confirmed.")
                    arguments = list(remote.args)
                    if arguments and isinstance(arguments[0], Payload):
                        arguments = await client.data_converter.decode(arguments)
                    expected_trigger = next(
                        trigger.id for trigger in definition.triggers
                        if schedule_id == native_schedule_id(identity, trigger.id)
                    )
                    if arguments != [identity, generation, row["revision"], expected_trigger]:
                        raise RuntimeError("Native schedule generation is not confirmed.")
                async with self.database.sessions.begin() as db:
                    await self._writer(db)
                    current = await db.get(Binding, identity)
                    fresh = await self._row(db, identity)
                    if current and current.generation == generation and fresh["revision"] == row["revision"]:
                        current.schedule_ids = json.dumps(desired_ids)
                        current.synced_generation, current.sync_state = generation, "ready"
                        current.last_error, current.updated_at = "", now()
                        return {"status": "ready", "generation": generation}
            return {"status": "pending"}

    async def _receipt(
        self, db: AsyncSession, row: dict[str, Any], binding: Binding, trigger_id: str,
        occurrence: str, status: str, detail: str, *, session_id: str | None = None, turn_id: str | None = None,
        source_revision: int | None = None, generation: int | None = None,
    ) -> dict[str, Any]:
        revision = row["revision"] if source_revision is None else source_revision
        await db.execute(text(
            f"INSERT INTO {self._table('automation_runs')} "
            "(occurrence,automation_id,revision,run_id,outcome,detail,created_at) "
            "VALUES (:key,:id,:revision,NULL,:status,:detail,:stamp)"
        ), {"key": occurrence, "id": row["id"], "revision": revision,
            "status": "started" if turn_id else status, "detail": detail, "stamp": now()})
        db.add(Occurrence(
            occurrence=occurrence, automation_id=row["id"],
            generation=binding.generation if generation is None else generation,
            source_revision=revision, trigger_id=trigger_id, owner_id=binding.owner_id,
            session_id=session_id, turn_id=turn_id, status=status, detail=detail, created_at=now(),
        ))
        return {"status": status, "session_id": session_id, "turn_id": turn_id}

    async def admit(
        self, identity: str, generation: int, revision: int, trigger_id: str,
        occurrence: str, scheduled_at: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_:./+-]{1,512}", occurrence):
            raise ValueError("Invalid occurrence identity.")
        scheduled = datetime.fromisoformat(scheduled_at)
        if scheduled.tzinfo is None:
            raise ValueError("Schedule time must include its timezone.")
        if occurrence != f"native:{identity}:{trigger_id}:{scheduled.astimezone(UTC).isoformat()}":
            raise ValueError("Occurrence does not match its automation, trigger and scheduled time.")
        async with self.database.sessions.begin() as db:
            await self._writer(db)
            row = await self._row(db, identity)
            binding = await db.get(Binding, identity)
            if binding is None:
                return {"status": "unbound"}
            receipt = partial(self._receipt, source_revision=revision, generation=generation)
            previous = await db.get(Occurrence, occurrence)
            if previous is not None:
                if previous.automation_id != identity:
                    raise ValueError("Occurrence belongs to another automation.")
                return {"status": previous.status, "session_id": previous.session_id, "turn_id": previous.turn_id}
            definition = _definition(row)
            trigger = next((item for item in definition.triggers if item.id == trigger_id and item.schedule), None)
            if (
                binding.state != "active" or binding.generation != generation
                or binding.owner_id != row["owner_id"]
                or binding.source_revision != revision or row["revision"] != revision or not row["paused"]
                or definition.harness != CORE_IDENTITY or definition_digest(definition) != binding.definition_digest
                or trigger is None
            ):
                return await receipt(db, row, binding, trigger_id, occurrence, "skipped", "Paused or changed before admission.")
            original = ProductGrant(binding.owner_id, binding.authority_version)
            try:
                principal = await self.identity.authorize_admission(db, original)
            except HTTPException:
                binding.sync_state = "blocked"
                binding.last_error = "Original owner authority is no longer current."
                return await receipt(db, row, binding, trigger_id, occurrence, "blocked", "Original owner authority is no longer current.")
            if datetime.now(UTC) - scheduled.astimezone(UTC) > timedelta(minutes=15):
                return await receipt(db, row, binding, trigger_id, occurrence, "skipped", "Occurrence exceeded the fifteen-minute catch-up window.")
            if binding.sync_state != "ready" or binding.synced_generation != generation:
                raise RuntimeError("Schedule synchronization is pending.")
            if await self._old_active(db, identity):
                return await receipt(db, row, binding, trigger_id, occurrence, "blocked", "A legacy occurrence is still active.")
            try:
                connections, workspace = await self._execution_context(db, binding, principal, definition)
            except (HTTPException, ValueError):
                return await receipt(db, row, binding, trigger_id, occurrence, "blocked", "Saved connections or workspace selections require owner attention.")
            # Same logical once key as the canonical scheduler, including old receipts.
            assert trigger.schedule is not None
            key = once_id(identity, trigger) if trigger.schedule.frequency == "once" else occurrence
            existing = (await db.execute(text(
                f"SELECT outcome FROM {self._table('automation_runs')} WHERE occurrence=:key"
            ), {"key": key})).first()
            if existing:
                linked = await db.get(Occurrence, key)
                return {"status": linked.status, "session_id": linked.session_id, "turn_id": linked.turn_id} if linked else {
                    "status": "already_recorded",
                }
            if definition.max_runs_per_hour is not None:
                count = await db.scalar(text(
                    f"SELECT COUNT(*) FROM {self._table('automation_runs')} WHERE automation_id=:id "
                    "AND outcome='started' AND created_at>:since"
                ), {"id": identity, "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat()})
                if count >= definition.max_runs_per_hour:
                    return await receipt(db, row, binding, trigger_id, key, "skipped", "Hourly invocation limit reached.")
            session, turn = await self.database.admit_automation(
                db, grant=original, title=f"Automation · {definition.name}",
                prompt=definition.prompt, model=self.settings.resolve_model(definition.model or None),
                request_key="automation-" + hashlib.sha256(key.encode()).hexdigest(),
                connections=connections,
            )
            if self.workspace is not None:
                await self.workspace.bind_automation_turn(db, session, turn, workspace)
            return await receipt(
                db, row, binding, trigger_id, key, "admitted", "Admitted to the native execution core.",
                session_id=session.id, turn_id=turn.id,
            )

    async def run_now(self, grant: ProductGrant, identity: str, body: Mutation) -> dict[str, Any]:
        """Owner-requested one-off admission; never enables a recurring schedule."""
        key = f"manual:{identity}:{body.request_key}"
        async with self.database.sessions.begin() as db:
            await self._writer(db)
            principal = await self.identity.authorize_admission(db, grant)
            row = await self._row(db, identity)
            self._owner(row, principal)
            previous = await db.get(Occurrence, key)
            if previous:
                return {"status": previous.status, "session_id": previous.session_id, "turn_id": previous.turn_id}
            binding = await db.get(Binding, identity)
            definition = _definition(row)
            if (
                binding is None or binding.state == "deleted" or row["revision"] != body.revision
                or binding.source_revision != body.revision or binding.owner_id != principal.id
                or binding.definition_digest != definition_digest(definition)
                or not row["paused"] or definition.harness != CORE_IDENTITY
                or binding.sync_state != "ready" or binding.synced_generation != binding.generation
            ):
                raise HTTPException(409, "Explicitly migrate and synchronize the current definition before running it.")
            original = ProductGrant(binding.owner_id, binding.authority_version)
            principal = await self.identity.authorize_admission(db, original)
            if await self._old_active(db, identity):
                raise HTTPException(409, "A legacy occurrence is still active.")
            if definition.max_runs_per_hour is not None and await db.scalar(text(
                f"SELECT count(*) FROM {self._table('automation_runs')} WHERE automation_id=:id "
                "AND outcome='started' AND created_at>:since"
            ), {"id": identity, "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}) >= definition.max_runs_per_hour:
                raise HTTPException(429, "The automation reached its hourly invocation limit.")
            connections, workspace = await self._execution_context(db, binding, principal, definition)
            session, turn = await self.database.admit_automation(
                db, grant=original, title=f"Automation · {definition.name}", prompt=definition.prompt,
                model=self.settings.resolve_model(definition.model or None),
                request_key="automation-" + hashlib.sha256(key.encode()).hexdigest(), connections=connections,
            )
            if self.workspace is not None:
                await self.workspace.bind_automation_turn(db, session, turn, workspace)
            result = await self._receipt(
                db, row, binding, "manual", key, "admitted", "Admitted by the owner's explicit Run now request.",
                session_id=session.id, turn_id=turn.id,
            )
        await self.identity.require_current(grant)
        return result

    async def history(
        self, grant: ProductGrant, identity: str, *, limit: int = 20, cursor: str = "",
        shared_library: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or len(cursor) > 2048:
            raise HTTPException(422, "Choose a bounded history page.")
        principal = await self.identity.require_current(grant)
        parameters: dict[str, Any] = {"id": identity, "limit": min(100, max(1, limit)) + 1}
        clause = ""
        if cursor:
            try:
                selected, stamp, occurrence = json.loads(base64.urlsafe_b64decode(cursor))
                if selected != identity or not isinstance(stamp, str) or not isinstance(occurrence, str):
                    raise ValueError
                parameters.update(stamp=stamp, occurrence=occurrence)
                clause = " AND (a.created_at<:stamp OR (a.created_at=:stamp AND a.occurrence<:occurrence))"
            except (ValueError, TypeError, UnicodeError):
                raise HTTPException(422, "Use a cursor returned for this automation.") from None
        async with self.database.sessions() as db:
            row = await self._row(db, identity)
            if not shared_library:
                self._owner(row, principal)
            rows = (await db.execute(text(
                f"SELECT a.occurrence,a.run_id,a.revision,a.outcome,a.detail,a.created_at,r.status AS legacy_status "
                f"FROM {self._table('automation_runs')} a LEFT JOIN {self._table('runs')} r ON r.id=a.run_id "
                "WHERE a.automation_id=:id AND COALESCE(r.private_owner_id,'')='' "
                "AND COALESCE(r.deleted_at,'')='' AND COALESCE(r.deletion_requested_at,'')=''" + clause
                + " ORDER BY a.created_at DESC,a.occurrence DESC LIMIT :limit"
            ), parameters)).mappings().all()
            values = []
            for row in rows[:limit]:
                link = await db.get(Occurrence, row["occurrence"])
                turn = await db.get(Turn, link.turn_id) if link and link.turn_id else None
                session = await db.get(Session, link.session_id) if link and link.session_id else None
                if session and (session.visibility == "private" or session.deleted):
                    continue
                values.append({
                    "occurrence": row["occurrence"], "revision": row["revision"], "outcome": row["outcome"],
                    "created_at": row["created_at"], "engine": CORE_IDENTITY if link else "legacy",
                    "status": turn.status if turn else link.status if link else row["legacy_status"] or row["outcome"],
                    "session_id": link.session_id if link else row["run_id"], "turn_id": link.turn_id if link else None,
                    "detail": link.detail if link else "",  # Historical provider diagnostics are not exported.
                })
        await self.identity.require_current(grant)
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            identity, rows[limit - 1]["created_at"], rows[limit - 1]["occurrence"],
        ]).encode()).decode() if len(rows) > limit else None
        return {"history": values, "next_cursor": next_cursor, "untrusted_reference": True}


@activity.defn(name="moyai_product_automation_reconcile_v1")
async def reconcile_product_automation(identity: str) -> dict[str, Any]:
    from .runtime import current
    service = getattr(current(), "product_automations", None)
    if not isinstance(service, ProductAutomationService):
        raise TypeError("The product automation service is not configured.")
    return await service.reconcile(identity)


@activity.defn(name="moyai_product_automation_admit_v1")
async def admit_product_automation(
    identity: str, generation: int, revision: int, trigger_id: str, occurrence: str, scheduled_at: str,
) -> dict[str, Any]:
    from .runtime import current
    service = getattr(current(), "product_automations", None)
    if not isinstance(service, ProductAutomationService):
        raise TypeError("The product automation service is not configured.")
    return await service.admit(
        identity, generation, revision, trigger_id, occurrence, scheduled_at,
    )


@activity.defn(name="moyai_product_automation_refused_v1")
async def refuse_product_automation(scope: RunScope, effect: str, name: str) -> str:
    from .runtime import current
    service = current().product_automations
    if service is None:
        raise RuntimeError("The product automation service is not configured.")
    return await service.refused_before_tool(scope, effect, name)


@workflow.defn(name="MoyaiProductAutomationSyncV1")
class ProductAutomationSyncWorkflow:
    def __init__(self):
        self.dirty = True

    @workflow.signal
    def changed(self) -> None:
        self.dirty = True

    @workflow.run
    async def run(self, identity: str) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "pending"}
        while self.dirty:
            self.dirty = False
            result = await workflow.execute_activity(
                reconcile_product_automation, identity, start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30)),
            )
            if result["status"] == "pending":
                await workflow.sleep(2)
                self.dirty = True
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(identity)
        return result


@workflow.defn(name="MoyaiProductAutomationV1")
class ProductAutomationWorkflow:
    @workflow.run
    async def run(self, identity: str, generation: int, revision: int, trigger_id: str) -> dict[str, Any]:
        scheduled = workflow.info().typed_search_attributes.get(
            SearchAttributeKey.for_datetime("TemporalScheduledStartTime"),
        )
        if scheduled is None:
            raise ApplicationError("A scheduled occurrence requires its Temporal schedule timestamp.", non_retryable=True)
        schedule_id = workflow.info().typed_search_attributes.get(
            SearchAttributeKey.for_keyword("TemporalScheduledById"),
        )
        if schedule_id != native_schedule_id(identity, trigger_id):
            raise ApplicationError("The occurrence does not belong to this native schedule.", non_retryable=True)
        occurrence = f"native:{identity}:{trigger_id}:{scheduled.astimezone(UTC).isoformat()}"
        return await workflow.execute_activity(
            admit_product_automation, args=[identity, generation, revision, trigger_id, occurrence, scheduled.isoformat()],
            start_to_close_timeout=timedelta(seconds=30), schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30)),
        )


with workflow.unsafe.imports_passed_through():
    from .product_automation_events import ProductAutomationEventWorkflow, admit_product_automation_event

PRODUCT_AUTOMATION_WORKFLOWS = [
    ProductAutomationSyncWorkflow, ProductAutomationWorkflow, ProductAutomationEventWorkflow,
]
PRODUCT_AUTOMATION_ACTIVITIES = [
    reconcile_product_automation, admit_product_automation, admit_product_automation_event, refuse_product_automation,
]


def routes(service: ProductAutomationService | None) -> APIRouter:
    router = APIRouter(prefix="/api/product/automations")
    if service is None:
        return router

    async def principal(request: Request, *, mutation: bool = False):
        return await service.identity.authenticate(request, mutation=mutation)

    @router.get("")
    async def listing(request: Request, limit: int = Query(50, ge=1, le=50), offset: int = Query(0, ge=0)):
        actor = await principal(request)
        return await service.listing(actor.grant, limit=limit, offset=offset, shared_library=True)

    @router.get("/{identity}/preview")
    async def preview(identity: str, request: Request):
        return await service.preview((await principal(request)).grant, identity, shared_library=True)

    @router.get("/{identity}/history")
    async def history(identity: str, request: Request, limit: int = Query(20, ge=1, le=100), cursor: str = ""):
        return await service.history((await principal(request)).grant, identity, limit=limit, cursor=cursor, shared_library=True)

    @router.post("")
    async def create(body: Save, request: Request):
        return await service.mutate((await principal(request, mutation=True)).grant, "create", None, body)

    @router.put("/{identity}")
    async def edit(identity: str, body: Save, request: Request):
        return await service.mutate((await principal(request, mutation=True)).grant, "update", identity, body)

    @router.post("/{identity}/migrate")
    async def migrate(identity: str, body: Migration, request: Request):
        return await service.mutate((await principal(request, mutation=True)).grant, "migrate", identity, body)

    @router.post("/{identity}/run", status_code=202)
    async def run_now(identity: str, body: Mutation, request: Request):
        return await service.run_now((await principal(request, mutation=True)).grant, identity, body)

    @router.post("/{identity}/{action}")
    async def state(identity: str, action: Literal["enable", "pause", "retire"], body: Mutation, request: Request):
        return await service.mutate((await principal(request, mutation=True)).grant, action, identity, body)

    if service.events is not None:
        from .product_automation_events import routes as event_routes
        root = APIRouter()
        root.include_router(event_routes(service.events))
        root.include_router(router)
        return root
    return router
