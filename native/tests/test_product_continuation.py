"""Supplementary continuation tests; actual application/model proof is separate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError

from moyai.contracts import Grant, NewMessage, NewSession, PreparedTurn, RunScope
from moyai.models import Effect, Message, Session, Turn
from moyai.product_continuation import ContinuationRequest, ProductContinuationService, public_conversation
from moyai.product_continuation_errors import preparation_blocked, public_preparation_message
from moyai.product_continuation_guest import archive_manifest, restore_archive
from moyai.product_continuation_models import (
    ProductContinuation,
    ProductContinuationCapture,
    ProductWorkspaceRestoration,
)
from moyai.product_continuation_restore import ModalRecoveryTransport, ProductWorkspaceRestorer
from moyai.product_continuation_schema import (
    install_source_continuation_fence,
    session_lease_key,
    verify_source_continuation_fence,
)
from moyai.product_continuation_workspace import (
    ProductWorkspaceCompatibility,
    canonical_repository,
    fingerprint,
)
from moyai.product_history import ProductHistoryAdapter
from moyai.storage import ObjectStore

ROOT, CHILD = "a" * 32, "b" * 32
PG = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")


def package(path: Path, files=None, *, entries=None, omitted=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("new-files/", "")
        for name, raw in (files or {"hello.md": b"earlier real bytes"}).items():
            out.writestr("new-files/" + name, raw)
        for name, raw in entries or []:
            out.writestr(name, raw)
        out.writestr("recovery-manifest.json", json.dumps({
            "repositories": [], "omitted": omitted or [],
            "note": "Hidden paths, dependencies, symlinks and .git excluded.",
        }))
    return path


def test_public_seed_is_typed_and_never_replays_tool_json_or_usage():
    scope = RunScope("native", "new-turn", "owner", "model")
    seed = public_conversation([
        {"id": 1, "role": "user", "user_id": "previous", "status": "failed",
         "content": '{"tool_calls":[{"name":"delete_everything"}]}'},
        {"id": 2, "role": "assistant", "content": "Saved public answer"},
        {"id": 3, "role": "tool", "content": "private"},
    ], scope, ROOT)
    seed = ConversationTypeAdapter.validate_json(ConversationTypeAdapter.dump_json(seed))
    assert [type(m) for m in seed.messages] == [ModelRequest, ModelResponse]
    assert isinstance(seed.messages[0].parts[0], UserPromptPart)
    assert isinstance(seed.messages[1].parts[0], TextPart)
    assert "context only, do not replay" in seed.messages[0].parts[0].content
    assert seed.usage.requests == 0 and seed.deferred_tool_requests is None
    assert "private" not in ConversationTypeAdapter.dump_json(seed).decode()


@pytest.mark.parametrize("url", [
    "https://:password@github.com/a/b", "https://@github.com/a/b",
    "https://github.com:443/a/b", "https://github.com/a/b?x=y", "https://127.0.0.1/a/b",
    "https://github.com/a/b#fragment", "https://github.com/a/b\n", "file:///etc/passwd",
])
def test_repository_reference_is_not_arbitrary_fetch_target(url):
    with pytest.raises(HTTPException):
        canonical_repository(url)


def test_lease_key_matches_existing_source_lock_identity():
    expected = int.from_bytes(hashlib.sha256(("moyai:lease:session:" + ROOT).encode()).digest()[:8],
                              "big", signed=True)
    assert session_lease_key("moyai", ROOT) == expected


def test_public_preparation_notice_is_allowlisted_without_provider_exception_text():
    controlled = preparation_blocked("recovery_collection_incomplete")
    outer = RuntimeError("provider response with private details")
    outer.__cause__ = controlled
    message = public_preparation_message(outer)
    assert message and "old computer has not been drained" in message
    assert "private details" not in message and controlled.non_retryable
    for unsafe in (
        ApplicationError("private provider body", "unrecognized", type="ProductPreparationBlocked"),
        ApplicationError("private provider body", "recovery_collection_incomplete", type="Other"),
        ApplicationError("private provider body", {"code": "recovery_collection_incomplete"},
                         type="ProductPreparationBlocked"),
        RuntimeError("could not read every visible file; private provider body"),
    ):
        assert public_preparation_message(unsafe) is None


def test_archive_roundtrip_idempotency_private_paths_and_conflicts(tmp_path):
    path = package(tmp_path / "source.zip", {
        "nested/δ.md": b"saved", ".env": b"private-token", ".git/config": b"private-remote",
        ".env.example": b"example",
    })
    manifest = archive_manifest(path)
    assert {item["path"] for item in manifest["files"]} == {"nested/δ.md"}
    assert manifest["excluded_private_files"] == 3
    root = tmp_path / "restored"
    first = restore_archive(str(path), str(root), fingerprint(manifest))
    assert first == restore_archive(str(path), str(root), fingerprint(manifest))
    assert first["manifest_fingerprint"] == fingerprint(manifest) and first["files"] == 1
    (root / "nested/δ.md").write_text("new human edit")
    with pytest.raises(ValueError, match="different file"):
        restore_archive(str(path), str(root), fingerprint(manifest))
    assert (root / "nested/δ.md").read_text() == "new human edit"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "x/../../escape", "x\\escape", "x:y"])
def test_archive_rejects_paths_before_extraction(tmp_path, name):
    path = package(tmp_path / "source.zip", {name: b"bad"})
    with pytest.raises(ValueError, match="Unsafe"):
        archive_manifest(path)


def test_archive_rejects_symlink_duplicate_bounds_and_corruption(tmp_path, monkeypatch):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as archive:
        entry = zipfile.ZipInfo("new-files/link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(ValueError, match="ordinary"):
        archive_manifest(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("new-files/a", "a")
        with pytest.warns(UserWarning):
            archive.writestr("new-files/a", "b")
    with pytest.raises(ValueError, match="duplicate"):
        archive_manifest(path)
    package(path)
    monkeypatch.setattr("moyai.product_continuation_guest.MAX_BYTES", 4)
    with pytest.raises(ValueError, match="budget"):
        archive_manifest(path)


def test_restore_rejects_symlink_parent_and_preserves_other_attempt_temporary(tmp_path):
    path = package(tmp_path / "source.zip", {"nested/a": b"saved"})
    manifest = archive_manifest(path)
    root, outside = tmp_path / "target", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "nested").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        restore_archive(str(path), str(root), fingerprint(manifest))
    assert list(outside.iterdir()) == []
    (root / "nested").unlink()
    (root / "nested").mkdir()
    leftover = root / "nested" / (".moyai-restore-" + manifest["files"][0]["sha256"])
    leftover.write_text("owned by another attempt")
    restore_archive(str(path), str(root), fingerprint(manifest))
    assert leftover.read_text() == "owned by another attempt"


def test_private_environment_requires_exact_selected_permanent_repository_grant():
    context = {"clone_access": "github", "repository_id": 42}
    for grants in ([], [Grant("g", "github", 1, "", policy={"product": {"repository_id": 43}})]):
        with pytest.raises(HTTPException, match="403"):
            ProductWorkspaceCompatibility.authorize_project_grants(context, grants)
    ProductWorkspaceCompatibility.authorize_project_grants(context, [
        Grant("g", "github", 1, "", policy={"product": {"repository_id": 42}}),
    ])


@pytest.mark.parametrize(("status", "pending"), [
    (None, []), (WorkflowExecutionStatus.CONTINUED_AS_NEW, []),
    (WorkflowExecutionStatus.RUNNING, ["old activity"]),
])
async def test_unknown_or_pending_old_workflow_is_never_terminated_or_replayed(status, pending):
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(
            status=status, raw_description=SimpleNamespace(pending_activities=pending),
        )), terminate=AsyncMock(),
    )
    client = SimpleNamespace(get_workflow_handle=lambda name: handle)
    transport = ModalRecoveryTransport(SimpleNamespace(connect=AsyncMock(return_value=client)))
    with pytest.raises(ValueError):
        await transport.verify_idle_workflow({"id": ROOT})
    with pytest.raises(ValueError):
        await transport.drain_workflow({"id": ROOT})
    handle.terminate.assert_not_awaited()


@pytest_asyncio.fixture
async def continuation(tmp_path):
    import test_product_authority as authority
    # Reuse the actual canonical identity/native schema fixture. The fixture
    # creates a unique local database and drops only that database afterwards.
    async for product in authority.product_database.__wrapped__(tmp_path):
        from test_product_identity import google_cookie, request

        from moyai.product_schema import migrate_product_schema

        await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
        await product.database.start()
        cookie, csrf = google_cookie(product.adapter, sub=product.subject)
        principal = await product.adapter.authenticate(request(cookie))
        async with product.database.sessions.begin() as db:
            actor = await product.adapter.project_native_user(db, principal)
        ddl = [
            """CREATE TABLE moyai.runs(
                id text PRIMARY KEY,owner_id text,private_owner_id text DEFAULT '',parent_run_id text DEFAULT '',
                status text DEFAULT 'completed',created_at text DEFAULT '2026-10-01',updated_at text DEFAULT '2026-10-02',
                display_title text DEFAULT 'Earlier session',agent_label text DEFAULT '',prompt text DEFAULT '',
                model text DEFAULT 'old',harness text DEFAULT 'legacy',repo_url text DEFAULT '',
                github_repository_id bigint,environment_id text DEFAULT 'none',environment_build_id text DEFAULT 'none',
                sandbox_provider text DEFAULT 'modal',sandbox_id text DEFAULT '',snapshot_id text DEFAULT '',
                token_hash text DEFAULT 'old-capability',checkpoint_error text DEFAULT '',
                deleted_at text DEFAULT '',deletion_requested_at text DEFAULT '',side_chat_of text DEFAULT '')""",
            "CREATE TABLE moyai.run_roots(run_id text PRIMARY KEY,root_id text)",
            "CREATE TABLE moyai.run_ancestry(run_id text,ancestor_id text)",
            """CREATE TABLE moyai.messages(id bigint PRIMARY KEY,run_id text,role text,user_id text,
                status text DEFAULT 'completed',created_at text DEFAULT '2026-10-01',started_at text DEFAULT '',
                revision bigint DEFAULT 1,content text DEFAULT '')""",
            "CREATE TABLE moyai.durable_sessions(run_id text PRIMARY KEY,revision bigint,state text)",
            "CREATE TABLE moyai.runtime_leases(name text PRIMARY KEY,token text,expires_at timestamptz)",
            "CREATE TABLE moyai.session_goals(run_id text PRIMARY KEY,state text)",
            "CREATE TABLE moyai.events(id bigint PRIMARY KEY,run_id text,kind text,data text)",
            "CREATE TABLE moyai.artifact_objects(name text PRIMARY KEY,reference text,size bigint,sha256 text,created_at text)",
            "CREATE TABLE moyai.session_pins(owner_id text,run_id text)",
            "CREATE TABLE moyai.session_archives(owner_id text,run_id text)",
            "CREATE TABLE moyai.session_folders(id text,owner_id text,name text)",
            "CREATE TABLE moyai.session_folder_memberships(owner_id text,run_id text,folder_id text)",
            "CREATE TABLE moyai.slack_threads(run_id text)",
            "CREATE TABLE moyai.slack_events(run_id text)",
            "CREATE TABLE moyai.user_identities(user_id text,identity_id text)",
        ]
        async with product.source.begin() as db:
            for statement in ddl:
                await db.execute(text(statement))
            await db.execute(text("INSERT INTO moyai.runs(id,owner_id,private_owner_id) VALUES(:id,:owner,:owner)"),
                             {"id": ROOT, "owner": actor.id})
            await db.execute(text("INSERT INTO moyai.run_roots VALUES(:id,:id)"), {"id": ROOT})
            await db.execute(text("INSERT INTO moyai.run_ancestry VALUES(:id,:id)"), {"id": ROOT})
            await db.execute(text("INSERT INTO moyai.messages(id,run_id,role,user_id,content) "
                                  "VALUES(1,:id,'user',:owner,'Earlier public question'),"
                                  "(2,:id,'assistant',:owner,'Earlier public answer'),"
                                  "(3,:id,'tool',:owner,'PRIVATE OLD TOOL OUTPUT')"),
                             {"id": ROOT, "owner": actor.id})
            await db.execute(text("INSERT INTO moyai.durable_sessions VALUES(:id,1,:state)"),
                             {"id": ROOT, "state": json.dumps({"version": 1, "phase": "idle"})})
            await install_source_continuation_fence(db, source_schema="moyai", native_schema=product.schema)
        history = ProductHistoryAdapter(product.adapter, product.source, schema="moyai")
        runtime = SimpleNamespace(
            database=product.database, security=product.database.security, settings=product.settings,
            product_identity=product.adapter, product_history=history, product_files=None,
            objects=ObjectStore(product.settings, product.database.security),
        )
        runtime.product_workspace = ProductWorkspaceCompatibility(runtime)
        runtime.product_continuations = ProductContinuationService(runtime, workspace=runtime.product_workspace)
        product.runtime, product.principal, product.actor = runtime, principal, actor
        product.cookie, product.csrf = cookie, csrf
        product.service = runtime.product_continuations

        async def sql(statement, *, source=product.source, **params):
            async with source.begin() as db:
                return await db.execute(text(statement), params)
        product.sql = sql
        yield product


async def admit(product, **kwargs):
    preview = await product.service.preview(product.principal, ROOT)
    return await product.service.continue_session(
        product.principal, product.actor, ROOT,
        ContinuationRequest(prompt="Continue with a new instruction.", request_key="new-human-turn",
                            expected_source_fingerprint=preview["source_fingerprint"], **kwargs),
    )


@PG
async def test_real_pg_admission_idempotency_history_mapping_and_source_fence(continuation):
    product = continuation
    await verify_source_continuation_fence(product.source, source_schema="moyai", native_schema=product.schema)
    result = await admit(product)
    async with product.database.sessions() as db:
        binding = await db.get(ProductContinuation, ("moyai", ROOT))
        session = await db.get(Session, result["id"])
        assert binding.first_turn_id == result["turn_id"]
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        seed = ConversationTypeAdapter.validate_json(product.database.security.unseal(session.conversation))
        assert len(seed.messages) == 2 and seed.usage.requests == 0
        assert "PRIVATE OLD TOOL" not in str(seed)
        assert session.owner_id == product.actor.id and result["url"] == "/#run=" + ROOT
    again = await product.service.continue_session(product.principal, product.actor, ROOT, ContinuationRequest(
        prompt="Continue with a new instruction.", request_key="new-human-turn",
    ))
    assert again["id"] == result["id"] and again["turn_id"] == result["turn_id"]
    for statement in (
        "UPDATE moyai.runs SET status='running' WHERE id=:id",
        "UPDATE moyai.messages SET run_id='elsewhere' WHERE run_id=:id",
        "DELETE FROM moyai.run_roots WHERE run_id=:id",
        "INSERT INTO moyai.runtime_leases VALUES('session:'||:id,'old',now()+interval '1 minute')",
        "UPDATE moyai.durable_sessions SET state='{}' WHERE run_id=:id",
        "INSERT INTO moyai.session_goals VALUES(:id,'{}')",
    ):
        with pytest.raises(DBAPIError, match="native execution"):
            await product.sql(statement, id=ROOT)
    assert (await product.sql("SELECT token_hash FROM moyai.runs WHERE id=:id", id=ROOT)).scalar() == ""


@PG
@pytest.mark.parametrize(("mutation", "blocker"), [
    ("UPDATE moyai.runs SET status='running'", "active_work"),
    ("UPDATE moyai.messages SET status='queued' WHERE id=1", "active_work"),
    ("INSERT INTO moyai.runtime_leases VALUES('session:'||:id,'old',now()+interval '1 minute')", "live_owner"),
    ("UPDATE moyai.durable_sessions SET state=json_build_object('version',9,'phase','idle')::text", "unsettled_journal"),
    ("INSERT INTO moyai.session_goals VALUES(:id,'{\"status\":\"paused\"}')", "open_goal"),
    ("UPDATE moyai.runs SET sandbox_id='old-live'", "unknown_machine"),
    ("UPDATE moyai.runs SET checkpoint_error='not saved'", "unverified_workspace"),
    ("INSERT INTO moyai.events VALUES(1,:id,'tool_dispatch',json_build_object('outcome','unknown')::text)",
     "uncertain_tools"),
])
async def test_real_pg_unsettled_source_never_admits(continuation, mutation, blocker):
    product = continuation
    await product.sql(mutation, id=ROOT)
    preview = await product.service.preview(product.principal, ROOT)
    assert preview["state"] == "blocked" and blocker in [item["code"] for item in preview["blockers"]]
    with pytest.raises(HTTPException) as failure:
        await admit(product)
    assert failure.value.status_code == 409
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
        assert await db.scalar(select(func.count()).select_from(ProductContinuation)) == 0


@PG
async def test_real_pg_private_owner_no_admin_bypass_and_mapping_delete_protects_old_files(continuation):
    product = continuation
    other = replace(product.principal, id="google:other", role="admin")
    async with product.database.sessions() as db:
        with pytest.raises(HTTPException) as failure:
            await product.service.lookup(db, ROOT, other)
    assert failure.value.status_code == 404
    result = await admit(product)
    async with product.database.sessions.begin() as db:
        session = await db.get(Session, result["id"])
        session.deleted = True
    with pytest.raises(HTTPException) as failure:
        await product.runtime.product_history.require(product.principal, ROOT)
    assert failure.value.status_code == 404


@PG
async def test_real_pg_capture_retained_before_drain_and_immutable_on_retry(continuation, tmp_path):
    product = continuation
    result = await admit(product)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])
    events = []

    class Transport:
        async def export(self, member, target):
            events.append("export")
            package(target)

    restorer = ProductWorkspaceRestorer(product.runtime, transport=Transport())
    member = {"id": ROOT, "snapshot_id": "old-snapshot"}
    saved = await restorer.capture(prepared.scope, member, tmp_path)
    assert saved["size"] > 0 and events == ["export"]
    assert await restorer.capture(prepared.scope, member, tmp_path) == saved
    assert events == ["export"]
    target = tmp_path / "read-back.zip"
    await restorer.materialize(prepared.scope, saved, target)
    assert fingerprint(archive_manifest(target)) == saved["manifest_fingerprint"]
    with pytest.raises(DBAPIError, match="immutable"):
        async with product.database.sessions.begin() as db:
            row = await db.get(ProductContinuationCapture, (result["id"], ROOT))
            row.descriptor = "replaced"


@PG
async def test_real_pg_changed_fingerprint_rolls_back_and_refuses_lost_history(continuation, monkeypatch):
    product = continuation
    preview = await product.service.preview(product.principal, ROOT)
    await product.sql("UPDATE moyai.messages SET content='changed' WHERE id=1")
    with pytest.raises(HTTPException) as failure:
        await product.service.continue_session(product.principal, product.actor, ROOT, ContinuationRequest(
            prompt="new", request_key="new", expected_source_fingerprint=preview["source_fingerprint"],
        ))
    assert failure.value.status_code == 409
    monkeypatch.setattr("moyai.product_continuation.MAX_HISTORY_MESSAGES", 1)
    preview = await product.service.preview(product.principal, ROOT)
    assert preview["requires_recent_history"]
    with pytest.raises(HTTPException) as failure:
        await admit(product)
    assert failure.value.detail["code"] == "history_review_required"


@PG
async def test_real_pg_concurrent_identical_admission_is_one_logical_session_and_turn(continuation):
    product = continuation
    preview = await product.service.preview(product.principal, ROOT)
    request = ContinuationRequest(
        prompt="One new instruction", request_key="concurrent-request",
        expected_source_fingerprint=preview["source_fingerprint"],
    )
    results = await asyncio.gather(*[
        product.service.continue_session(product.principal, product.actor, ROOT, request) for _ in range(2)
    ])
    assert results[0] == results[1]
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductContinuation)) == 1
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1


@PG
async def test_real_pg_recovery_readback_precedes_drain_and_restore_retries_no_overwrite(continuation, tmp_path):
    product = continuation
    product.runtime.product_connections = SimpleNamespace(prepare_repository=AsyncMock())
    await product.sql("UPDATE moyai.runs SET snapshot_id='verified-old-snapshot'")
    result = await admit(product, accept_workspace_omissions=True)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])
    events = []
    destination = tmp_path / "fresh-native"

    class Transport:
        async def verify_idle_workflow(self, member):
            events.append("verify_idle")

        async def export(self, member, target):
            package(target)
            events.append("export")

        async def drain(self, member):
            async with product.database.sessions() as db:
                assert await db.get(ProductContinuationCapture, (result["id"], ROOT)) is not None
            events.append("drain")

        async def drain_workflow(self, member):
            async with product.database.sessions() as db:
                assert await db.get(ProductContinuationCapture, (result["id"], ROOT)) is not None
            events.append("drain_workflow")

        async def native(self, scope, context, reference):
            events.append("native")
            return SimpleNamespace(object_id="sb-fresh-native")

        async def restore(self, sandbox, saved_package, target, digest):
            assert target == "/workspace/previous/" + ROOT
            events.append("restore")
            return restore_archive(str(saved_package), str(destination), digest)

    restorer = ProductWorkspaceRestorer(product.runtime, transport=Transport())
    context = {"repo_url": "https://github.com/example/project"}
    first = await restorer.prepare(prepared, context)
    assert events == ["verify_idle", "export", "drain_workflow", "native", "restore"]
    assert (destination / "hello.md").read_bytes() == b"earlier real bytes"
    assert first.workspace == {"provider": "modal", "id": "sb-fresh-native"}
    (destination / "hello.md").write_text("new human work")
    await restorer.prepare(first, context)
    assert events.count("export") == 1 and events.count("restore") == 1
    assert (destination / "hello.md").read_text() == "new human work"
    assert first.prompt.count("Saved earlier workspace files:") == 1
    assert "No Git checkout was imported" in first.prompt
    product.runtime.product_connections.prepare_repository.assert_not_awaited()


@PG
@pytest.mark.parametrize("revoked", [False, True])
async def test_real_pg_repository_bootstrap_rechecks_access_before_publishing_workspace(continuation, revoked):
    product = continuation
    result = await admit(product)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])
    events = []

    class Transport:
        async def verify_idle_workflow(self, member):
            events.append("verify_idle")

        async def drain_workflow(self, member):
            events.append("drain_workflow")

        async def native(self, scope, context, reference):
            events.append("native")
            return SimpleNamespace(object_id="sb-authorized-repository")

    async def bootstrap(scope, sandbox, context):
        assert sandbox.object_id == "sb-authorized-repository"
        assert context["repo_url"] == "https://github.com/example/project"
        events.append("bootstrap")
        if revoked:
            from moyai.models import User
            async with product.database.sessions.begin() as db:
                actor = await db.get(User, scope.actor_id)
                actor.enabled = False

    product.runtime.product_connections = SimpleNamespace(prepare_repository=bootstrap)
    restorer = ProductWorkspaceRestorer(product.runtime, transport=Transport())
    context = {"repo_url": "https://github.com/example/project"}
    if revoked:
        with pytest.raises(PermissionError):
            await restorer.prepare(prepared, context)
    else:
        restored = await restorer.prepare(prepared, context)
        assert restored.workspace == {"provider": "modal", "id": "sb-authorized-repository"}
        assert "separately authorized repository checkout is at /workspace/repo" in restored.prompt
    assert events == ["verify_idle", "drain_workflow", "native", "bootstrap"]
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductWorkspaceRestoration)) == int(not revoked)
        session = await db.get(Session, result["id"])
        assert (session.workspace is not None) == (not revoked)


@PG
async def test_real_pg_bare_repository_admission_requires_available_scoped_bootstrap(continuation):
    product = continuation
    workspace = product.runtime.product_workspace
    selection = {"repo_url": "https://github.com/example/project",
                 "github_repository_id": 42, "environment_id": "none"}
    async with product.database.sessions.begin() as db:
        assert await workspace.automation_blockers(db, product.actor.id, selection)
        with pytest.raises(HTTPException):
            await workspace.capture_workspace_context(db, product.principal, selection)
        product.runtime.product_connections = SimpleNamespace(prepare_repository=AsyncMock())
        assert await workspace.automation_blockers(db, product.actor.id, selection) == []
        saved = await workspace.capture_workspace_context(db, product.principal, selection)
    assert saved["repository_id"] == 42
    assert saved["environment_build_id"] is None


@PG
async def test_real_pg_revocation_during_capture_prevents_receipt_and_native_execution(continuation, tmp_path):
    product = continuation
    result = await admit(product)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])

    class Transport:
        async def export(self, member, target):
            package(target)
            from moyai.models import User
            async with product.database.sessions.begin() as db:
                actor = await db.get(User, product.actor.id)
                actor.enabled = False

    restorer = ProductWorkspaceRestorer(product.runtime, transport=Transport())
    with pytest.raises(PermissionError):
        await restorer.capture(prepared.scope, {"id": ROOT, "snapshot_id": "old"}, tmp_path)
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductContinuationCapture)) == 0


@PG
async def test_real_pg_collection_failure_stops_before_old_computer_drain(continuation):
    product = continuation
    await product.sql("UPDATE moyai.runs SET snapshot_id='old-snapshot'")
    result = await admit(product, accept_workspace_omissions=True)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])

    async def export(member, target):
        package(target, omitted=["project (repository collection failed)"])

    transport = SimpleNamespace(
        verify_idle_workflow=AsyncMock(), export=export, drain=AsyncMock(),
        drain_workflow=AsyncMock(), native=AsyncMock(), restore=AsyncMock(),
    )
    restorer = ProductWorkspaceRestorer(product.runtime, transport=transport)
    with pytest.raises(ApplicationError, match="could not read every visible file"):
        await restorer.prepare(prepared, {})
    transport.drain.assert_not_awaited()
    transport.drain_workflow.assert_not_awaited()
    transport.native.assert_not_awaited()
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductContinuationCapture)) == 0
        assert await db.scalar(select(func.count()).select_from(ProductWorkspaceRestoration)) == 0


@PG
async def test_real_pg_router_csrf_original_url_and_union_search_no_duplicate(continuation):
    from moyai.product_routes import routes
    product = continuation
    app = FastAPI()

    async def actor():
        return product.actor
    app.include_router(routes(product.runtime, actor))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787",
        cookies={"workspace_session": product.cookie},
    ) as client:
        path = f"/api/product/sessions/{ROOT}/continuation"
        preview = (await client.get(path)).json()
        body = {"prompt": "One actual new turn", "request_key": "http-submit",
                "expected_source_fingerprint": preview["source_fingerprint"]}
        assert (await client.post(path, json=body)).status_code == 403
        response = await client.post(path, json=body, headers={
            "X-CSRF-Token": product.csrf, "Origin": "http://127.0.0.1:8787",
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["url"] == "/#run=" + ROOT
        detail = (await client.get(f"/api/history/sessions/{ROOT}?source=legacy")).json()
        assert detail["continuation"]["id"] == result["id"]
        native = (await client.get(f"/api/history/sessions/{result['id']}?source=native")).json()["session"]
        assert native["id"] == result["id"] and native["source_session_id"] == ROOT
        assert native["url"] == "/#run=" + ROOT
        listing = (await client.get("/api/history/sessions")).json()
        assert [(row["id"], row["url"]) for row in listing["sessions"]] == [(result["id"], "/#run=" + ROOT)]
        search = (await client.get("/api/history/search", params={"q": "Earlier public answer"})).json()
        assert [(row["source"], row["id"]) for row in search["sessions"]] == [("native", result["id"])]
        assert not search["has_more"]
        async with product.database.sessions.begin() as db:
            session = await db.get(Session, result["id"])
            session.deleted = True
        for identity, source in ((ROOT, "legacy"), (result["id"], "native")):
            denied = await client.get(f"/api/history/sessions/{identity}?source={source}")
            assert denied.status_code == 404


@PG
async def test_real_pg_shared_root_never_copies_requesters_private_child_files(continuation):
    product = continuation
    await product.sql("UPDATE moyai.runs SET private_owner_id=''")
    await product.sql("INSERT INTO moyai.runs(id,owner_id,private_owner_id,parent_run_id) VALUES(:id,:owner,:owner,:root)",
                      id=CHILD, owner=product.actor.id, root=ROOT)
    await product.sql("INSERT INTO moyai.run_roots VALUES(:id,:root)", id=CHILD, root=ROOT)
    await product.sql("INSERT INTO moyai.run_ancestry VALUES(:id,:id),(:id,:root)", id=CHILD, root=ROOT)
    result = await product.service.preview(product.principal, ROOT)
    assert result["state"] == "blocked" and "protected_child" in [row["code"] for row in result["blockers"]]


@PG
async def test_real_pg_environment_refresh_context_integrity_and_exact_memory_turn_scope(continuation):
    product = continuation
    environment, first, second = "c" * 32, "d" * 32, "e" * 32
    recipe = json.dumps({"name": "Existing project", "repository": "example/project", "repository_id": 42,
                         "clone_access": "public"})
    await product.sql("""CREATE TABLE moyai.environments(id text PRIMARY KEY,recipe text,revision int,
        enabled int,activate_on_ready int,active_build text,is_default int,updated_at text)""")
    await product.sql("""CREATE TABLE moyai.environment_builds(id text PRIMARY KEY,environment_id text,revision int,
        recipe text,resolved_recipe text,snapshot_id text,commit_sha text,phase text,sandbox_provider text)""")
    await product.sql("INSERT INTO moyai.environments VALUES(:id,:recipe,1,1,0,:build,1,'now')",
                      id=environment, recipe=recipe, build=first)
    for revision, identity in enumerate((first, second), 1):
        await product.sql("INSERT INTO moyai.environment_builds VALUES(:id,:environment,:revision,"
                          ":recipe,:recipe,:image,:commit,'ready','modal')",
                          id=identity, environment=environment, revision=revision, recipe=recipe,
                          image="im-" + identity, commit=str(revision) * 40)
    workspace = product.runtime.product_workspace
    selection = {"environment_id": environment, "repo_url": "https://github.com/example/project",
                 "github_repository_id": 42}
    async with product.database.sessions.begin() as db:
        original = await workspace.capture_workspace_context(db, product.principal, selection)
    assert original["environment_build_id"] == first
    catalog = await workspace.catalog(product.principal)
    assert catalog[0]["name"] == "Existing project" and catalog[0]["ready"] and "recipe" not in catalog[0]
    await product.sql("UPDATE moyai.environments SET active_build=:id", id=second)
    async with product.database.sessions.begin() as db:
        refreshed = await workspace.validate_workspace_context(db, product.principal, selection, original)
    assert refreshed["environment_build_id"] == second
    session = await product.database.create_session(product.actor, NewSession())
    turn = await product.database.submit(product.actor, session.id, NewMessage(prompt="new", request_key="new"))
    async with product.database.sessions.begin() as db:
        await workspace.bind_automation_turn(db, session, turn, refreshed)
    scoped = await workspace.repository_scope(session.id, product.principal.grant, turn_id=turn.id)
    assert scoped["repository_id"] == 42
    with pytest.raises(PermissionError):
        await workspace.repository_scope("other", product.principal.grant, turn_id=turn.id)
    assert await workspace.repository_scope(session.id, product.principal.grant, turn_id="other") is None
    await product.sql("UPDATE moyai.environment_builds SET snapshot_id='changed' WHERE id=:id", id=second)
    with pytest.raises(PermissionError, match="build changed"):
        await workspace.repository_scope(session.id, product.principal.grant, turn_id=turn.id)


@PG
@pytest.mark.parametrize("case", [
    "completed", "other-workspace", "now-private", "revoked", "unselected-account", "public-unavailable",
])
async def test_real_pg_repository_memory_uses_exact_completed_turn_receipt_and_current_access(continuation, case):
    from moyai.product_github_bootstrap import repository_receipt
    product = continuation
    workspace = product.runtime.product_workspace
    session = await product.database.create_session(product.actor, NewSession())
    turn = await product.database.submit(product.actor, session.id, NewMessage(prompt="new", request_key="receipt"))
    await product.database.claim_next()
    prepared = await product.database.prepare(turn.id)
    prepared.workspace = {"provider": "modal", "id": "sb-original"}
    context = {"repo_url": "https://github.com/example/project", "source_schema": "moyai", "repository_id": None}
    context["context_fingerprint"] = fingerprint(context)
    receipt = {
        "repository_id": 42, "repository": "example/project", "commit_sha": "a" * 40,
        "working_dir": "/workspace/repo", "access": "github" if case == "unselected-account" else "public",
        "connection_id": "unselected", "connection_revision": 1,
        "workspace_id": "sb-later" if case == "other-workspace" else "sb-original",
        "workspace_scope": prepared.scope.workspace_key, "requested_url": context["repo_url"],
    }
    async with product.database.sessions.begin() as db:
        await workspace.bind_automation_turn(db, session, turn, context)
        saved_turn = await db.get(Turn, turn.id)
        saved_turn.preparation = product.runtime.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
        saved_turn.status = "completed"
        saved_session = await db.get(Session, session.id)
        saved_session.active_turn, saved_session.status = None, "idle"
        saved_session.workspace = json.dumps({"provider": "modal", "id": "sb-later"})
        db.add(Effect(
            id="repository-receipt", session_id=session.id, turn_id=turn.id, actor_id=product.actor.id,
            tool_name="product_repository_prepare", arguments="{}", fingerprint="a" * 64, state="confirmed",
            resolution=product.runtime.security.seal(receipt),
        ))
    service = SimpleNamespace(database=product.database)

    async def read_receipt(db, **kwargs):
        return await repository_receipt(service, db, **kwargs)

    async def public_lookup(*, repository_id):
        assert repository_id == 42
        if case == "public-unavailable":
            from app.connector_errors import ConnectorError
            raise ConnectorError("Public GitHub rate limit", status_code=403)
        if case == "revoked":
            from moyai.models import User
            async with product.database.sessions.begin() as db:
                user = await db.get(User, product.actor.id)
                user.enabled = False
        return {"id": 42, "private": case == "now-private"}

    product.runtime.product_connections = SimpleNamespace(
        repository_receipt=read_receipt, require_scope=AsyncMock(),
        public_repository=AsyncMock(side_effect=public_lookup),
    )
    if case in {"unselected-account", "revoked"}:
        with pytest.raises((PermissionError, HTTPException)):
            await workspace.repository_scope(session.id, product.principal.grant, turn_id=turn.id)
    else:
        result = await workspace.repository_scope(session.id, product.principal.grant, turn_id=turn.id)
        if case in {"other-workspace", "now-private", "public-unavailable"}:
            assert result is None
            if case == "other-workspace":
                product.runtime.product_connections.public_repository.assert_not_awaited()
        else:
            assert result["repository_id"] == 42
            assert result["context_fingerprint"] == context["context_fingerprint"]
            assert product.runtime.product_connections.require_scope.await_count == 2


@PG
async def test_real_pg_pre_manifest_local_archive_is_discovered_and_read_with_original_revision(continuation, tmp_path):
    product = continuation
    source = package(tmp_path / "original-local.zip")
    revision = "a" * 24

    class Artifacts:
        def info(self, name):
            assert name == ROOT + ".zip"
            return {"name": name, "size": source.stat().st_size, "reference": "", "sha256": "", "revision": revision}

        def open(self, name, *, revision):
            assert name == ROOT + ".zip" and revision == "a" * 24
            return source.open("rb")

    product.runtime.product_files = SimpleNamespace(artifacts=Artifacts())
    preview = await product.service.preview(product.principal, ROOT)
    assert preview["requires_workspace_ack"]
    result = await admit(product, accept_workspace_omissions=True)
    await product.database.claim_next()
    prepared = await product.database.prepare(result["turn_id"])
    async with product.database.sessions() as db:
        binding = await db.get(ProductContinuation, ("moyai", ROOT))
        plan = json.loads(product.runtime.security.unseal(binding.workspace))
    restorer = ProductWorkspaceRestorer(product.runtime)
    saved = await restorer.capture(prepared.scope, plan["members"][0], tmp_path)
    output = tmp_path / "roundtrip.zip"
    await restorer.materialize(prepared.scope, saved, output)
    assert output.read_bytes() == source.read_bytes()


@PG
@pytest.mark.parametrize("source_table", ["slack_threads", "slack_events"])
async def test_real_pg_slack_origin_never_exports_private_history(continuation, source_table):
    product = continuation
    await product.sql(f"INSERT INTO moyai.{source_table} VALUES(:id)", id=ROOT)
    preview = await product.service.preview(product.principal, ROOT)
    assert preview["surface"] == "slack"
    with pytest.raises(HTTPException) as failure:
        await admit(product)
    assert failure.value.status_code == 403
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductContinuation)) == 0
