"""An explicit, fenced handoff of an existing logical session to the native core.

No legacy checkpoint, tool call, approval or model usage is replayed/imported.
The first new message is admitted by Database.submit in the SAME transaction
that freezes source execution and records the original-ID mapping.

Assembly (parent-owned):
    runtime.product_workspace = ProductWorkspaceCompatibility(runtime)
    runtime.product_continuations = ProductContinuationService(
        runtime, workspace=runtime.product_workspace)
Construct these BEFORE ProductAutomationService.from_runtime. Existing
product_routes.routes(runtime, actor) then includes the continuation router.

Worker, after database.prepare and before input hydration/model execution:
    prepared = await prepare_product_turn(runtime, prepared)
Allow this preparation activity 20 minutes (with a 30-second heartbeat timeout)
for verified snapshot/export/restore. Normal native turns remain a cheap no-op.
No RunScope schema extension or replacement agent/workflow engine is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import ConfigDict, Field
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from .contracts import NewMessage, PreparedTurn, RunScope
from .database import grant_fingerprint
from .models import Session, SessionFolder, SessionPreference, Turn, User, new_id, now
from .product_continuation_models import ProductContinuation
from .product_continuation_schema import identifier, session_lease_key
from .product_continuation_workspace import ProductWorkspaceCompatibility, fingerprint
from .product_history import session_id
from .product_identity import ProductPrincipal

if TYPE_CHECKING:
    from .runtime import Runtime

MAX_FAMILY = 1000
MAX_HISTORY_MESSAGES = 2000
MAX_HISTORY_BYTES = 1024 * 1024
TERMINAL = {"idle", "completed", "failed", "cancelled", "interrupted"}
PENDING_MESSAGES = {"queued", "running", "saving", "injected"}
IMPORT_MARKER = "moyai_product_public_history_v1"


async def prepare_product_turn(
    runtime: Runtime, prepared: PreparedTurn, *, recover_workspace: bool = False,
) -> PreparedTurn:
    """Existing native prepare activity owns the single pre-model transition."""
    continuation = getattr(runtime, "product_continuations", None)
    workspace = getattr(runtime, "product_workspace", None)
    if continuation is None or workspace is None:
        if runtime.settings.product_mode == "existing":
            raise RuntimeError("The product continuation/workspace adapters are not configured.")
        return prepared

    async def heartbeat():
        while True:
            activity.heartbeat("Verifying existing history and preparing its native workspace.")
            await asyncio.sleep(10)

    heartbeats = asyncio.create_task(heartbeat()) if activity.in_activity() else None
    try:
        prepared = await continuation.prepare(prepared)
        if recover_workspace:
            return await workspace.prepare(prepared, recover_workspace=True)
        return await workspace.prepare(prepared)
    finally:
        if heartbeats:
            heartbeats.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeats


class ContinuationRequest(NewMessage):
    model_config = ConfigDict(extra="forbid")
    expected_source_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    accept_recent_history: bool = False
    accept_workspace_omissions: bool = False
    drain_idle_workspace: bool = False

    def message(self) -> NewMessage:
        return NewMessage.model_validate(self.model_dump(include=set(NewMessage.model_fields)))


def public_conversation(messages: list[dict[str, Any]], scope: RunScope, source_id: str) -> Conversation:
    """Construct only typed public user/assistant parts. No arbitrary model JSON."""
    history = []
    for message in messages:
        metadata = {
            IMPORT_MARKER: True, "source_session_id": source_id,
            "source_message_id": message["id"], "source_author_id": message.get("user_id"),
        }
        content = message["content"]
        if message["role"] == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)], metadata=metadata))
        elif message["role"] == "user":
            state = message.get("status", "completed")
            label = "Earlier public message"
            if state not in {"completed", "done"}:
                label += f" ({state}; context only, do not replay)"
            history.append(ModelRequest(
                parts=[UserPromptPart(content=f"{label} by {message.get('user_id') or 'an earlier participant'}:\n{content}")],
                metadata=metadata,
            ))
    return Conversation(messages=history, conversation_id=scope.conversation_id)


class ProductContinuationService:
    def __init__(self, runtime: Runtime, *, workspace: ProductWorkspaceCompatibility | None = None):
        self.runtime = runtime
        identity, history = runtime.product_identity, runtime.product_history
        self.workspace = workspace or ProductWorkspaceCompatibility(runtime)
        if identity is None or history is None or not identity.schema:
            raise ValueError("Existing-product continuation requires canonical identity/history.")
        self.identity, self.history = identity, history
        self.source_schema = identity.schema
        self.native_schema = runtime.settings.database_schema
        identifier(self.source_schema)
        identifier(self.native_schema)
        self.history.continuation_access = self.allowed_sources
        self.history.continuation_schema = self.native_schema

    async def allowed_sources(self, db, principal, ids: list[str]) -> set[str]:
        """The mapped native privacy/deletion boundary also protects old files."""
        if not ids:
            return set()
        query = text(f"""
            SELECT r.run_id FROM {self.table('run_roots')} r
            LEFT JOIN {identifier(self.native_schema)}.moyai_product_continuations c
              ON c.source_schema=:schema AND c.source_session_id=r.root_id
            LEFT JOIN {identifier(self.native_schema)}.moyai_sessions s ON s.id=c.native_session_id
            WHERE r.run_id IN :ids AND (c.source_session_id IS NULL OR
              (s.deleted=false AND (s.visibility='shared' OR (s.owner_id=:actor AND :individual))))
        """).bindparams(bindparam("ids", expanding=True))
        return set((await db.execute(query, {
            "schema": self.source_schema, "ids": ids, "actor": principal.id, "individual": principal.individual,
        })).scalars())

    def table(self, name: str) -> str:
        return identifier(self.source_schema) + "." + identifier(name)

    async def _tables(self, db: AsyncSession) -> set[str]:
        return set((await db.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=:schema"
        ), {"schema": self.source_schema})).scalars())

    async def _root(self, db: AsyncSession, source_id: str, principal: ProductPrincipal) -> dict[str, Any]:
        session_id(source_id)
        row = (await db.execute(text(f"""
            SELECT id,owner_id,private_owner_id,parent_run_id,status,created_at,updated_at,
                display_title,agent_label,substr(prompt,1,300) AS prompt,model,harness,repo_url,
                github_repository_id,environment_id,environment_build_id,sandbox_provider,
                sandbox_id,snapshot_id,token_hash,checkpoint_error,deleted_at,deletion_requested_at
            FROM {self.table('runs')} WHERE id=:id
        """), {"id": source_id})).mappings().first()
        if (
            row is None or row["deleted_at"] or row["deletion_requested_at"]
            or not self.history.privacy.can_access(row, principal.id if principal.individual else "")
        ):
            raise HTTPException(404, "Session not found.")
        if row["parent_run_id"]:
            raise HTTPException(409, {
                "code": "continue_parent", "message": "Continue the parent session to preserve its child work.",
                "url": "/#run=" + row["parent_run_id"],
            })
        if row["private_owner_id"] and row["private_owner_id"] != row["owner_id"]:
            raise HTTPException(409, "The source ownership record must be repaired before continuation.")
        return dict(row)

    async def lookup(
        self, db: AsyncSession, source_id: str, principal: ProductPrincipal,
    ) -> ProductContinuation | None:
        await self._root(db, source_id, principal)
        binding = await db.get(ProductContinuation, (self.source_schema, source_id))
        if binding is not None:
            target = await db.get(Session, binding.native_session_id)
            if (
                target is None or target.deleted
                or (target.visibility == "private" and
                    (not principal.individual or target.owner_id != principal.id))
            ):
                raise HTTPException(404, "Session not found.")
        return binding

    async def canonical_original_id(
        self, native_session_id: str, *, db: AsyncSession | None = None,
    ) -> str | None:
        query = select(ProductContinuation.source_session_id).where(
            ProductContinuation.source_schema == self.source_schema,
            ProductContinuation.native_session_id == native_session_id,
        )
        if db is not None:
            return await db.scalar(query)
        async with self.runtime.database.sessions() as connection:
            return await connection.scalar(query)

    @staticmethod
    def result(binding: ProductContinuation, turn_id: str | None = None) -> dict[str, Any]:
        return {
            "source": "native", "id": binding.native_session_id,
            "source_session_id": binding.source_session_id, "url": "/#run=" + binding.source_session_id,
            "turn_id": turn_id, "history_count": binding.history_count,
            "imported_count": binding.imported_count, "recent_history": binding.recent_history,
        }

    async def _inspect(
        self, db: AsyncSession, source_id: str, principal: ProductPrincipal, *, lock: bool = False,
    ) -> dict[str, Any]:
        root = await self._root(db, source_id, principal)
        family = [dict(row) for row in (await db.execute(text(f"""
            SELECT r.id,r.status,r.private_owner_id,r.deleted_at,r.deletion_requested_at,
                r.sandbox_provider,r.sandbox_id,r.snapshot_id,r.checkpoint_error
            FROM {self.table('runs')} r JOIN {self.table('run_roots')} roots ON roots.run_id=r.id
            WHERE roots.root_id=:id ORDER BY r.id LIMIT :limit
        """), {"id": source_id, "limit": MAX_FAMILY + 1})).mappings()]
        if len(family) > MAX_FAMILY:
            raise HTTPException(409, "Drain and review this large session family before migrating it.")
        ids = [row["id"] for row in family]
        if source_id not in ids:
            raise HTTPException(409, "The source session lineage is invalid.")
        if lock:
            # Legacy lease claims bypass the ordinary source writer lock. Hold
            # their exact advisory keys as well, until the mapping is committed.
            await db.execute(text(
                "SELECT pg_advisory_xact_lock(value) FROM unnest(CAST(:keys AS bigint[])) AS value"
            ), {"keys": sorted(session_lease_key(self.source_schema, run) for run in ids)})
        tables = await self._tables(db)
        if not {"durable_sessions", "runtime_leases"} <= tables:
            raise HTTPException(409, "The source runtime must be drained and its durable schema verified first.")
        slots = {"ids": ids}
        journals = [dict(row) for row in (await db.execute(text(f"""
            SELECT run_id,revision,
                CAST(state AS jsonb)->>'phase' AS phase,
                CAST(state AS jsonb)->>'version' AS version,
                CAST(state AS jsonb)->'computer'->>'phase' AS computer_phase,
                CAST(state AS jsonb)->>'sandbox_id' AS control_sandbox_id,
                CAST(state AS jsonb)->'computer'->>'workspace' AS working_dir
            FROM {self.table('durable_sessions')} WHERE run_id IN :ids
        """).bindparams(bindparam("ids", expanding=True)), slots)).mappings()]
        leases = list((await db.execute(text(f"""
            SELECT name FROM {self.table('runtime_leases')}
            WHERE name IN :names AND expires_at>clock_timestamp()
        """).bindparams(bindparam("names", expanding=True)),
            {"names": ["session:" + run for run in ids]})).scalars())
        pending = await db.scalar(text(f"""
            SELECT count(*) FROM {self.table('messages')}
            WHERE run_id IN :ids AND status IN ('queued','running','saving','injected')
        """).bindparams(bindparam("ids", expanding=True)), slots)
        blockers: list[dict[str, str]] = []
        def block(code: str, message: str, action: str) -> None:
            blockers.append({"code": code, "message": message, "action": action})
        if any(
            row["private_owner_id"] and (
                not root["private_owner_id"] or not principal.individual or row["private_owner_id"] != principal.id
            )
            for row in family
        ):
            block("protected_child", "Protected child work cannot be transferred by this requester.",
                  "Review the private branch separately; its files cannot be copied into a shared continuation.")
        if any(row["deleted_at"] or row["deletion_requested_at"] for row in family):
            block("family_deleting", "A child session is deleted or being deleted.",
                  "Finish the family's existing cleanup before continuation.")
        if any(row["status"] not in TERMINAL for row in family) or pending:
            block("active_work", "The old session family still has active or queued work.",
                  "Let it finish, or stop and drain it through the old execution owner.")
        if leases:
            block("live_owner", "The old runtime still holds a live session lease.",
                  "Wait for its execution/drain operation to release ownership.")
        by_run = {row["run_id"]: row for row in journals}
        for member in family:
            journal = by_run.get(member["id"], {})
            if journal.get("version") not in {None, "1"} or journal.get("phase") not in {None, "idle", "warm"}:
                block("unsettled_journal", "The old execution journal is not idle.",
                      "Finish or drain the old runtime; do not replay its checkpoint.")
                break
            if journal.get("computer_phase") not in {None, "ready", "idle", "warm"}:
                block("unsettled_computer", "An old computer operation has not settled.",
                      "Finish the operation and save the workspace before continuing.")
                break
        if "computer_operations" in tables:
            unsettled = await db.scalar(text(f"""
                SELECT count(*) FROM {self.table('computer_operations')}
                WHERE run_id IN :ids AND status IN ('queued','dispatched','unknown')
            """).bindparams(bindparam("ids", expanding=True)), slots)
            if unsettled:
                block("uncertain_operations", "Old computer operations still require outcome inspection.",
                      "Resolve their existing receipts; continuation will never replay those operations.")
        if "events" in tables:
            unsettled = await db.scalar(text(f"""
                SELECT count(*) FROM {self.table('events')}
                WHERE run_id IN :ids AND kind='tool_dispatch'
                  AND COALESCE(CAST(data AS jsonb)->>'outcome','unknown')='unknown'
            """).bindparams(bindparam("ids", expanding=True)), slots)
            if unsettled:
                block("uncertain_tools", "An earlier connected-app tool has no confirmed outcome.",
                      "Inspect its receipt and destination with the original owner before transferring.")
        if "approvals" in tables:
            approvals = await db.scalar(text(f"""
                SELECT count(*) FROM {self.table('approvals')} WHERE run_id IN :ids AND status='pending'
            """).bindparams(bindparam("ids", expanding=True)), slots)
            if approvals:
                block("pending_approval", "An old approval is still pending.",
                      "Resolve or cancel the old request before moving the session.")
        if "swarm_missions" in tables:
            mission = await db.scalar(text(f"""
                SELECT count(*) FROM {self.table('swarm_missions')}
                WHERE run_id IN :ids AND status IN ('active','paused')
            """).bindparams(bindparam("ids", expanding=True)), slots)
            if mission:
                block("active_mission", "The existing goal/swarm still owns this session.",
                      "Finish or retire that mission before continuing as a native conversation.")
        if "session_goals" in tables:
            goals = await db.scalar(text(f"""
                SELECT count(*) FROM {self.table('session_goals')} WHERE run_id IN :ids
                AND COALESCE(CAST(state AS jsonb)->>'status','unknown')
                    NOT IN ('completed','cancelled','failed','budget_exhausted')
            """).bindparams(bindparam("ids", expanding=True)), slots)
            if goals:
                block("open_goal", "An existing goal is still open or its state is unknown.",
                      "Finish or explicitly retire the goal with its original owner before transferring.")
        history_count = int(await db.scalar(text(f"""
            SELECT count(*) FROM {self.table('messages')} WHERE run_id=:id
                AND role IN ('user','assistant') AND status!='deleted'
        """), {"id": source_id}) or 0)
        rows = [dict(row) for row in (await db.execute(text(f"""
            SELECT id,role,user_id,status,created_at,started_at,revision,
                substr(content,1,:chars) AS content,length(content)>:chars AS oversized
            FROM {self.table('messages')} WHERE run_id=:id AND role IN ('user','assistant') AND status!='deleted'
            ORDER BY COALESCE(NULLIF(started_at,''),created_at) DESC,id DESC LIMIT :limit
        """), {"id": source_id, "chars": MAX_HISTORY_BYTES, "limit": MAX_HISTORY_MESSAGES})).mappings()]
        chosen, total = [], 0
        for row in rows:
            size = len(row["content"].encode())
            if row.pop("oversized") or total + size > MAX_HISTORY_BYTES:
                continue
            chosen.append(row)
            total += size
        chosen.reverse()
        archives = {}
        if "artifact_objects" in tables:
            items = (await db.execute(text(f"""
                SELECT name,size,sha256,reference FROM {self.table('artifact_objects')} WHERE name=:name
            """.replace("name=:name", "name IN :names")).bindparams(bindparam("names", expanding=True)),
                {"names": [run + ".zip" for run in ids]})).mappings()
            archives = {row["name"]: dict(row) for row in items}
        files = getattr(self.runtime, "product_files", None)
        artifacts = getattr(files, "artifacts", None)
        if artifacts is not None:
            # Older installs kept fixed-name ZIPs before artifact_objects existed.
            # Probe only authorized family names, never a directory walk/user path.
            missing = [identity + ".zip" for identity in ids if identity + ".zip" not in archives]

            def local_archives():
                return {name: info for name in missing if (info := artifacts.info(name)) is not None}
            archives.update(await asyncio.to_thread(local_archives))
        warm = any(member["sandbox_id"] or by_run.get(member["id"], {}).get("control_sandbox_id")
                   for member in family)
        members = []
        for member in family:
            journal = by_run.get(member["id"], {})
            archive = archives.get(member["id"] + ".zip")
            machine = bool(member["sandbox_id"] or journal.get("control_sandbox_id"))
            if machine and journal.get("phase") != "warm":
                block("unknown_machine", "A saved computer handle has no verified idle journal.",
                      "Drain that machine using the original owner before transferring its files.")
            if member["sandbox_provider"] != "modal" and (member["snapshot_id"] or machine):
                block("provider_export", "This old computer needs a provider-owned drain before transfer.",
                      "Export and verify its workspace archive, then drain and clear its live handle.")
            if member["checkpoint_error"]:
                block("unverified_workspace", "The earlier workspace has an unresolved save/recovery warning.",
                      "Verify or export its files before continuing; a failed save is not an empty workspace.")
            members.append({
                "id": member["id"], "provider": member["sandbox_provider"],
                "snapshot_id": member["snapshot_id"] or None, "sandbox_id": member["sandbox_id"] or None,
                "control_sandbox_id": journal.get("control_sandbox_id"), "archive": archive,
                "working_dir": journal.get("working_dir") or "/workspace",
            })
        workspace_plan = {
            "version": 1, "source_session_id": source_id, "source_schema": self.source_schema,
            "members": members,
        }
        surface = "web"
        for slack_table in ("slack_threads", "slack_events"):
            if slack_table in tables and await db.scalar(text(
                f"SELECT 1 FROM {self.table(slack_table)} WHERE run_id=:id LIMIT 1"
            ), {"id": source_id}):
                surface = "slack"
                break
        digest = fingerprint({
            "root": root, "family": family, "journals": journals,
            "history_count": history_count, "history": chosen, "workspace": workspace_plan,
        })
        return {
            "root": root, "blockers": blockers, "history": chosen, "history_count": history_count,
            "imported_count": len(chosen), "history_bytes": total, "source_fingerprint": digest,
            "workspace": workspace_plan, "surface": surface, "warm": warm,
            "requires_recent_history": history_count != len(chosen),
            "requires_workspace_ack": any(
                item["snapshot_id"] or item["sandbox_id"] or item["archive"] for item in members
            ),
        }

    async def preview(self, principal: ProductPrincipal, source_id: str) -> dict[str, Any]:
        current = await self.identity.require_current(principal.grant)
        async with self.runtime.database.sessions() as db:
            binding = await self.lookup(db, source_id, current)
            if binding:
                return {"state": "continued", "continuation": self.result(binding), "blockers": []}
            inspected = await self._inspect(db, source_id, current)
        await self.identity.require_current(principal.grant)
        await self.history.require(current, source_id)
        return {
            "state": "blocked" if inspected["blockers"] else "ready",
            "source_fingerprint": inspected["source_fingerprint"], "blockers": inspected["blockers"],
            "history_count": inspected["history_count"], "imported_count": inspected["imported_count"],
            "history_bytes": inspected["history_bytes"], "surface": inspected["surface"],
            "visibility": "private" if inspected["root"]["private_owner_id"] else "shared",
            "requires_recent_history": inspected["requires_recent_history"],
            "requires_workspace_ack": inspected["requires_workspace_ack"],
            "requires_workspace_drain": inspected["warm"],
            "file_notice": "Visible saved files are restored under /workspace/previous/<original-session-id>/ "
                           "in a fresh computer. Hidden files, dependencies, symlinks, .git and browser profiles "
                           "are excluded. Original files and recovery ZIPs remain accessible; no old work is replayed.",
        }

    async def _retain_owner(self, db: AsyncSession, source_owner: str) -> None:
        """A non-login identity cache, never another person's authority grant."""
        if await db.get(User, source_owner) is not None:
            return
        profile = (await db.execute(text(f"""
            SELECT id,name,email,to_jsonb(u)->>'enabled' AS enabled,
                to_jsonb(u)->>'disabled' AS disabled,to_jsonb(u)->>'disabled_at' AS disabled_at
            FROM {self.table('users')} u WHERE id=:id
        """), {"id": source_owner})).mappings().first()
        disabled = bool(profile and (
            profile["enabled"] in {"false", "0"} or profile["disabled"] in {"true", "1"} or profile["disabled_at"]
        ))
        await db.execute(insert(User).values(
            id=source_owner, username="product:" + hashlib.sha256(source_owner.encode()).hexdigest(),
            display_name=(profile["name"] or profile["email"] or "Earlier participant") if profile else "Earlier participant",
            password="!retained-product-profile", role="member", enabled=not disabled, auth_version=1,
        ).on_conflict_do_nothing(index_elements=[User.id]))

    async def continue_session(
        self, principal: ProductPrincipal, actor: User, source_id: str, request: ContinuationRequest,
        *, received_at: str | None = None,
    ) -> dict[str, Any]:
        async with self.runtime.database.sessions.begin() as db:
            return await self.continue_for_admission(
                db, actor, principal, source_id, request.message(), surface="web", provenance={"surface": "web"},
                expected_source_fingerprint=request.expected_source_fingerprint,
                accept_recent_history=request.accept_recent_history,
                accept_workspace_omissions=request.accept_workspace_omissions,
                drain_idle_workspace=request.drain_idle_workspace,
                received_at=received_at,
            )

    async def continue_for_admission(
        self, db: AsyncSession, actor: User, principal: ProductPrincipal, source_id: str, body: NewMessage, *,
        surface: str, provenance: dict[str, Any], expected_source_fingerprint: str | None = None,
        accept_recent_history: bool = False, accept_workspace_omissions: bool = False,
        drain_idle_workspace: bool = False,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        if actor.id != principal.id or not db.in_transaction() or db.get_bind().dialect.name != "postgresql":
            raise HTTPException(403, "Continuation needs the same canonical requester and PostgreSQL admission.")
        # The owner change must exclude source writers, not merely acquire the
        # shared read-admission lock used by ordinary native turns.
        await db.execute(text(
            "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": self.source_schema})
        current = await self.identity.authorize_admission(db, principal.grant)
        setattr(actor, "product_grant", principal.grant)  # noqa: B010 -- transient authenticated projection
        binding = await self.lookup(db, source_id, current)
        if binding is not None:
            turn = await self.runtime.database.submit(
                actor, binding.native_session_id, body, db=db, received_at=received_at,
            )
            return self.result(binding, turn.id)
        inspected = await self._inspect(db, source_id, current, lock=True)
        if inspected["blockers"]:
            raise HTTPException(409, {"code": "source_not_idle", "blockers": inspected["blockers"],
                                      "url": "/#run=" + source_id})
        if expected_source_fingerprint is not None and expected_source_fingerprint != inspected["source_fingerprint"]:
            raise HTTPException(409, "The source session changed. Review its continuation preview again.")
        if surface != "web" and inspected["surface"] != surface:
            raise HTTPException(403, "The source session belongs to a different delivery surface.")
        if inspected["requires_recent_history"] and not accept_recent_history:
            raise HTTPException(409, {"code": "history_review_required",
                                      "message": "Review the recent-history import before continuing.",
                                      "url": "/#run=" + source_id})
        if inspected["requires_workspace_ack"] and not accept_workspace_omissions:
            raise HTTPException(409, {"code": "workspace_review_required",
                                      "message": "Review file restoration and excluded runtime/credential files.",
                                      "url": "/#run=" + source_id})
        if inspected["warm"] and not drain_idle_workspace:
            raise HTTPException(409, {"code": "workspace_drain_required",
                                      "message": "Save and close the idle old computer before native execution.",
                                      "url": "/#run=" + source_id})
        root = inspected["root"]
        visibility = "private" if root["private_owner_id"] else "shared"
        target_surface = inspected["surface"]
        if visibility == "private" and target_surface != "web":
            raise HTTPException(403, "Private history cannot be exported to another surface.")
        context = await self.workspace.capture_workspace_context(db, current, {
            **{key: root.get(key) for key in ("repo_url", "github_repository_id", "environment_id")},
            # A session without a captured old build must not silently adopt a
            # newly selected organization default.
            "environment_build_id": root.get("environment_build_id") or "none",
        })
        context["source_session_id"] = source_id
        context["context_fingerprint"] = fingerprint({key: value for key, value in context.items()
                                                       if key != "context_fingerprint"})
        await self._retain_owner(db, root["owner_id"])
        target = Session(
            id=new_id(), owner_id=root["owner_id"],
            title=(root["display_title"] or root["agent_label"] or root["prompt"] or "Earlier conversation")[:300],
            visibility=visibility, surface=target_surface, model=body.model or self.runtime.settings.default_model,
            created_at=root["created_at"], updated_at=now(),
        )
        db.add(target)
        await db.flush()
        grants = await self.runtime.database.resolve_grants(
            db, actor.id, body.connections, shared_session=visibility == "shared", surface=target_surface,
        )
        self.workspace.authorize_project_grants(context, grants)
        scope = RunScope(target.id, "", actor.id, target.model, grants, visibility, target_surface,
                         product_grant=principal.grant)
        seed = public_conversation(inspected["history"], scope, source_id)
        target.conversation = self.runtime.security.seal(ConversationTypeAdapter.dump_json(seed))
        target.last_actor_id, target.last_model, target.last_grants = actor.id, target.model, grant_fingerprint(grants)
        # Preserve this viewer's navigation without treating another person's
        # personal pin/folder/archive settings as global session properties.
        metadata = await self.history.allowed(db, current, source_id)  # AsyncSession supplies the same execute API.
        if metadata["folder"]:
            exists = await db.scalar(select(SessionFolder.id).where(
                SessionFolder.user_id == actor.id, SessionFolder.name == metadata["folder"],
            ))
            if exists is None:
                db.add(SessionFolder(user_id=actor.id, name=metadata["folder"]))
        db.add(SessionPreference(user_id=actor.id, session_id=target.id,
                                 pinned=bool(metadata["pinned"]), folder=metadata["folder"] or ""))
        turn = await self.runtime.database.submit(actor, target.id, body, db=db, received_at=received_at)
        await self.workspace.bind_automation_turn(db, target, turn, context)
        # Invalidate old broker capabilities before publishing the immutable
        # mapping. The trigger then refuses every later source admission/write.
        await db.execute(text(
            f"UPDATE {self.table('runs')} SET token_hash='' WHERE id IN "
            f"(SELECT run_id FROM {self.table('run_roots')} WHERE root_id=:id)"
        ), {"id": source_id})
        workspace_plan = inspected["workspace"] | {
            "context": context, "accept_workspace_omissions": accept_workspace_omissions,
            "drain_idle_workspace": drain_idle_workspace, "native_session_id": target.id,
        }
        binding = ProductContinuation(
            source_schema=self.source_schema, source_session_id=source_id, native_session_id=target.id,
            source_owner_id=root["owner_id"], initiated_by=actor.id,
            source_fingerprint=inspected["source_fingerprint"],
            history=self.runtime.security.seal(inspected["history"]),
            history_count=inspected["history_count"], imported_count=inspected["imported_count"],
            recent_history=inspected["requires_recent_history"],
            workspace=self.runtime.security.seal(workspace_plan), workspace_fingerprint=fingerprint(workspace_plan),
            first_turn_id=turn.id, provenance=self.runtime.security.seal(provenance),
        )
        db.add(binding)
        await db.flush()
        return self.result(binding, turn.id)

    async def prepare(self, prepared: PreparedTurn) -> PreparedTurn:
        """Restore public ancestry after a normal native scope rebase.

        Matching native working conversations retain their SDK compaction/history.
        A new actor/model/account scope gets only the authorized public seed.
        """
        scope = prepared.scope
        await self.runtime.database.authorize_scope(scope)
        async with self.runtime.database.sessions.begin() as db:
            binding = await db.scalar(select(ProductContinuation).where(
                ProductContinuation.native_session_id == scope.session_id,
                ProductContinuation.source_schema == self.source_schema,
            ))
            if binding is None:
                return prepared
            session = await db.get(Session, scope.session_id)
            turn = await db.get(Turn, scope.turn_id)
            if session is None or turn is None:
                raise PermissionError("The continuation target no longer exists.")
            current = await self.identity.require_current(scope.product_grant)
            await self._root(db, binding.source_session_id, current)
            conversation = ConversationTypeAdapter.validate_json(prepared.conversation) if prepared.conversation else None
            imported = bool(conversation and any(
                (message.metadata or {}).get(IMPORT_MARKER)
                for message in conversation.messages
            ))
            same_scope = (session.last_actor_id == scope.actor_id and session.last_model == scope.model
                          and session.last_grants == grant_fingerprint(scope.grants))
            if not imported and (conversation is None or not same_scope):
                messages = json.loads(self.runtime.security.unseal(binding.history))
                seed = public_conversation(messages, scope, binding.source_session_id)
                if conversation is not None:
                    seed.messages.extend(conversation.messages)
                prepared.conversation = ConversationTypeAdapter.dump_json(seed).decode()
                from pydantic import TypeAdapter
                turn.preparation = self.runtime.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
        await self.runtime.database.authorize_scope(scope)
        return prepared
