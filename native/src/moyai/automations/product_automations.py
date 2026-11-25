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
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, cast
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from app.automation_definitions import Definition, Trigger
    from app.connector_errors import ConnectorError
    from app.repository_identity import repository_origin, repository_provider
    from fastapi import APIRouter, HTTPException, Query, Request
    from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError, field_validator
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession
    from temporalio.client import Client
    from temporalio.exceptions import ApplicationError

    from ..continuations.product_continuation_models import ProductContinuation, ProductExecutionContext
    from ..continuations.product_continuation_workspace import fingerprint as workspace_fingerprint
    from ..contracts import RunScope
    from ..models import Effect, Session, Turn, User, now
    from ..product_config import CORE_IDENTITY
    from ..product_identity import ProductGrant, ProductPrincipal
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
    from .product_automation_models import ProductAutomationTurn

MAX_TRIGGERS = 16
TERMINAL = ("completed", "failed", "stopped", "included")
OLD_TERMINAL = ("idle", "completed", "failed", "cancelled", "interrupted")
AUTOMATION_ID = r"^[0-9a-f]{32}$"
REQUEST_KEY = r"^[A-Za-z0-9_-]{8,80}$"
Action = Literal["create", "update", "migrate", "enable", "pause", "retire"]
BindingState = Literal["active", "paused", "deleted"]
EMPTY_ATTENTION_PAUSE = "google_empty_pause_generation"
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


class AttentionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    owner_id: str
    revision: int
    definition: Json[Definition]


# Response models. The generated frontend client is built from these; every
# key a handler returns must be declared here or FastAPI drops it.
_OUTPUT = ConfigDict(json_schema_serialization_defaults_required=True)


class ProductAutomationTrigger(Trigger):
    model_config = _OUTPUT


class ProductAutomationDefinition(Definition):
    """A saved canonical definition as returned to the editor."""

    model_config = _OUTPUT


class ProductAutomationNativeState(BaseModel):
    model_config = _OUTPUT
    core: str
    generation: int
    state: Literal["paused", "active", "deleted"]
    sync_state: str
    synced_generation: int
    source_revision: int
    last_error: str


class ProductAutomationSummary(BaseModel):
    model_config = _OUTPUT
    id: str
    revision: int
    name: str
    owner_id: str
    owned: bool
    visibility: Literal["private", "shared"]
    legacy_paused: bool
    triggers: list[ProductAutomationTrigger]
    native: ProductAutomationNativeState | None


class ProductAutomationInventoryCapabilities(BaseModel):
    model_config = _OUTPUT
    create: bool
    read: bool


class ProductAutomationInventory(BaseModel):
    model_config = _OUTPUT
    automations: list[ProductAutomationSummary]
    next_offset: int | None
    source: Literal["canonical_product"]
    migration: Literal["opt_in"]
    event_choices: dict[str, list[tuple[str, str]]]
    capabilities: ProductAutomationInventoryCapabilities


class ProductAutomationCapabilities(BaseModel):
    model_config = _OUTPUT
    update: bool
    migrate: bool
    enable: bool
    pause: bool
    retire: bool
    run: bool
    webhook: bool
    test_event: bool


class ProductAutomationPreview(BaseModel):
    model_config = _OUTPUT
    id: str
    owner_id: str
    revision: int
    definition: ProductAutomationDefinition
    legacy_paused: bool
    native: ProductAutomationNativeState | None
    blockers: list[str]
    supported: bool
    activation: str
    target_core: str
    overlap: str
    catchup_seconds: int
    execution_visibility: Literal["private", "shared"]
    sharing_notice: str
    legacy_fence: str
    migration_changes: list[str]
    capabilities: ProductAutomationCapabilities


class ProductAutomationHistoryEntry(BaseModel):
    model_config = _OUTPUT
    occurrence: str
    revision: int
    outcome: str
    created_at: str
    engine: str
    status: str
    session_id: str | None
    turn_id: str | None
    detail: str


class ProductAutomationHistoryPage(BaseModel):
    model_config = _OUTPUT
    history: list[ProductAutomationHistoryEntry]
    next_cursor: str | None
    untrusted_reference: bool


class ProductAutomationMutationResult(BaseModel):
    model_config = _OUTPUT
    id: str
    revision: int
    outcome: str
    legacy_paused: bool
    native_state: str
    sync_state: str
    previous_harness: str | None = None


class ProductAutomationRunResult(BaseModel):
    model_config = _OUTPUT
    status: str
    session_id: str | None = None
    turn_id: str | None = None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def definition_payload(definition: Definition) -> dict[str, Any]:
    """Preserve existing receipts when new optional selections are absent."""
    payload = definition.model_dump(mode="json")
    if payload["kind"] == "task":
        payload.pop("kind")
    if payload["attention_mode"] == "focused":
        payload.pop("attention_mode")
    if not payload["attention_sources"]:
        payload.pop("attention_sources")
    for field in ("session_id", "gitlab_project_id"):
        if payload[field] is None:
            payload.pop(field)
    if payload["visibility"] == "shared":
        payload.pop("visibility")
    if not payload["connection_ids"]:
        payload.pop("connection_ids")
    for trigger in payload["triggers"]:
        event = trigger.get("event")
        if event is not None and not event.get("gitlab_origin"):
            event.pop("gitlab_origin", None)
    return payload


def definition_digest(definition: Definition) -> str:
    return _digest(definition_payload(definition))


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
    """Automation commands and admission; construction performs no I/O or activation."""

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
        from .product_automation_sync import ProductAutomationSync
        self._synchronization = ProductAutomationSync(self)

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

    async def _single_discovery(self, db: AsyncSession, owner: str, identity: str, definition: Definition) -> None:
        if definition.attention_mode != "discovery":
            return
        rows = (await db.execute(text(
            f"SELECT * FROM {self._table('automations')} WHERE owner_id=:owner AND id<>:id"
        ), {"owner": owner, "id": identity})).mappings()
        for row in rows:
            other = _definition(dict(row))
            binding = await db.get(Binding, row["id"])
            if other.attention_mode == "discovery" and (binding is None or binding.state != "deleted"):
                raise HTTPException(409, "Your Dot already has a discovery check. Edit or resume that check.")

    @staticmethod
    def _owner(row: dict[str, Any], principal: Any, *, pausing: bool = False) -> None:
        if _definition(row).visibility == "private" and row["owner_id"] != principal.id:
            raise HTTPException(404, "Automation not found.")
        if row["owner_id"] != principal.id and not (pausing and principal.role == "admin"):
            raise HTTPException(403, "Only the owner may manage this automation. Administrators may pause it.")

    @staticmethod
    def _readable(
        row: dict[str, Any], principal: Any, *, visibility: str | None = None,
    ) -> Definition:
        definition = _definition(row)
        if (
            definition.visibility == "private" and row["owner_id"] != principal.id
            or visibility is not None and definition.visibility != visibility
        ):
            raise HTTPException(404, "Automation not found.")
        return definition

    @staticmethod
    def _tool_target(scope: RunScope, definition: Definition) -> None:
        if definition.visibility != scope.authority.visibility:
            raise HTTPException(403, "Manage this automation from a chat with matching visibility.")

    @staticmethod
    def _attention_edit(scope: RunScope, definition: Definition, previous: Definition | None = None) -> None:
        if definition.kind != "attention":
            return
        retained = set(previous.connection_ids) if previous and previous.kind == "attention" else set()
        added = set(definition.connection_ids) - retained
        if not added.issubset(grant.id for grant in scope.grants):
            raise HTTPException(403, "Select each new attention account in this chat before adding it.")

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
        if definition.kind == "attention":
            if not definition.session_id:
                result.append("personal_dot_required")
            try:
                await self.database.dots.grants(db, row["owner_id"], definition.connection_ids)
            except HTTPException:
                result.append("personal_read_only_connections_with_explicit_tool_allowlists_required")
        if definition.session_id:
            try:
                await self.database.automation_session(
                    db, row["owner_id"], definition.session_id, visibility=definition.visibility,
                )
            except HTTPException:
                result.append("scheduled_chat_unavailable")
            if definition.kind == "attention":
                try:
                    await self.database.dots.own_session(db, row["owner_id"], definition.session_id)
                except HTTPException:
                    result.append("personal_dot_required")
        if definition.mode != "modal":
            result.append("execution_mode_not_supported")
        if len(definition.triggers) > MAX_TRIGGERS:
            result.append("too_many_triggers")
        if any(trigger.event is not None for trigger in definition.triggers):
            if self.events is None:
                result.append("event_trigger_adapter_required")
            else:
                result.extend(await self.events.blockers(db, row, definition))
        if (definition.plugins or definition.connection_ids) and not definition.session_id:
            if self.connections is None:
                result.append("connection_mapping_required")
            else:
                try:
                    project_grants = await self.connections.resolve_grants(
                        row["owner_id"], definition.connection_ids or definition.plugins,
                        shared_session=definition.visibility == "shared", surface="automation", db=db,
                    )
                except (HTTPException, ValueError):
                    result.append("selected_connections_require_onboarding_or_authorization")
        if definition.session_id:
            pass  # The existing chat supplies its computer and connected accounts.
        elif self.workspace is None:
            if definition.repo_url or definition.github_repository_id or definition.gitlab_project_id:
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
                        "gitlab_project_id": definition.gitlab_project_id,
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
        if definition.kind == "attention" and not definition.session_id:
            raise HTTPException(422, "Choose Your Dot as the attention delivery destination.")
        if previous is not None and definition.visibility != previous.visibility:
            raise HTTPException(409, "Automation visibility is fixed. Create a separate automation for another audience.")
        try:
            result = definition.model_copy(update={
                "model": self.settings.resolve_model(definition.model or None),
                "plugins": sorted(set(definition.plugins)),
                "connection_ids": sorted(set(definition.connection_ids)),
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

    async def _chat_context(
        self, db: AsyncSession, owner: str, session_id: str, *, visibility: str = "shared",
    ) -> tuple[list[str], dict]:
        session = await self.database.automation_session(db, owner, session_id, visibility=visibility)
        latest = await db.scalar(select(Turn).where(Turn.session_id == session.id).order_by(
            Turn.created_at.desc(), Turn.id.desc(),
        ).limit(1))
        connections = json.loads(latest.connections) if latest else []
        selected = await db.scalar(select(ProductExecutionContext).join(
            Turn, Turn.id == ProductExecutionContext.turn_id,
        ).where(Turn.session_id == session.id).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))
        if selected:
            workspace = json.loads(self.database.security.unseal(selected.descriptor))
        else:
            origin = await db.scalar(select(ProductContinuation).where(
                ProductContinuation.native_session_id == session.id,
            ))
            workspace = json.loads(self.database.security.unseal(origin.workspace))["context"] if origin else {}
        if selected and workspace_fingerprint(workspace) != selected.fingerprint:
            raise HTTPException(409, "The chat's saved project context changed.")
        return sorted(connections), workspace

    async def _capture_context(
        self, db: AsyncSession, binding: Binding, definition: Definition, principal: Any,
        *, capture: bool, previous_generation: int,
    ) -> None:
        if capture:
            connections = []
            grants = []
            if definition.kind == "attention":
                from ..database import grant_fingerprint
                grants = await self.database.dots.grants(db, binding.owner_id, definition.connection_ids)
                from ..dot_sources import repositories
                try:
                    repositories(grants, [source.model_dump() for source in definition.attention_sources])
                except ValueError as error:
                    raise HTTPException(409, str(error)) from None
                connections = sorted(grant.id for grant in grants)
                workspace = {"format": "moyai-attention-v1", "grants_key": grant_fingerprint(grants)}
            elif definition.session_id:
                connections, workspace = await self._chat_context(
                    db, binding.owner_id, definition.session_id, visibility=definition.visibility,
                )
                grants = await self.database.resolve_grants(
                    db, binding.owner_id, connections, shared_session=definition.visibility == "shared", surface="automation",
                )
            else:
                if definition.plugins or definition.connection_ids:
                    grants = await self.connections.resolve_grants(
                        binding.owner_id, definition.connection_ids or definition.plugins,
                        shared_session=definition.visibility == "shared", surface="automation", db=db,
                    )
                    connections = sorted({grant.id for grant in grants})
                workspace = await self.workspace.capture_workspace_context(db, principal, {
                    "repo_url": definition.repo_url, "github_repository_id": definition.github_repository_id,
                    "gitlab_project_id": definition.gitlab_project_id,
                    "environment_id": definition.environment_id,
                }) if self.workspace else {}
            if not self._repository_identity_known(workspace):
                raise HTTPException(409, "Verify the repository's permanent identity before enabling this automation.")
            if self.workspace is not None and definition.kind != "attention":
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
        if definition.kind == "attention":
            from ..database import grant_fingerprint
            if not definition.session_id:
                raise HTTPException(409, "The attention destination is missing.")
            await self.database.dots.own_session(db, binding.owner_id, definition.session_id)
            grants = await self.database.dots.grants(db, binding.owner_id, connections)
            from ..dot_sources import repositories
            repositories(grants, [source.model_dump() for source in definition.attention_sources])
            if (
                connections != sorted(definition.connection_ids)
                or workspace != {"format": "moyai-attention-v1", "grants_key": grant_fingerprint(grants)}
            ):
                raise HTTPException(409, "The attention accounts changed. Review and enable this schedule again.")
            return connections, workspace
        if workspace.get("format") == "moyai-automation-context-v2":
            event_authority, workspace = workspace["events"], workspace["workspace"]
        if not self._repository_identity_known(workspace):
            raise HTTPException(409, "This saved repository selection requires explicit owner reauthorization.")
        if any(trigger.event for trigger in definition.triggers):
            if self.events is None:
                raise HTTPException(409, "The canonical event adapter is unavailable.")
            await self.events.validate_authority(db, binding, definition, event_authority)
        if definition.session_id:
            selected, current_workspace = await self._chat_context(
                db, binding.owner_id, definition.session_id, visibility=definition.visibility,
            )
            if selected != connections or current_workspace != workspace:
                raise HTTPException(409, "The chat's accounts or project changed. Review and enable the schedule again.")
            await self.database.resolve_grants(
                db, binding.owner_id, connections, shared_session=definition.visibility == "shared", surface="automation",
            )
            return connections, workspace
        current = []
        if connections:
            if self.connections is None:
                raise HTTPException(409, "The canonical connection adapter is unavailable.")
            current = await self.connections.resolve_grants(
                binding.owner_id, connections, shared_session=definition.visibility == "shared", surface="automation", db=db,
            )
            if sorted({grant.id for grant in current}) != connections:
                raise HTTPException(409, "Selected connection authority changed.")
        if self.workspace is not None:
            workspace = await self.workspace.validate_workspace_context(db, principal, {
                "repo_url": definition.repo_url, "github_repository_id": definition.github_repository_id,
                "gitlab_project_id": definition.gitlab_project_id,
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

    async def preview(
        self, grant: ProductGrant, identity: str, *, shared_library: bool = False,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        principal = await self.identity.require_current(grant)
        async with self.database.sessions() as db:
            row = await self._row(db, identity)
            definition = self._readable(row, principal, visibility=visibility)
            owner = row["owner_id"] == principal.id
            if not shared_library:
                self._owner(row, principal)
            binding = await db.get(Binding, identity)
            blockers = await self.blockers(db, row, definition, principal=principal) if owner else ["owner_action_required"]
            result = {
                "id": identity, "owner_id": row["owner_id"], "revision": row["revision"],
                "definition": definition.model_dump(mode="json"), "legacy_paused": bool(row["paused"]),
                "native": self._binding_view(binding), "blockers": blockers,
                "supported": not blockers, "activation": "explicit_owner_action_required",
                "target_core": CORE_IDENTITY, "overlap": "ALLOW_ALL", "catchup_seconds": 900,
                "execution_visibility": definition.visibility,
                "sharing_notice": (
                    "Only you can view this automation and its private chats."
                    if definition.visibility == "private" else
                    "This automation and its scheduled chats are shared with your workspace."
                ),
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
                    "webhook": owner and definition.visibility == "shared",
                    "test_event": owner and definition.visibility == "shared",
                },
            }
        await self.identity.require_current(grant)
        return result

    async def listing(
        self, grant: ProductGrant, *, limit: int = 50, offset: int = 0, shared_library: bool = False,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50 or offset < 0:
            raise HTTPException(422, "Choose a bounded page.")
        principal = await self.identity.require_current(grant)
        async with self.database.sessions() as db:
            rows = (await db.execute(text(
                f"SELECT * FROM {self._table('automations')} "
                "WHERE (owner_id=:owner OR (:shared AND COALESCE(definition::jsonb->>'visibility','shared')='shared')) "
                "AND (CAST(:visibility AS text) IS NULL OR COALESCE(definition::jsonb->>'visibility','shared')=:visibility) "
                "ORDER BY updated_at DESC,id LIMIT :limit OFFSET :offset"
            ), {"owner": principal.id, "shared": shared_library, "visibility": visibility,
                "limit": limit + 1, "offset": offset})).mappings().all()
            values = []
            for row in rows[:limit]:
                definition = _definition(dict(row))
                values.append({
                    "id": row["id"], "revision": row["revision"], "name": definition.name,
                    "owner_id": row["owner_id"], "owned": row["owner_id"] == principal.id,
                    "visibility": definition.visibility,
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
            or principal.email.rpartition("@")[2] not in self.settings.identity_domains()
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
        session = await self.database.execution_session(db, scope.session_id, actor)
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
            session is None or session.deleted or session.visibility != scope.authority.visibility
            or (session.visibility == "private" and (session.owner_id != scope.actor_id or session.surface != "web"))
            or session.surface not in {"web", "slack"} or session.surface != scope.surface
            or session.active_turn != scope.turn_id
            or turn is None or turn.status != "running" or turn.actor_id != scope.actor_id
            or turn.parent_turn is not None
        ):
            raise HTTPException(403, "Automation management requires the original direct chat and its visibility.")
        if scope.authority.surface == "automation" or await self.automated_session(db, session.id) or await self.human_origin(db, scope) is None:
            raise HTTPException(403, "Delegated and automated work cannot create automation authority.")

    async def automated_session(self, db: AsyncSession, session_id: str) -> bool:
        session = await db.get(Session, session_id)
        if session and session.surface == "automation":
            return True
        original = await db.scalar(select(ProductContinuation.source_session_id).where(
            ProductContinuation.native_session_id == session_id, ProductContinuation.source_schema == self.schema,
        ))
        return bool(original and await db.scalar(text(
            f"SELECT 1 FROM {self._table('automation_runs')} a WHERE a.run_id=:id OR EXISTS ("
            f"SELECT 1 FROM {self._table('run_ancestry')} tree WHERE tree.run_id=:id AND tree.ancestor_id=a.run_id) LIMIT 1"
        ), {"id": original}))

    async def human_origin(self, db: AsyncSession, scope: RunScope) -> str | None:
        return await self.database.human_origin(db, scope)

    async def repository_preflight(
        self, grant: ProductGrant, action: Action, identity: str | None, body: Mutation,
        operation_scope: str, fingerprint: str, scope: RunScope | None,
    ) -> tuple[Definition | None, set[str]]:
        """Resolve stable IDs before the writer fence, never during an occurrence."""
        resolver = getattr(self.connections, "resolve_repository", None)
        if action not in {"create", "update", "migrate", "enable"} or not callable(resolver):
            return None, set()
        if isinstance(body, Save) and not (
            body.definition.repo_url or body.definition.github_repository_id or body.definition.gitlab_project_id
            or any(t.event and t.event.provider in {"github", "gitlab"} for t in body.definition.triggers)
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
            if scope is not None:
                self._tool_target(scope, definition)
            selected_providers = set(definition.plugins)
            if definition.connection_ids:
                selected_grants = await self.connections.resolve_grants(
                    principal.id, definition.connection_ids, shared_session=definition.visibility == "shared",
                    surface="automation", db=db,
                )
                selected_providers.update(item.kind for item in selected_grants)
        required: set[str] = set()

        async def resolve(
            repository: str, repository_id: int | None, *, selected: bool,
            provider: Literal["github", "gitlab"] = "github", base_url: str | None = None,
        ) -> dict:
            try:
                if provider == "gitlab":
                    value = await self.connections.resolve_repository(
                        grant, repository, repository_id, selected=selected, provider=provider, base_url=base_url,
                    )
                else:
                    value = await self.connections.resolve_repository(
                        grant, repository, repository_id, selected=selected,
                    )
            except (ConnectorError, TimeoutError, OSError):
                raise HTTPException(409, "The repository identity or selected access could not be verified.") from None
            target = value.get("repository_id")
            if type(target) is not int or target <= 0:
                raise HTTPException(409, "The provider did not confirm a permanent repository identity.")
            if repository_id is not None and target != repository_id:
                raise HTTPException(409, "The permanent repository identity changed.")
            if provider == "gitlab":
                try:
                    valid = (
                        bool(value.get("repo_url"))
                        and repository_provider(value["repo_url"]) == "gitlab"
                        and value.get("repository_provider") == "gitlab"
                        and value.get("gitlab_project_id") == target
                        and value.get("repository_origin") == repository_origin(value.get("repo_url", ""))
                        and (not base_url or value.get("repository_origin") == base_url)
                    )
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    raise HTTPException(409, "The GitLab project origin or identity changed.")
            if selected:
                connection_id = value.get("connection_id")
                if not isinstance(connection_id, str) or not connection_id:
                    raise HTTPException(409, "The repository has no current selected authority.")
                required.add(connection_id)
            return value

        if not definition.session_id and (definition.repo_url or definition.github_repository_id or definition.gitlab_project_id):
            if repository_provider(definition.repo_url) == "gitlab":
                if "gitlab" not in selected_providers:
                    raise HTTPException(422, "Select the GitLab connection for this automation's project.")
                value = await resolve(
                    definition.repo_url, definition.gitlab_project_id, selected=True,
                    provider="gitlab", base_url=repository_origin(definition.repo_url),
                )
                definition.repo_url, definition.gitlab_project_id = value["repo_url"], value["repository_id"]
            else:
                repository = definition.repo_url.removeprefix("https://github.com/").removesuffix(".git")
                value = await resolve(repository, definition.github_repository_id, selected="github" in selected_providers)
                definition.repo_url, definition.github_repository_id = value["repo_url"], value["repository_id"]
        for trigger in definition.triggers:
            if trigger.event and trigger.event.provider == "github":
                event = trigger.event
                value = await resolve(event.repository, event.repository_id, selected=True)
                event.repository, event.repository_id = value["repository"], value["repository_id"]
            elif trigger.event and trigger.event.provider == "gitlab":
                event = trigger.event
                value = await resolve(
                    event.repository, event.repository_id, selected=True, provider="gitlab",
                    base_url=event.gitlab_origin or None,
                )
                event.repository, event.repository_id = value["repository"], value["repository_id"]
                event.gitlab_origin = value["repository_origin"]
        if definition.connection_ids and not required.issubset(set(definition.connection_ids)):
            raise HTTPException(403, "Select the exact verified repository connection before saving.")
        await self.identity.require_current(grant)
        if scope is not None:
            await self.check_tool_caller(scope, scope.turn_id, grant)
        return Definition.model_validate(definition.model_dump()), required

    async def _store_definition(
        self, db: AsyncSession, identity: str, owner: str, definition: Definition,
        previous_revision: int | None,
    ) -> int:
        revision = 1 if previous_revision is None else previous_revision + 1
        values = {
            "id": identity, "owner": owner, "definition": definition.model_dump_json(),
            "revision": revision, "stamp": now(),
        }
        if previous_revision is None:
            await db.execute(text(
                f"INSERT INTO {self._table('automations')} "
                "(id,owner_id,definition,revision,paused,synced_revision,synced_schedule_version,"
                "sync_error,created_at,updated_at) VALUES (:id,:owner,:definition,1,1,0,0,'',:stamp,:stamp)"
            ), values)
        else:
            await db.execute(text(
                f"UPDATE {self._table('automations')} SET definition=:definition,paused=1,"
                "revision=:revision,updated_at=:stamp,sync_error='' WHERE id=:id AND owner_id=:owner"
            ), values)
        return revision

    async def _transition_binding(
        self, db: AsyncSession, binding: Binding, definition: Definition, principal: ProductPrincipal,
        revision: int, state: BindingState, *, capture: bool, revive: bool = False,
        invalid_context: Literal["raise", "pause"] = "raise",
    ) -> None:
        if binding.state == "deleted" and state != "deleted" and not revive:
            raise HTTPException(409, "Explicitly migrate this retired binding before enabling it again.")
        previous_generation = binding.generation or 0
        binding.generation = previous_generation + 1
        binding.source_revision = revision
        binding.definition_digest = definition_digest(definition)
        binding.state = state
        binding.sync_state, binding.last_error, binding.updated_at = "pending", "", now()
        try:
            if capture and definition.kind == "attention":
                if definition.session_id is None:
                    raise HTTPException(409, "The Dot conversation is unavailable.")
                await self.database.dots.own_session(db, principal.id, definition.session_id)
            await self._capture_context(
                db, binding, definition, principal, capture=capture, previous_generation=previous_generation,
            )
        except HTTPException as error:
            if invalid_context != "pause" or error.status_code not in {403, 404, 409}:
                raise
            binding.state = "paused"
            binding.last_error = "Review your Dot conversation and selected accounts before resuming attention."
            await self._capture_context(
                db, binding, definition, principal, capture=False, previous_generation=previous_generation,
            )
        if self.events is not None:
            await self.events.capture_activation(db, binding, definition)

    async def attention_records(self, db: AsyncSession, owner: str) -> tuple[AttentionRecord, ...]:
        rows = (await db.execute(text(
            f"SELECT id,owner_id,revision,definition FROM {self._table('automations')} WHERE owner_id=:owner"
        ), {"owner": owner})).mappings()
        records = tuple(AttentionRecord.model_validate(row) for row in rows)
        return tuple(record for record in records if record.definition.attention_mode == "discovery")

    async def reconcile_attention_accounts(
        self, db: AsyncSession, grant: ProductGrant, *, managed: frozenset[str],
        selected: frozenset[str], initial: Definition | None,
    ) -> str | None:
        """Apply consent in its transaction, preserving retirement and durable sync intent."""
        await self._writer(db)
        principal = await self.identity.authorize_admission(db, grant)
        if not principal.individual:
            raise HTTPException(403, "Proactive accounts require an individual sign-in.")
        records = await self.attention_records(db, principal.id)
        live: list[tuple[AttentionRecord, Binding | None]] = []
        for record in records:
            candidate = await db.get(Binding, record.id)
            if candidate is None or candidate.state != "deleted":
                live.append((record, candidate))
        if not live and (records or not selected):
            return None  # Retiring attention remains an explicit owner decision.
        if len(live) > 1:
            # Fence every conflicting schedule without rolling back account revocation.
            for record, candidate in live:
                if candidate is not None:
                    await self._transition_binding(
                        db, candidate, record.definition, principal, record.revision, "paused", capture=False,
                    )
                    candidate.last_error = "Review duplicate Dot attention checks before resuming."
            return live[0][0].id
        record, binding = live[0] if live else (None, None)
        if record is None:
            if initial is None:
                return None
            definition = self.normalize_definition(initial)
            identity = uuid4().hex
        else:
            identity = record.id
            definition = Definition.model_validate({
                **record.definition.model_dump(),
                "session_id": initial.session_id if initial is not None else record.definition.session_id,
            })
        capacity_error = False
        wanted = (set(definition.connection_ids) - managed) | selected
        try:
            definition = Definition.model_validate({
                **definition.model_dump(), "connection_ids": sorted(wanted),
                "attention_sources": [
                    source.model_dump() for source in definition.attention_sources
                    if source.connection_id in wanted
                ],
            })
        except ValidationError:
            # Remove revoked accounts even if newly opted-in accounts exceed capacity.
            # The owner can choose a valid subset before resuming.
            retained = set(definition.connection_ids) - (managed - selected)
            definition = Definition.model_validate({
                **definition.model_dump(), "connection_ids": sorted(retained),
                "attention_sources": [
                    source.model_dump() for source in definition.attention_sources
                    if source.connection_id in retained
                ],
            })
            capacity_error = True
        is_new = record is None
        previous_generation = (binding.generation or 0) if binding else 0
        was_active = (
            is_new or (binding is not None and binding.state == "active")
            or definition.metadata.get(EMPTY_ATTENTION_PAUSE) == str(previous_generation)
        )
        definition.metadata.pop(EMPTY_ATTENTION_PAUSE, None)
        if was_active and not definition.connection_ids:
            definition.metadata[EMPTY_ATTENTION_PAUSE] = str(previous_generation + 1)
        await self._single_discovery(db, principal.id, identity, definition)
        revision = await self._store_definition(
            db, identity, principal.id, definition, record.revision if record else None,
        )
        if binding is None and not is_new:
            return None  # A prepared definition still needs explicit activation.
        if binding is None:
            binding = Binding(
                id=identity, owner_id=principal.id, authority_version=grant.authority_version,
                source_revision=revision, generation=0, definition_digest=definition_digest(definition),
            )
            db.add(binding)
        binding.authority_version = grant.authority_version
        active = was_active and bool(definition.connection_ids) and not capacity_error
        await self._transition_binding(
            db, binding, definition, principal, revision, "active" if active else "paused",
            capture=active, invalid_context="pause",
        )
        if capacity_error:
            binding.last_error = "Choose at most four attention accounts before resuming."
        return identity

    async def mutate(
        self, grant: ProductGrant, action: Action, identity: str | None, body: Mutation,
        *, operation_scope: str = "http", scope: RunScope | None = None, effect: str | None = None,
        effect_attempt: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", operation_scope):
            raise HTTPException(422, "Invalid operation scope.")
        payload = body.model_dump(mode="json")
        if isinstance(body, Save):
            payload["definition"] = definition_payload(body.definition)
        fingerprint = _digest({"action": action, "id": identity, "body": payload})
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
                if scope is not None:
                    self._tool_target(scope, _definition(row))
                if row["revision"] != prior.source_revision:
                    raise HTTPException(409, "This action succeeded earlier but the automation has since changed.")
                result = json.loads(prior.result)
                identity = prior.automation_id
                touched_binding = await db.get(Binding, identity) is not None
                await self._confirm_effect(db, effect, scope, action, effect_attempt)
            else:
                if repository_grants:
                    current_grants = await self.connections.resolve_grants(
                        principal.id, sorted(repository_grants),
                        shared_session=identified is None or identified.visibility == "shared", surface="automation", db=db,
                    )
                    if not repository_grants.issubset({item.id for item in current_grants}):
                        raise HTTPException(409, "The selected repository authority changed before the automation was saved.")
                if action == "create":
                    if not isinstance(body, Save) or body.revision != 0:
                        raise HTTPException(409, "Creation requires a definition at revision zero.")
                    identity = uuid4().hex
                    definition = identified or self.normalize_definition(Definition.model_validate(body.definition.model_dump()))
                    if scope is not None:
                        self._tool_target(scope, definition)
                        self._attention_edit(scope, definition)
                    if definition.visibility == "private" and not principal.individual:
                        raise HTTPException(403, "Private automations require an individual account.")
                    await self._store_definition(db, identity, principal.id, definition, None)
                    row = await self._row(db, identity)
                else:
                    if identity is None:
                        raise HTTPException(422, "Select an automation.")
                    row = await self._row(db, identity)
                    self._owner(row, principal, pausing=action == "pause" and scope is None)
                    if row["revision"] != body.revision:
                        raise HTTPException(409, "The automation changed. Read its current revision.")
                    definition = identified or _definition(row)
                    if scope is not None:
                        self._tool_target(scope, _definition(row))
                assert identity is not None
                binding = await db.get(Binding, identity)
                previous_harness = _definition(row).harness
                if action == "update":
                    if not isinstance(body, Save):
                        raise HTTPException(422, "An update requires a complete definition.")
                    previous = definition
                    definition = identified or self.normalize_definition(Definition.model_validate(body.definition.model_dump()), definition)
                    if scope is not None:
                        self._tool_target(scope, definition)
                        self._attention_edit(scope, definition, previous)
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
                if action in {"create", "update"} and definition.session_id:
                    await self.database.automation_session(
                        db, principal.id, definition.session_id, visibility=definition.visibility,
                    )
                    if definition.kind == "attention":
                        await self.database.dots.own_session(db, principal.id, definition.session_id)
                if action in {"create", "update", "migrate", "enable"}:
                    await self._single_discovery(db, principal.id, identity, definition)
                revision = row["revision"] + (action != "create")
                if action != "create":
                    revision = await self._store_definition(
                        db, identity, row["owner_id"], definition, row["revision"],
                    )
                if binding is not None:
                    if binding.owner_id != row["owner_id"]:
                        raise HTTPException(409, "The migration binding owner changed.")
                    state: BindingState = (
                        "active" if action == "enable" or (action == "migrate" and isinstance(body, Migration) and body.activate)
                        else "deleted" if action == "retire" or (
                            binding.state == "deleted" and action in {"pause", "update"}
                        ) else "paused"
                    )
                    await self._transition_binding(
                        db, binding, definition, principal, revision, state,
                        capture=action in {"migrate", "enable"}, revive=action == "migrate",
                    )
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
        return await self._synchronization.request_sync(identity)

    async def dispatch_pending(self, *, limit: int = 20) -> int:
        """Recover schedules and events on the existing coordinator dispatcher."""
        count = await self._synchronization.dispatch_pending(limit=limit)
        if self.events is not None:
            await self.events.capture_sessions(limit=limit)
            count += await self.events.dispatch_pending(limit=limit)
            await self.events.reconcile_dispatched(limit=limit)
        return count

    async def reconcile(self, identity: str) -> dict[str, Any]:
        return await self._synchronization.reconcile(identity)

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
            if definition.attention_mode == "discovery" and await self.database.dots.discovery.active(db, definition.session_id):
                return await receipt(db, row, binding, trigger_id, occurrence, "skipped", "The previous discovery check is still running.")
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
                connections=connections, session_id=None if definition.kind == "attention" else definition.session_id,
                visibility=definition.visibility,
            )
            if definition.kind == "attention":
                assert definition.session_id is not None
                await self.database.dots.bind_attention(
                    db, turn, automation_id=identity, generation=binding.generation,
                    destination=definition.session_id, grants_key=workspace["grants_key"],
                    discovery=definition.attention_mode == "discovery",
                    sources=[source.model_dump() for source in definition.attention_sources],
                )
            elif self.workspace is not None:
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
                return {
                    "status": previous.status, "turn_id": previous.turn_id,
                    "session_id": await self.database.dots.delivery_session(db, previous.session_id, previous.turn_id),
                }
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
            if definition.attention_mode == "discovery" and await self.database.dots.discovery.active(db, definition.session_id):
                raise HTTPException(409, "The previous discovery check is still running.")
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
                session_id=None if definition.kind == "attention" else definition.session_id,
                visibility=definition.visibility,
            )
            if definition.kind == "attention":
                assert definition.session_id is not None
                await self.database.dots.bind_attention(
                    db, turn, automation_id=identity, generation=binding.generation,
                    destination=definition.session_id, grants_key=workspace["grants_key"],
                    discovery=definition.attention_mode == "discovery",
                    sources=[source.model_dump() for source in definition.attention_sources],
                )
            elif self.workspace is not None:
                await self.workspace.bind_automation_turn(db, session, turn, workspace)
            result = await self._receipt(
                db, row, binding, "manual", key, "admitted", "Admitted by the owner's explicit Run now request.",
                session_id=session.id, turn_id=turn.id,
            )
            result["session_id"] = await self.database.dots.delivery_session(db, session.id, turn.id)
        await self.identity.require_current(grant)
        return result

    async def history(
        self, grant: ProductGrant, identity: str, *, limit: int = 20, cursor: str = "",
        shared_library: bool = False,
        visibility: str | None = None,
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
            definition = self._readable(row, principal, visibility=visibility)
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
                if turn and turn.status == "resumed":
                    turn = await db.scalar(select(Turn).join(
                        ProductAutomationTurn, ProductAutomationTurn.turn_id == Turn.id,
                    ).where(ProductAutomationTurn.origin_turn_id == link.turn_id).order_by(
                        Turn.created_at.desc(), Turn.id.desc(),
                    ).limit(1)) or turn
                destination = await self.database.dots.delivery_session(
                    db, link.session_id, link.turn_id,
                ) if link else row["run_id"]
                session = await db.get(Session, destination) if link and destination else None
                if session and (session.deleted or (
                    session.visibility == "private"
                    and (session.owner_id != principal.id or definition.visibility != "private" or visibility == "shared")
                )):
                    continue
                from ..dot_models import DotAttentionTurn
                attention = await db.get(DotAttentionTurn, turn.id) if turn else None
                values.append({
                    "occurrence": row["occurrence"], "revision": row["revision"], "outcome": row["outcome"],
                    "created_at": row["created_at"], "engine": CORE_IDENTITY if link else "legacy",
                    "status": turn.status if turn else link.status if link else row["legacy_status"] or row["outcome"],
                    "session_id": destination, "turn_id": link.turn_id if link else None,
                    "detail": (
                        f"Attention check: {attention.outcome}."
                        if attention and attention.outcome else link.detail if link else ""
                    ),  # Historical provider diagnostics are not exported.
                })
        await self.identity.require_current(grant)
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            identity, rows[limit - 1]["created_at"], rows[limit - 1]["occurrence"],
        ]).encode()).decode() if len(rows) > limit else None
        return {"history": values, "next_cursor": next_cursor, "untrusted_reference": True}


@activity.defn(name="moyai_product_automation_reconcile_v1")
async def reconcile_product_automation(identity: str) -> dict[str, Any]:
    from ..runtime import current
    service = getattr(current(), "product_automations", None)
    if not isinstance(service, ProductAutomationService):
        raise TypeError("The product automation service is not configured.")
    return await service.reconcile(identity)


@activity.defn(name="moyai_product_automation_admit_v1")
async def admit_product_automation(
    identity: str, generation: int, revision: int, trigger_id: str, occurrence: str, scheduled_at: str,
) -> dict[str, Any]:
    from ..runtime import current
    service = getattr(current(), "product_automations", None)
    if not isinstance(service, ProductAutomationService):
        raise TypeError("The product automation service is not configured.")
    return await service.admit(
        identity, generation, revision, trigger_id, occurrence, scheduled_at,
    )


@activity.defn(name="moyai_product_automation_refused_v1")
async def refuse_product_automation(scope: RunScope, effect: str, name: str) -> str:
    from ..runtime import current
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

    @router.get("", operation_id="listProductAutomations")
    async def listing(
        request: Request, limit: int = Query(50, ge=1, le=50), offset: int = Query(0, ge=0),
    ) -> ProductAutomationInventory:
        actor = await principal(request)
        return ProductAutomationInventory.model_validate(
            await service.listing(actor.grant, limit=limit, offset=offset, shared_library=True),
        )

    @router.get("/{identity}/preview", operation_id="getProductAutomationPreview")
    async def preview(identity: str, request: Request) -> ProductAutomationPreview:
        return ProductAutomationPreview.model_validate(
            await service.preview((await principal(request)).grant, identity, shared_library=True),
        )

    @router.get("/{identity}/history", operation_id="listProductAutomationHistory")
    async def history(
        identity: str, request: Request, limit: int = Query(20, ge=1, le=100), cursor: str = "",
    ) -> ProductAutomationHistoryPage:
        return ProductAutomationHistoryPage.model_validate(await service.history(
            (await principal(request)).grant, identity, limit=limit, cursor=cursor, shared_library=True,
        ))

    @router.post("", operation_id="createProductAutomation")
    async def create(body: Save, request: Request) -> ProductAutomationMutationResult:
        return ProductAutomationMutationResult.model_validate(
            await service.mutate((await principal(request, mutation=True)).grant, "create", None, body),
        )

    @router.put("/{identity}", operation_id="updateProductAutomation")
    async def edit(identity: str, body: Save, request: Request) -> ProductAutomationMutationResult:
        return ProductAutomationMutationResult.model_validate(
            await service.mutate((await principal(request, mutation=True)).grant, "update", identity, body),
        )

    @router.post("/{identity}/migrate", operation_id="migrateProductAutomation")
    async def migrate(identity: str, body: Migration, request: Request) -> ProductAutomationMutationResult:
        return ProductAutomationMutationResult.model_validate(
            await service.mutate((await principal(request, mutation=True)).grant, "migrate", identity, body),
        )

    @router.post("/{identity}/run", status_code=202, operation_id="runProductAutomation")
    async def run_now(identity: str, body: Mutation, request: Request) -> ProductAutomationRunResult:
        return ProductAutomationRunResult.model_validate(
            await service.run_now((await principal(request, mutation=True)).grant, identity, body),
        )

    @router.post("/{identity}/{action}", operation_id="setProductAutomationState")
    async def state(
        identity: str, action: Literal["enable", "pause", "retire"], body: Mutation, request: Request,
    ) -> ProductAutomationMutationResult:
        return ProductAutomationMutationResult.model_validate(
            await service.mutate((await principal(request, mutation=True)).grant, action, identity, body),
        )

    if service.events is not None:
        # Event routes must precede /{identity}/{action} so POST .../webhook and
        # .../test-event never reach the action route.
        from .product_automation_events import routes as event_routes
        root = APIRouter()
        root.include_router(event_routes(service.events))
        root.include_router(router)
        return root
    return router
