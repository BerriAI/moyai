"""Canonical PostgreSQL and real Temporal tests; not final provider/app proof."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from fastapi import HTTPException
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from temporalio.client import ScheduleAlreadyRunningError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_product_authority import demote

from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.effects import EffectReceipts, effect_id
from moyai.models import Effect, Session, Turn
from moyai.product_automation_models import ProductAutomationBinding as Binding
from moyai.product_automation_models import ProductAutomationOccurrence as Occurrence
from moyai.product_automation_models import ProductAutomationOperation as Operation
from moyai.product_automation_tools import CreateRequest, MigrateRequest, TargetRequest, toolset
from moyai.product_automations import (
    PRODUCT_AUTOMATION_ACTIVITIES,
    PRODUCT_AUTOMATION_WORKFLOWS,
    AutomationDefinition,
    Migration,
    Mutation,
    ProductAutomationService,
    Save,
    legacy_schedule_id,
    native_schedule_id,
)
from moyai.product_connections import ProductConnections
from moyai.storage import ObjectStore, encrypted_converter

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


class FakeSchedules:
    """Deterministic RPC ordering/failure fixture, not scheduler execution proof."""

    def __init__(self):
        self.schedules = {}
        self.calls = []
        self.pause_confirmed = True
        self.block_update = None
        self.update_started = asyncio.Event()
        self.update_release = asyncio.Event()

    async def start_workflow(self, *args, **kwargs):
        self.calls.append(("sync_requested", kwargs["id"]))

    async def create_schedule(self, identity, schedule, **kwargs):
        if identity in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.calls.append(("create", identity))
        self.schedules[identity] = copy.deepcopy(schedule)

    def get_schedule_handle(self, identity):
        owner = self

        class Handle:
            async def pause(self, **kwargs):
                owner.calls.append(("pause", identity))
                if identity not in owner.schedules:
                    raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
                owner.schedules[identity].state.paused = owner.pause_confirmed

            async def describe(self, **kwargs):
                owner.calls.append(("describe", identity))
                if identity not in owner.schedules:
                    raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
                return SimpleNamespace(
                    schedule=copy.deepcopy(owner.schedules[identity]),
                    info=SimpleNamespace(next_action_times=[datetime(2030, 1, 1, tzinfo=UTC)]),
                )

            async def update(self, callback, **kwargs):
                owner.calls.append(("update", identity))
                value = callback(SimpleNamespace(description=await self.describe()))
                if owner.block_update == identity:
                    owner.block_update = None
                    owner.update_started.set()
                    await owner.update_release.wait()
                owner.schedules[identity] = copy.deepcopy(value.schedule)

            async def delete(self, **kwargs):
                owner.calls.append(("delete", identity))
                owner.schedules.pop(identity, None)

        return Handle()


@pytest_asyncio.fixture
async def automations(admitted_product):
    product = admitted_product
    statements = (
        """CREATE TABLE moyai.automations (
            id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES moyai.users(id),definition TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,synced_revision INTEGER NOT NULL DEFAULT 0,
            synced_schedule_version INTEGER NOT NULL DEFAULT 0,paused INTEGER NOT NULL DEFAULT 1,
            sync_error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
        """CREATE TABLE moyai.runs (
            id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'completed',private_owner_id TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL DEFAULT '',deletion_requested_at TEXT NOT NULL DEFAULT '')""",
        "CREATE TABLE moyai.run_ancestry(run_id TEXT,ancestor_id TEXT)",
        """CREATE TABLE moyai.automation_runs (
            occurrence TEXT PRIMARY KEY,automation_id TEXT NOT NULL REFERENCES moyai.automations(id),
            revision INTEGER NOT NULL,run_id TEXT UNIQUE REFERENCES moyai.runs(id),
            outcome TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)""",
        """CREATE TABLE moyai.automation_schedules (
            automation_id TEXT NOT NULL REFERENCES moyai.automations(id),schedule_id TEXT PRIMARY KEY)""",
        """CREATE TABLE moyai.automation_items (
            automation_id TEXT NOT NULL REFERENCES moyai.automations(id),item_key TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES moyai.runs(id),created_at TEXT NOT NULL,
            PRIMARY KEY(automation_id,item_key))""",
    )
    async with product.source.begin() as db:
        for statement in statements:
            await db.execute(text(statement))
    remote = FakeSchedules()

    async def connect():
        return remote

    service = ProductAutomationService(
        product.database, product.adapter, product.adapter.settings.model_copy(update={"agent_model": "test"}), connect,
    )
    return SimpleNamespace(product=product, service=service, remote=remote, grant=product.principal.grant)


@pytest_asyncio.fixture
async def canonical_connections(automations):
    ctx = automations
    async with ctx.product.source.begin() as db:
        for statement in (
            "CREATE TABLE moyai.connections (provider TEXT PRIMARY KEY,encrypted TEXT NOT NULL,label TEXT,updated_at TEXT)",
            "CREATE TABLE moyai.connection_policies (provider TEXT PRIMARY KEY,enabled INTEGER,read_only INTEGER,checked_at TEXT,check_status TEXT)",
            "CREATE TABLE moyai.connection_audit (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,provider TEXT)",
            "CREATE TABLE moyai.personal_slack_grants (owner_id TEXT PRIMARY KEY,encrypted TEXT)",
            "CREATE TABLE moyai.github_app (id INTEGER PRIMARY KEY,encrypted TEXT)",
        ):
            await db.execute(text(statement))

    class NoOtherConnection:
        schema_updates = False

        def rows(self, *args, **kwargs):
            pytest.fail("A canonical grant read escaped the caller's PostgreSQL transaction.")

    connections = ProductConnections(SimpleNamespace(
        store_proxy=NoOtherConnection(), identity=ctx.product.adapter,
        security=ctx.product.adapter.legacy_security, settings=ctx.service.settings,
    ), ctx.product.database)
    ctx.service.connections = connections
    ctx.product.database.product_connections = connections
    return connections


async def save_connection(ctx, provider, **extra):
    from app.connection_authority import new_credentials
    encrypted = ctx.product.adapter.legacy_security.encrypt(json.dumps(new_credentials({
        "access_token": "synthetic-token", "kind": "personal", **extra,
    })))
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.connections VALUES (:provider,:encrypted,'Synthetic','now') "
            "ON CONFLICT(provider) DO UPDATE SET encrypted=excluded.encrypted"
        ), {"provider": provider, "encrypted": encrypted})


def definition(**overrides):
    return AutomationDefinition.model_validate({
        "name": "Synthetic scheduled work", "prompt": "Summarize synthetic results.",
        "triggers": [{"id": "default", "schedule": {"frequency": "daily", "time": "09:00", "timezone": "UTC"}}],
        "environment_id": "none", "plugins": [], "model": "test", "mode": "modal",
        **overrides,
    })


async def create(ctx, **overrides):
    return await ctx.service.mutate(
        ctx.grant, "create", None,
        Save(revision=0, request_key="create-" + uuid4().hex, definition=definition(**overrides)),
    )


async def activate(ctx, **overrides):
    created = await create(ctx, **overrides)
    migrated = await ctx.service.mutate(
        ctx.grant, "migrate", created["id"],
        Migration(revision=created["revision"], request_key="migrate-" + uuid4().hex, activate=True),
    )
    await ctx.service.reconcile(created["id"])
    async with ctx.product.database.sessions() as db:
        binding = await db.get(Binding, created["id"])
    return migrated, binding


async def occurrence(ctx, row, binding, *, stamp=None):
    stamp = stamp or datetime.now(UTC)
    key = f"native:{row['id']}:default:{stamp.isoformat()}"
    return await ctx.service.admit(
        row["id"], binding.generation, row["revision"], "default", key, stamp.isoformat(),
    )


async def test_create_and_preview_are_paused_canonical_and_never_schedule(automations):
    ctx = automations
    row = await create(ctx)
    preview = await ctx.service.preview(ctx.grant, row["id"])
    assert preview["supported"] and preview["native"] is None and preview["legacy_paused"]
    assert preview["execution_visibility"] == "shared"
    assert not ctx.remote.calls
    async with ctx.product.source.connect() as db:
        saved = (await db.execute(text("SELECT * FROM moyai.automations"))).mappings().one()
        assert saved["paused"] == 1
        assert json.loads(saved["definition"])["harness"] != "pydantic-v1"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


@pytest.mark.parametrize(("change", "blocker"), [
    ({"plugins": ["github"]}, "connection_mapping_required"),
    ({"environment_id": "auto"}, "environment_adapter_required_choose_none_explicitly"),
    ({"repo_url": "https://github.com/synthetic/repository"}, "repository_bootstrap_adapter_required"),
    ({"mode": "demo"}, "execution_mode_not_supported"),
])
async def test_unsupported_variants_never_activate_or_capture_grants(automations, change, blocker):
    ctx = automations
    row = await create(ctx, **change)
    assert blocker in (await ctx.service.preview(ctx.grant, row["id"]))["blockers"]
    with pytest.raises(HTTPException) as denied:
        await ctx.service.mutate(
            ctx.grant, "migrate", row["id"], Migration(revision=1, request_key="migration-rejected", activate=True),
        )
    assert denied.value.status_code == 409
    async with ctx.product.database.sessions() as db:
        assert await db.get(Binding, row["id"]) is None
    assert not ctx.remote.calls


async def test_migration_fences_old_schedules_and_native_admission_is_atomic_idempotent(automations):
    ctx = automations
    row, binding = await activate(ctx)
    assert ctx.remote.calls.index(("pause", legacy_schedule_id(row["id"], "default"))) < ctx.remote.calls.index(
        ("create", native_schedule_id(row["id"], "default")),
    )
    stamp = datetime.now(UTC)
    results = await asyncio.gather(*[occurrence(ctx, row, binding, stamp=stamp) for _ in range(2)])
    assert results[0] == results[1] and results[0]["status"] == "admitted"
    async with ctx.product.database.sessions() as db:
        session = await db.get(Session, results[0]["session_id"])
        assert session.visibility == "shared" and session.surface == "automation"
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
        assert await db.scalar(text("SELECT count(*) FROM moyai.runs")) == 0
        receipt = (await db.execute(text("SELECT * FROM moyai.automation_runs"))).mappings().one()
        assert receipt["run_id"] is None and receipt["outcome"] == "started"
        source = (await db.execute(text("SELECT * FROM moyai.automations"))).mappings().one()
        assert source["paused"] == 1 and json.loads(source["definition"])["harness"] == "pydantic-v1"
    assert (await ctx.service.history(ctx.grant, row["id"]))["history"][0]["turn_id"] == results[0]["turn_id"]
    with pytest.raises(ValueError, match="harness"):
        ctx.service.settings.harness_model("pydantic-v1", "test")


async def test_lost_mutation_ack_reuses_receipt_and_does_not_reenable_after_change(automations):
    ctx = automations
    body = Save(revision=0, request_key="stable-operation-key", definition=definition())
    first = await ctx.service.mutate(ctx.grant, "create", None, body, operation_scope="session:synthetic")
    second = await ctx.service.mutate(ctx.grant, "create", None, body, operation_scope="session:synthetic")
    assert first == second
    await ctx.service.mutate(ctx.grant, "pause", first["id"], Mutation(revision=1, request_key="pause-existing"))
    with pytest.raises(HTTPException) as changed:
        await ctx.service.mutate(ctx.grant, "create", None, body, operation_scope="session:synthetic")
    assert changed.value.status_code == 409
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 1


async def test_original_grant_revocation_never_mints_replacement(automations):
    ctx = automations
    row, binding = await activate(ctx)
    await demote(ctx.product)
    result = await occurrence(ctx, row, binding)
    assert result["status"] == "blocked"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
        assert (await db.get(Binding, row["id"])).authority_version == ctx.grant.authority_version


@pytest.mark.parametrize("last_paused", [True, False])
async def test_concurrent_stale_remote_update_converges_to_latest_generation(automations, last_paused):
    ctx = automations
    row, _binding = await activate(ctx)
    first_action = "enable" if last_paused else "pause"
    first = await ctx.service.mutate(
        ctx.grant, first_action, row["id"], Mutation(revision=row["revision"], request_key="first-change"),
    )
    schedule_id = native_schedule_id(row["id"], "default")
    ctx.remote.block_update = schedule_id
    syncing = asyncio.create_task(ctx.service.reconcile(row["id"]))
    await asyncio.wait_for(ctx.remote.update_started.wait(), 3)
    try:
        latest = await ctx.service.mutate(
            ctx.grant, "pause" if last_paused else "enable", row["id"],
            Mutation(revision=first["revision"], request_key="latest-change"),
        )
    finally:
        ctx.remote.update_release.set()
    assert (await asyncio.wait_for(syncing, 5))["status"] == "ready"
    assert ctx.remote.schedules[schedule_id].state.paused is last_paused
    async with ctx.product.database.sessions() as db:
        binding = await db.get(Binding, row["id"])
        assert binding.source_revision == latest["revision"]
        assert binding.synced_generation == binding.generation and binding.sync_state == "ready"


async def test_pause_and_native_admission_share_canonical_writer_fence(automations):
    ctx = automations
    row, binding = await activate(ctx)
    await ctx.service.mutate(
        ctx.grant, "pause", row["id"], Mutation(revision=row["revision"], request_key="pause-before-tick"),
    )
    assert (await occurrence(ctx, row, binding))["status"] == "skipped"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


async def test_stale_source_editor_and_pending_sync_block_admission(automations):
    ctx = automations
    row, binding = await activate(ctx)
    async with ctx.product.source.begin() as db:
        await db.execute(text("UPDATE moyai.automations SET revision=revision+1,paused=0"))
    assert (await occurrence(ctx, row, binding))["status"] == "skipped"
    assert (await ctx.service.reconcile(row["id"]))["status"] == "blocked"
    assert ctx.remote.schedules[native_schedule_id(row["id"], "default")].state.paused


async def test_private_and_delegated_tool_sources_are_denied_from_persisted_state(automations):
    ctx = automations
    session = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    turn = await ctx.product.database.submit(
        ctx.product.actor, session.id, NewMessage(prompt="Synthetic direct request", request_key="tool-origin"),
    )
    await ctx.product.database.claim_next()
    scope = (await ctx.product.database.prepare(turn.id)).scope
    await ctx.service.authorize_tool(scope, turn.id)
    with pytest.raises(HTTPException):
        await ctx.service.authorize_tool(scope, "delegated-run")
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session.id).values(visibility="private"))
    assert scope.visibility == "shared"  # Persisted visibility wins over the old snapshot.
    with pytest.raises(HTTPException):
        await ctx.service.mutate(
            ctx.grant, "create", None, Save(revision=0, request_key="private-export", definition=definition()),
            operation_scope=f"session:{session.id}", scope=scope,
        )
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 0


def test_missing_trigger_ids_rejected_before_random_default_factory():
    with pytest.raises(ValueError, match="stable id"):
        definition(triggers=[{"schedule": {"frequency": "daily"}}])


async def test_real_temporal_schedule_sync_and_once_admission(automations, monkeypatch):
    ctx = automations
    converter = encrypted_converter(
        ctx.product.database.security, ObjectStore(ctx.product.settings, ctx.product.database.security),
    )
    environment = await WorkflowEnvironment.start_local(data_converter=converter)
    async with environment:
        async def connect():
            return environment.client
        ctx.service.connect = connect
        monkeypatch.setattr("moyai.runtime._runtime", SimpleNamespace(product_automations=ctx.service))
        async with Worker(
            environment.client, task_queue=ctx.service.queue,
            workflows=PRODUCT_AUTOMATION_WORKFLOWS, activities=PRODUCT_AUTOMATION_ACTIVITIES,
        ):
            run_at = (datetime.now(UTC) + timedelta(seconds=7)).replace(microsecond=0)
            row = await create(ctx, triggers=[{
                "id": "default", "schedule": {"frequency": "once", "run_at": run_at.isoformat(), "timezone": "UTC"},
            }])
            migrated = await ctx.service.mutate(
                ctx.grant, "migrate", row["id"],
                Migration(revision=1, request_key="real-temporal-migrate", activate=True),
            )
            async with asyncio.timeout(15):
                while True:
                    async with ctx.product.database.sessions() as db:
                        receipt = await db.scalar(select(Occurrence).where(Occurrence.automation_id == row["id"]))
                        if receipt is not None:
                            break
                    await asyncio.sleep(0.1)
            assert receipt.status == "admitted" and receipt.turn_id
            async with ctx.product.database.sessions() as db:
                binding = await db.get(Binding, row["id"])
                assert binding.sync_state == "ready" and binding.synced_generation == binding.generation
                assert (await db.get(Session, receipt.session_id)).visibility == "shared"
                assert (await db.get(Turn, receipt.turn_id)).status == "queued"
                assert await db.scalar(text("SELECT count(*) FROM moyai.runs")) == 0
            # The same acknowledged occurrence recovers its original native IDs.
            result = await occurrence(ctx, migrated, binding, stamp=run_at)
            assert result["turn_id"] == receipt.turn_id
            async with ctx.product.database.sessions() as db:
                assert await db.scalar(select(func.count()).select_from(Turn)) == 1
            handle = environment.client.get_schedule_handle(native_schedule_id(row["id"], "default"))
            description = await handle.describe()
            assert description.schedule.policy.catchup_window == timedelta(minutes=15)
            assert description.schedule.policy.overlap.name == "ALLOW_ALL"
            await handle.delete()


async def test_shared_password_ui_authority_does_not_authorize_agent_tools(automations):
    ctx = automations
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Synthetic shared admin',NULL,'c','u')"
        ))
    principal = await ctx.product.adapter.current("shared:password:admin")
    async with ctx.product.database.sessions.begin() as db:
        actor = await ctx.product.adapter.project_native_user(db, principal)
    session = await ctx.product.database.create_session(actor, NewSession(visibility="shared"))
    turn = await ctx.product.database.submit(actor, session.id, NewMessage(prompt="Synthetic", request_key="shared-ui"))
    await ctx.product.database.claim_next()
    scope = (await ctx.product.database.prepare(turn.id)).scope
    with pytest.raises(HTTPException) as denied:
        await ctx.service.authorize_tool(scope, turn.id)
    assert denied.value.status_code == 403
    # Preserve the original UI policy without extending it to agent tools.
    saved = await ctx.service.mutate(
        principal.grant, "create", None, Save(revision=0, request_key="shared-ui-create", definition=definition()),
    )
    assert saved["native_state"] == "not_migrated" and saved["legacy_paused"]


async def test_nonowner_cannot_manage_or_read_automation_but_admin_can_pause_ui(automations):
    ctx = automations
    row, _ = await activate(ctx)
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('google:other','google','other@example.test','Other',NULL,'c','u')"
        ))
    other = await ctx.product.adapter.current("google:other")
    for action in ("enable", "update", "migrate"):
        with pytest.raises(HTTPException) as denied:
            await ctx.service.mutate(
                other.grant, action, row["id"],
                Mutation(revision=row["revision"], request_key=f"other-{action}-key"),
            )
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException):
        await ctx.service.history(other.grant, row["id"])
    # The original admin principal can pause another owner's canonical definition.
    created = await ctx.service.mutate(
        other.grant, "create", None, Save(revision=0, request_key="other-create-key", definition=definition()),
    )
    paused = await ctx.service.mutate(
        ctx.grant, "pause", created["id"], Mutation(revision=1, request_key="admin-pause-key"),
    )
    assert paused["legacy_paused"]


async def tool_context(ctx, *, visibility="shared", actor=None, delegate=False, surface="web"):
    actor = actor or ctx.product.actor
    session = await ctx.product.database.create_session(actor, NewSession(visibility=visibility))
    if surface != "web":
        async with ctx.product.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == session.id).values(surface=surface))
    turn = await ctx.product.database.submit(
        actor, session.id, NewMessage(prompt="Synthetic user scheduling request", request_key="tool-" + uuid4().hex),
    )
    await ctx.product.database.claim_next()
    scope = (await ctx.product.database.prepare(turn.id)).scope
    return RunContext(
        deps=scope, model=FunctionModel(lambda *_: ModelResponse(parts=[TextPart("unused")])),
        usage=RunUsage(), run_id="delegated" if delegate else turn.id,
        tool_call_id="synthetic-tool-call",
    )


async def reserved_call(ctx, run_ctx, name, body):
    native_tools = toolset(ctx.service)
    tool = (await native_tools.get_tools(run_ctx))[name]
    run_ctx.tool_name = name
    call = ToolCallPart(name, {"request": body.model_dump(mode="json")}, tool_call_id=run_ctx.tool_call_id)
    identity = effect_id(run_ctx, call)
    await EffectReceipts(ctx.product.database).reserve(run_ctx.deps, identity, name, call.args)
    return native_tools, tool, identity


@pytest.mark.parametrize("reason", ["private", "delegate", "wrong_turn", "revision", "blocker", "shared_password"])
async def test_trusted_tool_refusals_settle_only_the_current_unstarted_attempt(automations, reason):
    ctx = automations
    actor = None
    if reason == "shared_password":
        async with ctx.product.source.begin() as db:
            await db.execute(text(
                "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared synthetic',NULL,'c','u')"
            ))
        principal = await ctx.product.adapter.current("shared:password:admin")
        async with ctx.product.database.sessions.begin() as db:
            actor = await ctx.product.adapter.project_native_user(db, principal)
    run_ctx = await tool_context(
        ctx, visibility="private" if reason == "private" else "shared", actor=actor, delegate=reason == "delegate",
    )
    name = "automation_create"
    body = CreateRequest(
        revision=0, request_key="refused-create", definition=definition(),
        turn_id="wrong" if reason == "wrong_turn" else run_ctx.deps.turn_id,
    )
    existing = 0
    if reason in {"revision", "blocker"}:
        row = await create(ctx, **({"environment_id": "auto"} if reason == "blocker" else {}))
        existing = 1
        name = "automation_pause" if reason == "revision" else "automation_migrate"
        body = (
            TargetRequest(automation_id=row["id"], revision=99, request_key="wrong-revision", turn_id=run_ctx.deps.turn_id)
            if reason == "revision" else
            MigrateRequest(automation_id=row["id"], revision=1, request_key="blocked-migration", turn_id=run_ctx.deps.turn_id)
        )
    tools, selected, identity = await reserved_call(ctx, run_ctx, name, body)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool(name, {"request": body}, run_ctx, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "not_dispatched"
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == existing
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


async def test_postcommit_authority_change_keeps_confirmed_effect(automations, monkeypatch):
    ctx = automations
    run_ctx = await tool_context(ctx)
    body = CreateRequest(
        revision=0, request_key="postcommit-revoke", definition=definition(), turn_id=run_ctx.deps.turn_id,
    )
    tools, selected, identity = await reserved_call(ctx, run_ctx, "automation_create", body)
    original = ctx.product.adapter.require_current
    revoked = False

    async def revoke_after_commit(grant):
        nonlocal revoked
        async with ctx.product.database.sessions() as db:
            recorded = await db.scalar(select(Operation.request_key).where(Operation.request_key == body.request_key))
        if recorded and not revoked:
            revoked = True
            await demote(ctx.product)
        return await original(grant)

    monkeypatch.setattr(ctx.product.adapter, "require_current", revoke_after_commit)
    with pytest.raises(ToolFailed, match="was recorded"):
        await tools.call_tool("automation_create", {"request": body}, run_ctx, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "confirmed"
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 1
        assert await db.scalar(select(func.count()).select_from(Operation)) == 1


async def test_unknown_commit_and_unowned_attempt_are_never_cleared(automations, monkeypatch):
    ctx = automations
    run_ctx = await tool_context(ctx)
    body = CreateRequest(
        revision=0, request_key="unknown-db-commit", definition=definition(), turn_id=run_ctx.deps.turn_id,
    )
    tools, selected, identity = await reserved_call(ctx, run_ctx, "automation_create", body)

    async def unknown(*args, **kwargs):
        raise SQLAlchemyError("Synthetic commit acknowledgement lost.")

    monkeypatch.setattr(ctx.service, "mutate", unknown)
    with pytest.raises(SQLAlchemyError):
        await tools.call_tool("automation_create", {"request": body}, run_ctx, selected)
    assert await ctx.service.refused_tool_call(
        run_ctx.deps, identity, "automation_create", "another-attempt",
    ) == "unknown"
    assert await ctx.service.refused_before_tool(run_ctx.deps, identity, "automation_create") == "unknown"
    wrong = RunScope(run_ctx.deps.session_id, run_ctx.deps.turn_id, "another-owner", "test")
    assert await ctx.service.refused_tool_call(wrong, identity, "automation_create", "another-attempt") == "unknown"
    assert await ctx.service.claim_tool_call(run_ctx.deps, identity, "automation_create") is None
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "pending"


async def test_known_rollback_after_sql_write_settles_refused_receipt(automations, monkeypatch):
    ctx = automations
    run_ctx = await tool_context(ctx)
    body = CreateRequest(
        revision=0, request_key="rollback-after-write", definition=definition(), turn_id=run_ctx.deps.turn_id,
    )
    tools, selected, identity = await reserved_call(ctx, run_ctx, "automation_create", body)
    written = False

    async def refuse_inside_transaction(db, *args, **kwargs):
        nonlocal written
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 1
        written = True
        raise HTTPException(409, "Synthetic authoritative refusal after staged SQL.")

    monkeypatch.setattr(ctx.service, "_confirm_effect", refuse_inside_transaction)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool("automation_create", {"request": body}, run_ctx, selected)
    assert written
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 0
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0
        assert (await db.get(Effect, identity)).state == "not_dispatched"


async def test_manual_run_reuses_saved_authority_without_enabling_schedule(automations):
    ctx = automations
    row, _ = await activate(ctx)
    paused = await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(revision=2, request_key="pause-manual"))
    await ctx.service.reconcile(row["id"])
    request = Mutation(revision=paused["revision"], request_key="manual-once")
    result = await ctx.service.run_now(ctx.grant, row["id"], request)
    assert result["status"] == "admitted"
    assert await ctx.service.run_now(ctx.grant, row["id"], request) == result
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Binding, row["id"])).state == "paused"
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
    await demote(ctx.product)
    fresh = await ctx.product.adapter.current(ctx.grant.user_id)
    with pytest.raises(HTTPException):
        await ctx.service.run_now(fresh.grant, row["id"], request.model_copy(update={"request_key": "new-manual"}))


async def test_sync_does_not_restore_consumed_once_action(automations):
    ctx = automations
    at = (datetime.now(UTC) + timedelta(seconds=10)).replace(microsecond=0)
    row, binding = await activate(ctx, triggers=[{
        "id": "default", "schedule": {"frequency": "once", "run_at": at.isoformat(), "timezone": "UTC"},
    }])
    assert (await occurrence(ctx, row, binding, stamp=at))["status"] == "admitted"
    await ctx.service.reconcile(row["id"])
    remote = ctx.remote.schedules[native_schedule_id(row["id"], "default")]
    assert remote.state.paused and not remote.state.limited_actions and remote.state.remaining_actions == 0
    assert (await ctx.service.preview(ctx.grant, row["id"]))["supported"]


async def test_retirement_remains_fenced_until_explicit_owner_migration(automations):
    ctx = automations
    row, original = await activate(ctx)
    retired = await ctx.service.mutate(ctx.grant, "retire", row["id"], Mutation(revision=2, request_key="retire-native"))
    await ctx.service.reconcile(row["id"])
    assert native_schedule_id(row["id"], "default") not in ctx.remote.schedules
    assert (await occurrence(ctx, row, original))["status"] == "skipped"
    paused = await ctx.service.mutate(
        ctx.grant, "pause", row["id"], Mutation(revision=retired["revision"], request_key="pause-retired"),
    )
    assert paused["native_state"] == "deleted"
    with pytest.raises(HTTPException, match="retired"):
        await ctx.service.mutate(
            ctx.grant, "enable", row["id"], Mutation(revision=paused["revision"], request_key="enable-retired"),
        )
    migrated = await ctx.service.mutate(
        ctx.grant, "migrate", row["id"], Migration(revision=paused["revision"], request_key="migrate-retired"),
    )
    assert migrated["native_state"] == "paused"


async def test_automation_captures_real_canonical_connection_ids_and_never_reselects_after_rotation(
    automations, canonical_connections,
):
    ctx = automations
    connections = canonical_connections
    await save_connection(ctx, "linear")
    row, binding = await activate(ctx, plugins=["linear"])
    first = await occurrence(ctx, row, binding)
    assert first["status"] == "admitted"
    async with ctx.product.database.sessions() as db:
        original = json.loads((await db.get(Turn, first["turn_id"])).connections)
    async with ctx.product.database.sessions() as db:
        grants = await connections.resolve_grants(ctx.grant.user_id, original, shared_session=True, surface="automation", db=db)
    assert len(grants) == 1 and grants[0].kind == "linear" and grants[0].credential == ""
    await save_connection(ctx, "linear", access_token="rotated-synthetic-token")
    assert (await occurrence(ctx, row, binding))["status"] == "blocked"
    enabled = await ctx.service.mutate(
        ctx.grant, "enable", row["id"], Mutation(revision=2, request_key="reauthorize-linear"),
    )
    await ctx.service.reconcile(row["id"])
    async with ctx.product.database.sessions() as db:
        current = await db.get(Binding, row["id"])
    second = await occurrence(ctx, enabled, current)
    assert second["status"] == "admitted"
    async with ctx.product.database.sessions() as db:
        fresh = json.loads((await db.get(Turn, second["turn_id"])).connections)
    assert fresh[0] != original[0]
    assert "synthetic-token" not in json.dumps(fresh)
