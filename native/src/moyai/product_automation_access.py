"""Scoped native automation authority; never grants general management to agents."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from app.automation_definitions import Definition
from fastapi import HTTPException
from sqlalchemy import select, text

from .contracts import RunScope
from .models import Session, Turn, now
from .product_automation_access_models import ProductAutomationChatGrant as ChatGrant
from .product_automation_models import ProductAutomationBinding as Binding
from .product_automation_models import ProductAutomationOccurrence as Occurrence
from .product_automation_models import ProductAutomationOperation as Operation
from .product_config import CORE_IDENTITY
from .product_identity import ProductGrant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .product_automations import ProductAutomationService


class ProductAutomationAccess:
    def __init__(self, service: ProductAutomationService):
        self.service = service

    async def source(
        self, db: AsyncSession, scope: RunScope, *, individual: bool = True,
    ) -> tuple[Any, Occurrence | None]:
        service = self.service
        await service.authorize_scope_in(db, scope)
        if scope.product_grant is None:
            raise HTTPException(403, "A current product requester is required.")
        principal = await service.identity.authorize_admission(db, scope.product_grant)
        session = await db.scalar(select(Session).where(Session.id == scope.session_id).with_for_update())
        turn = await db.get(Turn, scope.turn_id)
        if (
            session is None or session.deleted or session.visibility != "shared"
            or session.active_turn != scope.turn_id or turn is None or turn.actor_id != scope.actor_id
            or turn.status != "running" or turn.session_id != session.id or turn.parent_turn is not None
        ):
            raise HTTPException(403, "Scoped controls require the current shared execution.")
        if individual:
            principal = await service.management_principal(db, scope)
        occurrence = await db.scalar(select(Occurrence).where(Occurrence.session_id == scope.session_id))
        if occurrence is None:
            await service._tool_source(db, scope, lock=True)
        elif occurrence.owner_id != principal.id or occurrence.session_id != scope.session_id:
            raise HTTPException(403, "The automation execution owner changed.")
        return principal, occurrence

    async def targets(self, db: AsyncSession, scope: RunScope) -> dict[str, set[str]]:
        from .product_automations import _definition
        principal, occurrence = await self.source(db, scope)
        result: dict[str, set[str]] = {}
        if occurrence:
            row = await self.service._row(db, occurrence.automation_id)
            if row["owner_id"] == principal.id:
                result[row["id"]] = set(_definition(row).self_management.allowed())
        else:
            origin = await self.service.human_origin(db, scope)
            for grant in await db.scalars(select(ChatGrant).where(
                ChatGrant.turn_id == origin, ChatGrant.owner_id == principal.id,
            )):
                row = await self.service._row(db, grant.automation_id)
                if row["owner_id"] == principal.id:
                    result[grant.automation_id] = set(json.loads(grant.permissions))
        return {key: value for key, value in result.items() if value}

    async def read(self, scope: RunScope, identity: str = "") -> dict:
        from .product_automations import _definition
        async with self.service.database.sessions.begin() as db:
            await self.service._writer(db)
            targets = await self.targets(db, scope)
            if identity and identity not in targets:
                raise HTTPException(403, "No current owner grant for this automation.")
            rows = []
            for target in ([identity] if identity else targets):
                row = await self.service._row(db, target)
                rows.append({
                    "id": target, "revision": row["revision"], "permissions": sorted(targets[target]),
                    "definition": _definition(row).model_dump(mode="json"),
                    "native": self.service._binding_view(await db.get(Binding, target)),
                })
        await self.service.database.authorize_scope(scope)
        return {"turn_id": scope.turn_id, "automations": rows}

    async def authorize_history(self, scope: RunScope, run_id: str | None, identity: str) -> ProductGrant:
        async with self.service.database.sessions.begin() as db:
            await self.service._writer(db)
            principal, occurrence = await self.source(db, scope)
            if (run_id != scope.root_run_id or scope.delegation_generated) or occurrence:
                targets = await self.targets(db, scope)
                if identity not in targets and (occurrence is None or occurrence.automation_id != identity):
                    raise HTTPException(403, "Read history only for this execution's automation targets.")
            self.service._owner(await self.service._row(db, identity), principal)
        return principal.grant

    async def history(self, scope: RunScope, run_id: str | None, identity: str, *, cursor: str, limit: int) -> dict:
        if not identity:
            async with self.service.database.sessions.begin() as db:
                await self.service._writer(db)
                _, source = await self.source(db, scope)
                targets = {source.automation_id} if source else set(await self.targets(db, scope))
                if len(targets) != 1:
                    raise HTTPException(422, "Select one automation from the current execution's targets.")
                identity = next(iter(targets))
        grant = await self.authorize_history(scope, run_id, identity)
        result = await self.service.history(grant, identity, limit=limit, cursor=cursor)
        if await self.authorize_history(scope, run_id, identity) != grant:
            raise HTTPException(403, "The verified automation owner changed.")
        return {"turn_id": scope.turn_id, "automation_id": identity, **result}

    async def mutate(
        self, scope: RunScope, run_id: str | None, action: str, request: Any, effect: str, attempt: str,
    ) -> dict:
        from .product_automations import _definition, _digest, definition_digest
        service = self.service
        sync = False
        async with service.database.sessions.begin() as db:
            await service._writer(db)
            principal, source = await self.source(db, scope, individual=action != "claim_item")
            if action == "claim_item":
                if (run_id != scope.root_run_id or scope.delegation_generated):
                    raise HTTPException(403, "The root automation execution owns item claims.")
                return await self.claim(db, scope, source, request.item_key, effect, attempt)
            identity = request.automation_id
            row = await service._row(db, identity)
            service._owner(row, principal)
            if action == "grant":
                # No automated/delegated call may create owner authority.
                await service.authorize_tool(scope, run_id, db=db)
            elif action == "self_update":
                allowed = (await self.targets(db, scope)).get(identity, set())
                required = ({"instructions"} if request.prompt is not None else set()) | (
                    {"schedule"} if request.schedules else set()
                )
                if not required or not required.issubset(allowed):
                    raise HTTPException(403, "The owner has not granted this change for the current execution.")
            else:
                raise ValueError("Unknown scoped automation action.")
            operation_scope = f"session:{scope.session_id}"
            digest = _digest({"action": action, "request": request.model_dump(mode="json")})
            previous = await db.get(Operation, (principal.id, operation_scope, request.request_key))
            if previous:
                if previous.fingerprint != digest or previous.source_revision != row["revision"]:
                    raise HTTPException(409, "The earlier action or automation revision changed.")
                result = json.loads(previous.result)
            else:
                if row["revision"] != request.revision:
                    raise HTTPException(409, "The automation changed. Read its current revision.")
                definition = _definition(row)
                revision = row["revision"]
                if action == "grant":
                    origin = await service.human_origin(db, scope)
                    if origin is None:
                        raise HTTPException(403, "The current human authorization is unavailable.")
                    current = await db.get(ChatGrant, (identity, origin))
                    if current is None:
                        current = ChatGrant(automation_id=identity, turn_id=origin, owner_id=principal.id)
                        db.add(current)
                    current.permissions, current.updated_at = json.dumps(sorted(set(request.permissions))), now()
                    result = {"id": identity, "revision": revision, "permissions": sorted(set(request.permissions)),
                              "turn_id": scope.turn_id, "outcome": "saved"}
                else:
                    binding = await db.get(Binding, identity)
                    if binding is None or binding.state == "deleted" or binding.source_revision != revision:
                        raise HTTPException(409, "Scoped edits require an explicitly migrated current definition.")
                    await service.identity.authorize_admission(db, ProductGrant(binding.owner_id, binding.authority_version))
                    schedules = {t.id: t for t in definition.triggers if t.schedule}
                    if set(request.schedules or {}) - set(schedules):
                        raise HTTPException(403, "Change only existing schedules; event sources and membership are fixed.")
                    if request.prompt is not None:
                        definition.prompt = request.prompt
                    for key, timing in (request.schedules or {}).items():
                        schedules[key].schedule = timing
                    definition = Definition.model_validate(definition.model_dump())
                    blockers = await service.blockers(db, row, definition, principal=principal)
                    if blockers:
                        raise HTTPException(409, {"message": "The scoped update is blocked.", "blockers": blockers})
                    revision += 1
                    await db.execute(text(
                        f"UPDATE {service._table('automations')} SET definition=:definition,revision=:revision,"
                        "paused=1,updated_at=:stamp,sync_error='' WHERE id=:id"
                    ), {"id": identity, "definition": definition.model_dump_json(), "revision": revision, "stamp": now()})
                    previous_generation = binding.generation
                    binding.generation += 1
                    binding.source_revision, binding.definition_digest = revision, definition_digest(definition)
                    binding.sync_state, binding.last_error, binding.updated_at = "pending", "", now()
                    # Retain original authority and enabled/paused intent.
                    await service._capture_context(
                        db, binding, definition, principal, capture=False, previous_generation=previous_generation,
                    )
                    result = {"id": identity, "revision": revision, "outcome": "saved",
                              "native_state": binding.state, "sync_state": "pending"}
                    sync = True
                db.add(Operation(
                    owner_id=principal.id, operation_scope=operation_scope, request_key=request.request_key,
                    fingerprint=digest, automation_id=identity, source_revision=revision, result=json.dumps(result),
                ))
            await service._confirm_effect(db, effect, scope, action, attempt)
        if sync:
            await service.request_sync(identity)
        await service.database.authorize_scope(scope)
        return result

    async def claim(
        self, db: AsyncSession, scope: RunScope, source: Occurrence | None, item: str, effect: str, attempt: str,
    ) -> dict:
        """Preserve prior claims, including completed or failed source executions."""
        if source is None:
            raise HTTPException(403, "Item claims require a persisted automation occurrence.")
        service = self.service
        item = item.lower()
        legacy = (await db.execute(text(
            f"SELECT a.run_id,r.private_owner_id FROM {service._table('automation_items')} a "
            f"LEFT JOIN {service._table('runs')} r ON r.id=a.run_id "
            "WHERE a.automation_id=:id AND a.item_key=:key"
        ), {"id": source.automation_id, "key": item})).mappings().first()
        operation_scope = "item:" + source.automation_id
        key = hashlib.sha256(item.encode()).hexdigest()
        claimed = await db.get(Operation, (scope.actor_id, operation_scope, key))
        if legacy:
            result = {"claimed": False, "engine": "legacy",
                      "session_id": legacy["run_id"] if not legacy["private_owner_id"] else None}
        elif claimed:
            original = json.loads(claimed.result)
            session = await db.get(Session, original["session_id"])
            result = {"claimed": original["session_id"] == scope.session_id, "engine": CORE_IDENTITY,
                      "session_id": session.id if session and not session.deleted and session.visibility == "shared" else None}
        else:
            result = {"claimed": True, "engine": CORE_IDENTITY, "session_id": scope.session_id}
            db.add(Operation(
                owner_id=scope.actor_id, operation_scope=operation_scope, request_key=key,
                fingerprint=key, automation_id=source.automation_id, source_revision=source.source_revision,
                result=json.dumps(result),
            ))
        await service._confirm_effect(db, effect, scope, "claim_item", attempt)
        return result
