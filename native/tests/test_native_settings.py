"""Native-store/Temporal qualification, supplementary to actual-app E2E proof."""

from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException, Request
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import func, select
from temporalio.client import ScheduleAlreadyRunningError
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from moyai.agent import make_agent
from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection, Turn, User, new_id
from moyai.runtime import configure
from moyai.security import check_password, password_hash
from moyai.settings_api import routes
from moyai.settings_models import SettingAutomation, SettingAutomationRun, SettingSecret
from moyai.settings_runtime import (
    SETTINGS_ACTIVITIES,
    SETTINGS_WORKFLOWS,
    SettingsAutomationWorkflow,
    SettingsSkills,
    admit_automation,
    automation_readiness,
    reconcile_schedule,
    schedule_id,
    settings_worker_identity,
)
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@asynccontextmanager
async def client_for(runtime, user):
    app = create_app(runtime, run_dispatcher=False)
    # Use the actual app mount when integrated. Before parent integration, this
    # narrow mount applies the same native cookie/CSRF dependency.
    if "/api/settings/skills" not in app.openapi()["paths"]:
        async def actor(request: Request):
            identity = runtime.security.read_session(request.cookies.get(COOKIE, ""))
            if not identity:
                raise HTTPException(401)
            current = await runtime.database.user(identity["user"])
            if current.auth_version != identity["version"]:
                raise HTTPException(401)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
                request.headers.get("x-moyai-csrf", ""), identity["csrf"],
            ):
                raise HTTPException(403)
            return current
        app.include_router(routes(runtime, actor))
        # Production mounts this beside the other API routers, before the SPA
        # fallback. This post-construction test mount must do the same.
        app.router.routes.insert(0, app.router.routes.pop())
    token, csrf = runtime.security.issue_session(user.id, user.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        cookies={COOKIE: token}, headers={"X-Moyai-CSRF": csrf},
    ) as client:
        yield client


async def member(runtime, *, role="member"):
    async with runtime.database.sessions.begin() as db:
        row = User(
            id=new_id(), username=f"fixture-{new_id()}", display_name="Synthetic member",
            password=password_hash("synthetic-test-password"), role=role,
        )
        db.add(row)
    return row


async def automation(runtime, user, *, paused=False):
    session = await runtime.database.create_session(user, NewSession(visibility="private"))
    async with runtime.database.sessions.begin() as db:
        row = SettingAutomation(
            id=new_id(), owner_id=user.id, owner_auth_version=user.auth_version,
            session_id=session.id, name="Synthetic schedule", prompt=runtime.security.seal("Check test data"),
            model="test", connections="[]", interval_minutes=15, paused=paused, state="ready",
        )
        db.add(row)
    return row


async def test_admin_routes_and_csrf_are_enforced(runtime, actor):
    other = await member(runtime)
    async with client_for(runtime, other) as client:
        for page in ("users", "usage", "performance"):
            assert (await client.get(f"/api/settings/{page}")).status_code == 403
        assert (await client.post("/api/settings/users", json={
            "username": "unauthorized", "display_name": "Synthetic", "password": "synthetic-pass",
        })).status_code == 403
        assert (await client.patch(f"/api/settings/users/{actor.id}", json={"role": "member"})).status_code == 403
        assert (await client.delete(f"/api/settings/users/{actor.id}")).status_code == 403
        client.headers.pop("X-Moyai-CSRF")
        assert (await client.post("/api/settings/secrets", json={
            "name": "Token", "value": "synthetic-secret",
        })).status_code == 403
        client.cookies.clear()
        assert (await client.get("/api/settings/memory")).status_code == 401


async def test_user_crud_hashes_password_revokes_sessions_and_preserves_last_admin(runtime, actor):
    async with client_for(runtime, actor) as admin:
        created = await admin.post("/api/settings/users", json={
            "username": "synthetic-user", "display_name": "Synthetic user", "password": "synthetic-pass-123",
        })
        assert created.status_code == 201
        row = created.json()
        assert "password" not in row
        async with runtime.database.sessions() as db:
            saved = await db.get(User, row["id"])
            assert check_password("synthetic-pass-123", saved.password)
        async with client_for(runtime, saved) as old_session:
            changed = await admin.patch(f"/api/settings/users/{saved.id}", json={"role": "admin"})
            assert changed.status_code == 200 and changed.json()["auth_version"] == saved.auth_version + 1
            assert (await old_session.get("/api/me")).status_code == 401
        assert (await admin.delete(f"/api/settings/users/{saved.id}")).json()["enabled"] is False
        assert (await admin.delete(f"/api/settings/users/{actor.id}")).status_code == 409
        assert (await admin.patch(f"/api/settings/users/{actor.id}", json={"role": "member"})).status_code == 409
        assert (await admin.post("/api/settings/users", json={
            "username": "synthetic-user", "display_name": "Duplicate", "password": "synthetic-pass-123",
        })).status_code == 409
        invalid = await admin.post("/api/settings/users", json={
            "username": "../unsafe", "display_name": "Unsafe", "password": "sensitive-short",
        })
        assert invalid.status_code == 422 and "sensitive-short" not in invalid.text


async def test_concurrent_admin_demotion_cannot_remove_both_admins(runtime, actor):
    other = await member(runtime, role="admin")
    async with client_for(runtime, actor) as first, client_for(runtime, other) as second:
        results = await asyncio.gather(
            first.patch(f"/api/settings/users/{actor.id}", json={"role": "member"}),
            second.patch(f"/api/settings/users/{other.id}", json={"role": "member"}),
        )
    assert sorted(result.status_code for result in results) == [200, 409]
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.enabled.is_(True),
        )) == 1


async def test_encrypted_secret_metadata_and_private_shared_boundaries(runtime, actor):
    other = await member(runtime)
    async with client_for(runtime, actor) as admin, client_for(runtime, other) as client:
        private = (await client.post("/api/settings/secrets", json={
            "name": "PrivateToken", "value": "synthetic-private-value",
        })).json()
        shared = (await admin.post("/api/settings/secrets", json={
            "name": "SharedToken", "value": "synthetic-shared-value", "scope": "shared",
        })).json()
        assert "value" not in private and "value" not in shared
        listed = (await client.get("/api/settings/secrets")).json()
        assert {row["id"] for row in listed["secrets"]} == {private["id"], shared["id"]}
        assert "synthetic-private-value" not in json.dumps(listed)
        assert not listed["capabilities"]["agent_access"]
        assert (await admin.patch(f"/api/settings/secrets/{private['id']}", json={"description": "steal"})).status_code == 404
        assert (await admin.delete(f"/api/settings/secrets/{private['id']}")).status_code == 404
        assert (await client.patch(f"/api/settings/secrets/{shared['id']}", json={"value": "replace"})).status_code == 403
        assert (await client.post("/api/settings/secrets", json={
            "name": "Escalate", "value": "synthetic-value", "scope": "shared",
        })).status_code == 403
        assert (await client.patch(f"/api/settings/secrets/{private['id']}", json={"scope": "shared"})).status_code == 403
        assert (await client.post("/api/settings/secrets", json={
            "name": "../bad", "value": "not-echoed-secret",
        })).status_code == 422
        assert (await client.patch(f"/api/settings/secrets/{private['id']}", json={
            "value": "rotated-synthetic-value",
        })).status_code == 200
        async with runtime.database.sessions() as db:
            saved = await db.get(SettingSecret, private["id"])
            assert "rotated-synthetic-value" not in saved.value
            assert runtime.security.unseal(saved.value).decode() == "rotated-synthetic-value"
        assert (await client.delete(f"/api/settings/secrets/{private['id']}")).status_code == 200


async def test_demoted_owner_cannot_edit_or_unshare_shared_connection(runtime, actor):
    publisher = await member(runtime, role="admin")
    body = {
        "name": "Synthetic shared connection", "kind": "github",
        "credential": "synthetic-connection-value", "shared": True,
        "enabled": True, "read_only": True,
    }
    async with client_for(runtime, publisher) as original, client_for(runtime, actor) as admin:
        created = await original.post("/api/connections", json=body)
        assert created.status_code == 200
        shared = created.json()
        private = (await original.post("/api/connections", json={
            **body, "name": "Synthetic personal connection", "shared": False,
        })).json()
        assert (await admin.patch(f"/api/settings/users/{publisher.id}", json={"role": "member"})).status_code == 200
        assert (await original.put(f"/api/connections/{shared['id']}", json={
            **body, "shared": False,
        })).status_code == 401

    demoted = await runtime.database.user(publisher.id)
    async with client_for(runtime, demoted) as client:
        for changes in (
            {"shared": False},
            {"shared": False, "enabled": False, "read_only": False},
            {"name": "Changed shared connection", "credential": "synthetic-replacement"},
        ):
            assert (await client.put(f"/api/connections/{shared['id']}", json={
                **body, **changes,
            })).status_code == 403
        # Demotion does not remove normal personal connection management.
        assert (await client.put(f"/api/connections/{private['id']}", json={
            **body, "shared": False, "name": "Updated personal connection",
        })).status_code == 200
        assert (await client.post("/api/connections", json=body)).status_code == 403
    async with runtime.database.sessions() as db:
        saved = await db.get(Connection, shared["id"])
        assert saved.shared and saved.enabled and saved.read_only
        assert saved.name == body["name"] and saved.revision == shared["revision"]
        assert runtime.security.unseal(saved.credential).decode() == body["credential"]


async def test_native_memory_uses_actor_namespace_and_optimistic_versions(runtime, actor, active):
    _, _, prepared = active
    other = await member(runtime)
    native_path = f"{prepared.scope.memory_namespace}/main/MEMORY.md"
    result = await runtime.stores.memory.write(native_path, "Native stored fact", expected_version=None)
    async with client_for(runtime, actor) as admin, client_for(runtime, other) as client:
        listing = (await admin.get("/api/settings/memory")).json()
        row = listing["memories"][0]
        assert row["content"] == "Native stored fact" and row["version"] == result.version
        assert (await client.get("/api/settings/memory")).json()["memories"] == []
        assert (await client.get("/api/settings/memory", params={"namespace": row["namespace"]})).status_code == 404
        own = (await client.put("/api/settings/memory", json={"content": "  Own memory\n"})).json()
        assert own["content"] == "  Own memory\n"
        assert (await admin.get("/api/settings/memory", params={"namespace": own["namespace"]})).status_code == 404
        assert (await admin.put("/api/settings/memory", json={"content": "stale"})).status_code == 409
        edited = await admin.put("/api/settings/memory", json={"content": "Revised", "version": row["version"]})
        assert edited.status_code == 200
        assert (await runtime.stores.memory.read(native_path, max_chars=100)).content == "Revised"
        assert (await admin.delete("/api/settings/memory", params={
            "path": "MEMORY.md", "version": row["version"],
        })).status_code == 409
        for path in ("../MEMORY.md", "nested/file.md", "topic..md", "/absolute.md"):
            assert (await admin.put("/api/settings/memory", params={"path": path}, json={
                "content": "unsafe",
            })).status_code == 422
        assert (await admin.delete("/api/settings/memory", params={
            "path": "MEMORY.md", "version": edited.json()["version"],
        })).status_code == 200
        assert await runtime.stores.memory.read(native_path, max_chars=100) is None


async def test_saved_skills_are_native_deferred_capabilities_with_correct_scopes(runtime, actor, active):
    _, _, prepared = active
    other = await member(runtime)
    async with client_for(runtime, actor) as admin, client_for(runtime, other) as client:
        assert any(row["scope"] == "bundled" for row in (await admin.get("/api/settings/skills")).json()["skills"])
        body = {"name": "synthetic-skill", "description": "Handle synthetic input", "content": "Emit SYNTHETIC_MARKER"}
        own = (await admin.post("/api/settings/skills", json=body)).json()
        shared = (await admin.post("/api/settings/skills", json={**body, "scope": "shared"})).json()
        other_private = (await client.post("/api/settings/skills", json=body)).json()
        assert (await client.delete(f"/api/settings/skills/{own['id']}")).status_code == 404
        assert (await client.delete(f"/api/settings/skills/{shared['id']}")).status_code == 403
        assert (await admin.delete(f"/api/settings/skills/{other_private['id']}")).status_code == 404
        assert (await client.post("/api/settings/skills", json={**body, "scope": "shared"})).status_code == 403
        assert (await admin.post("/api/settings/skills", json={**body, "name": "../bad"})).status_code == 422
        capability = SettingsSkills(runtime.database)
        catalog = await capability.catalog(prepared.scope)
        assert {row["id"] for row in catalog} == {own["id"], shared["id"]}
        # authorize_scope verifies the genuine persisted scope. A shared session
        # needs its own native turn, not a caller-mutated authority.
        session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
        turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Synthetic", request_key="shared"))
        await runtime.database.claim_next()
        shared_scope = (await runtime.database.prepare(turn.id)).scope
        assert {row["id"] for row in await capability.catalog(shared_scope)} == {shared["id"]}

        captured = []
        def model(messages, info):
            captured.append(info)
            if len(captured) == 1:
                tool = next(tool for tool in info.function_tools if tool.name == "load_capability")
                properties = tool.parameters_json_schema["properties"]
                key = next(iter(properties))
                return ModelResponse(parts=[ToolCallPart(
                    "load_capability", {key: f"saved_skill_{own['id'].replace('-', '_')}"},
                )])
            assert "SYNTHETIC_MARKER" in str(messages)
            return ModelResponse(parts=[TextPart("Loaded stored skill")])
        native = Agent(FunctionModel(model), deps_type=type(prepared.scope), capabilities=[capability])
        assert (await native.run("Load the saved skill", deps=prepared.scope)).output == "Loaded stored skill"
        assert (await admin.patch(f"/api/settings/skills/{own['id']}", json={**body, "enabled": False})).status_code == 200
        assert {row["id"] for row in await capability.catalog(prepared.scope)} == {shared["id"]}


async def test_analytics_uses_actual_rows_and_never_private_content_or_fabricated_cost(runtime, actor):
    other = await member(runtime)
    session = await runtime.database.create_session(other, NewSession(
        title="Never disclose this private title", visibility="private",
    ))
    first = await runtime.database.submit(other, session.id, NewMessage(
        prompt="Never disclose this private prompt", request_key="analytics-first",
    ))
    await runtime.database.claim_next()
    await runtime.database.prepare(first.id)
    second = await runtime.database.submit(other, session.id, NewMessage(prompt="Secret second", request_key="analytics-second"))
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with runtime.database.sessions.begin() as db:
        one, two = await db.get(Turn, first.id), await db.get(Turn, second.id)
        one.created_at = start.isoformat()
        one.accepted_at = (start + timedelta(seconds=2)).isoformat()
        one.finished_at = (start + timedelta(seconds=5)).isoformat()
        one.status = "completed"
        one.usage = json.dumps({"input_tokens": 20, "output_tokens": 10, "requests": 1, "cost": 999})
        two.created_at = (start + timedelta(minutes=1)).isoformat()
        two.model = "=HYPERLINK(\"unsafe\")"
        two.status = "failed"
        two.usage = json.dumps({"input_tokens": -99, "output_tokens": True})
    params = {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()}
    async with client_for(runtime, actor) as admin:
        response = await admin.get("/api/settings/usage", params={**params, "limit": 1})
        assert response.status_code == 200
        report = response.json()
        assert report["summary"]["turns"] == 2 and report["summary"]["total_tokens"] == 30
        assert report["summary"]["mean_latency_ms"] == 3000
        assert report["summary"]["cost_usd"] is None and report["summary"]["unpriced_turns"] == 2
        assert report["summary"]["priced_turns"] == 0
        assert report["summary"]["sessions"] == 1 and len(report["rows"]) == 1
        assert report["by_user"] == [{
            "user_id": other.id, **report["summary"],
        }]
        assert "Never disclose" not in response.text and "session_id" not in response.text
        assert all(row["cost_usd"] is None for row in report["rows"])
        filtered = (await admin.get("/api/settings/performance", params={**params, "status": "completed"})).json()
        assert filtered["total"] == 1 and filtered["rows"][0]["queue_ms"] == 2000
        exported = await admin.get("/api/settings/usage", params={**params, "format": "csv", "limit": 1})
        assert exported.status_code == 200 and "'=HYPERLINK" in exported.text
        assert "unpriced" in exported.text and "Never disclose" not in exported.text
        assert (await admin.get("/api/settings/usage", params={"start": "bad"})).status_code == 422
        assert (await admin.get("/api/settings/usage", params={"limit": -1})).status_code == 422


async def test_admission_is_idempotent_recovers_receipt_loss_and_checks_current_authority(runtime, actor):
    row = await automation(runtime, actor)
    first, second = await asyncio.gather(
        admit_automation(runtime, row.id, "occurrence-1"), admit_automation(runtime, row.id, "occurrence-1"),
    )
    assert first == second and first["status"] == "admitted"
    async with runtime.database.sessions.begin() as db:
        receipt = await db.scalar(select(SettingAutomationRun).where(SettingAutomationRun.automation_id == row.id))
        receipt.turn_id = None  # persisted submit, lost admission acknowledgment
    assert await admit_automation(runtime, row.id, "occurrence-1") == first
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(SettingAutomation, row.id)
        saved.paused = True
    assert (await admit_automation(runtime, row.id, "occurrence-2"))["status"] == "skipped"
    async with runtime.database.sessions.begin() as db:
        (await db.get(SettingAutomation, row.id)).paused = False
        (await db.get(User, actor.id)).auth_version += 1
    assert (await admit_automation(runtime, row.id, "occurrence-3"))["status"] == "denied"
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1


async def test_automation_revocation_between_owner_lookup_and_native_submit_denies_admission(runtime, actor, monkeypatch):
    owner = await member(runtime, role="admin")
    row = await automation(runtime, owner)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.database.user

    async def delayed_user(identity):
        loaded = await original(identity)
        if identity == owner.id and not entered.is_set():
            entered.set()
            await release.wait()
        return loaded

    monkeypatch.setattr(runtime.database, "user", delayed_user)
    admission = asyncio.create_task(admit_automation(runtime, row.id, "revoke-during-lookup"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with client_for(runtime, actor) as client:
            assert (await client.patch(f"/api/settings/users/{owner.id}", json={"role": "member"})).status_code == 200
        release.set()
        assert await asyncio.wait_for(admission, timeout=5) == {"status": "denied"}
    finally:
        release.set()
        await asyncio.gather(admission, return_exceptions=True)
    assert await runtime.database.claim_next() is None
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


@pytest.mark.parametrize("create", [False, True])
async def test_automation_mutation_does_not_return_prompt_after_revocation_during_rpc(
    runtime, actor, monkeypatch, create,
):
    from moyai import settings_api

    owner = await member(runtime)
    row = await automation(runtime, owner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_rpc(*args, **kwargs):
        entered.set()
        await release.wait()

    async def ready(*args):
        return {"create": True}

    monkeypatch.setattr(settings_api, "reconcile_schedule", delayed_rpc)
    monkeypatch.setattr(settings_api, "automation_readiness", ready)
    async with client_for(runtime, owner) as client, client_for(runtime, actor) as administrator:
        mutation = asyncio.create_task(client.post("/api/settings/automations", json={
            "name": "Created during revocation", "prompt": "Check test data",
            "session_id": row.session_id, "interval_minutes": 15,
        }) if create else client.patch(
            f"/api/settings/automations/{row.id}", json={"paused": True},
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (await administrator.patch(
                f"/api/settings/users/{owner.id}", json={"enabled": False},
            )).status_code == 200
            release.set()
            response = await asyncio.wait_for(mutation, timeout=5)
            assert response.status_code == 401
            assert "Check test data" not in response.text
        finally:
            release.set()
            await asyncio.gather(mutation, return_exceptions=True)


async def test_schedule_unavailable_disables_creation_but_logs_and_local_pause_remain_real(runtime, actor, monkeypatch):
    row = await automation(runtime, actor)
    async def unavailable():
        raise RuntimeError("synthetic unavailable")
    monkeypatch.setattr(runtime, "connect", unavailable)
    other = await member(runtime)
    async with client_for(runtime, actor) as admin, client_for(runtime, other) as stranger:
        report = (await admin.get("/api/settings/automations")).json()
        assert not report["capabilities"]["create"] and not report["capabilities"]["runtime_available"]
        assert (await admin.post("/api/settings/automations", json={
            "name": "Offline", "prompt": "Synthetic", "session_id": row.session_id, "interval_minutes": 15,
        })).status_code == 503
        assert (await stranger.get(f"/api/settings/automations/{row.id}/logs")).status_code == 404
        assert (await stranger.patch(f"/api/settings/automations/{row.id}", json={"paused": True})).status_code == 404
        assert (await admin.patch(f"/api/settings/automations/{row.id}", json={"paused": True})).status_code == 503
        assert (await admit_automation(runtime, row.id, "after-pause"))["status"] == "skipped"
        assert (await admin.get(f"/api/settings/automations/{row.id}/logs")).json() == {"logs": []}


async def test_real_temporal_schedule_pause_delete_and_durable_admission(runtime, actor):
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(data_converter=converter)
    configure(runtime)
    async with environment:
        runtime.client = environment.client
        assert not (await automation_readiness(runtime))["create"]
        async with Worker(
            runtime.client, task_queue=runtime.settings.temporal_queue,
            workflows=SETTINGS_WORKFLOWS, activities=SETTINGS_ACTIVITIES, identity=settings_worker_identity(),
        ):
            for _ in range(40):
                if (await automation_readiness(runtime))["create"]:
                    break
                await asyncio.sleep(0.1)
            assert (await automation_readiness(runtime))["create"]
            session = await runtime.database.create_session(actor, NewSession(visibility="private"))
            async with client_for(runtime, actor) as client:
                created = await client.post("/api/settings/automations", json={
                    "name": "Real schedule", "prompt": "Synthetic task", "session_id": session.id,
                    "interval_minutes": 60, "paused": True,
                })
                assert created.status_code == 201, created.text
                identity = created.json()["id"]
                handle = runtime.client.get_schedule_handle(schedule_id(identity))
                assert (await handle.describe()).schedule.state.paused
                assert (await client.patch(f"/api/settings/automations/{identity}", json={"paused": False})).status_code == 200
                assert not (await handle.describe()).schedule.state.paused
                await handle.trigger()
                for _ in range(100):
                    logs = (await client.get(f"/api/settings/automations/{identity}/logs")).json()["logs"]
                    if logs and logs[0]["turn_id"]:
                        break
                    await asyncio.sleep(0.1)
                assert logs and logs[0]["status"] == "queued"
                # Replay an actual admission workflow, with a distinct occurrence.
                execution = await runtime.client.start_workflow(
                    SettingsAutomationWorkflow.run, identity, id=f"settings-replay-{new_id()}",
                    task_queue=runtime.settings.temporal_queue,
                )
                assert (await execution.result())["status"] == "admitted"
                history = await execution.fetch_history()
                await Replayer(interceptors=[ActivityCapacity()], workflows=SETTINGS_WORKFLOWS, data_converter=converter).replay_workflow(history)
                assert (await client.delete(f"/api/settings/automations/{identity}")).status_code == 200
                with pytest.raises(RPCError) as missing:
                    await handle.describe()
                assert missing.value.status == RPCStatusCode.NOT_FOUND
                assert (await admit_automation(runtime, identity, "after-delete"))["status"] == "skipped"
                # Missing ack reconciliation is idempotent, including deletion.
                await reconcile_schedule(runtime, identity)


@pytest.mark.parametrize("page", ["memory", "overview", "usage", "performance", "secrets"])
async def test_long_read_rechecks_revoked_actor_before_returning(runtime, actor, monkeypatch, page):
    original = runtime.database.user
    calls = 0

    async def revoke_before_response(identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with runtime.database.sessions.begin() as db:
                (await db.get(User, identity)).auth_version += 1
        return await original(identity)

    async def unavailable():
        raise RuntimeError("synthetic offline")

    monkeypatch.setattr(runtime.database, "user", revoke_before_response)
    monkeypatch.setattr(runtime, "connect", unavailable)
    async with client_for(runtime, actor) as client:
        response = await client.get(f"/api/settings/{page}")
        assert response.status_code == 401
        assert "counts" not in response.text and "memories" not in response.text and "rows" not in response.text


async def test_memory_write_rechecks_authorization_after_namespace_lookup(runtime, actor, monkeypatch):
    from moyai import settings_api
    entered, release = asyncio.Event(), asyncio.Event()
    original = settings_api.memory_path

    async def delayed(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(settings_api, "memory_path", delayed)
    async with client_for(runtime, actor) as client:
        saving = asyncio.create_task(client.put("/api/settings/memory", json={"content": "must not persist"}))
        await entered.wait()
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, actor.id)).auth_version += 1
        release.set()
        assert (await saving).status_code == 401
    assert await runtime.stores.memory.list_paths(limit=10) == []


async def test_user_access_changes_fence_existing_native_turns(runtime, actor, active):
    _, turn, prepared = active
    another_admin = await member(runtime, role="admin")
    async with client_for(runtime, another_admin) as client:
        assert (await client.patch(f"/api/settings/users/{actor.id}", json={"role": "member"})).status_code == 200
    with pytest.raises(PermissionError):
        await runtime.database.authorize_scope(prepared.scope)
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "stopped"


async def test_stale_schedule_rpc_reconciles_latest_remote_state(runtime, actor, monkeypatch):
    row = await automation(runtime, actor, paused=True)
    entered, release = asyncio.Event(), asyncio.Event()

    class Handle:
        paused = False
        pauses = 0

        async def pause(self, **kwargs):
            self.pauses += 1
            if self.pauses == 1:
                entered.set()
                await release.wait()
            self.paused = True

        async def unpause(self, **kwargs):
            self.paused = False

    handle = Handle()

    class Client:
        def get_schedule_handle(self, identity):
            return handle

        async def create_schedule(self, *args):
            raise ScheduleAlreadyRunningError()

    async def connect():
        return Client()

    monkeypatch.setattr(runtime, "connect", connect)
    older = asyncio.create_task(reconcile_schedule(runtime, row.id))
    await entered.wait()
    async with runtime.database.sessions.begin() as db:
        current = await db.get(SettingAutomation, row.id)
        current.paused, current.state, current.revision = False, "pending", current.revision + 1
    await reconcile_schedule(runtime, row.id)
    assert not handle.paused
    release.set()
    await older
    assert not handle.paused
    async with runtime.database.sessions() as db:
        assert (await db.get(SettingAutomation, row.id)).state == "ready"


async def test_saved_skill_runs_through_integrated_native_temporal_agent_and_replays(runtime, actor, active):
    """Controlled model, real native worker, real store and Temporal history."""
    _, turn, prepared = active
    async with client_for(runtime, actor) as client:
        saved = (await client.post("/api/settings/skills", json={
            "name": "temporal-test", "description": "Load this saved synthetic instruction",
            "content": "Return STORED_SKILL_TEMPORAL_MARKER.",
        })).json()
    capability_id = f"saved_skill_{saved['id'].replace('-', '_')}"

    def respond(messages, info):
        loaded = [
            part for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "load_capability"
        ]
        if not loaded:
            assert capability_id in str(info.instructions)
            # Bundled native Skills are still part of the same deferred catalog.
            assert "coding" in str(info.instructions)
            return ModelResponse(parts=[ToolCallPart(
                "load_capability", {"id": capability_id}, tool_call_id="load-saved-skill",
            )])
        assert "STORED_SKILL_TEMPORAL_MARKER" in str(loaded)
        return ModelResponse(parts=[TextPart("Stored skill loaded in native Temporal execution.")])

    async def stream(messages, info):
        for part in respond(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args),
                                       tool_call_id=part.tool_call_id)}

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"), skills_directory="skills",
    )
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            execution = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            result = await asyncio.wait_for(execution.result(), timeout=45)
            assert result["status"] == "completed"
            history = await execution.fetch_history()
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    async with runtime.database.sessions() as db:
        recorded = await db.get(Turn, turn.id)
        assert runtime.security.unseal(recorded.result).decode() == "Stored skill loaded in native Temporal execution."
    assert len(await runtime.stores.steps.list_runs(conversation_id=prepared.scope.conversation_id)) == 1


async def test_general_preferences_persist_partially_and_are_user_scoped(runtime, actor):
    other = await member(runtime)
    async with runtime.database.sessions.begin() as db:
        row = await db.get(User, actor.id)
        row.preferences = json.dumps({"unrelated": {"keep": True}, "general": {"future": "preserve"}})
    async with client_for(runtime, actor) as client:
        defaults = {"delivery_mode": "when_idle", "connection_ids": None}
        assert (await client.get("/api/settings/general")).json() == defaults
        # Concurrent partial changes must merge under the user-row lock.
        responses = await asyncio.gather(
            client.patch("/api/settings/general", json={"delivery_mode": "asap"}),
            client.patch("/api/settings/general", json={"connection_ids": ["selected"]}),
        )
        assert all(response.status_code == 200 for response in responses)
        saved = {"delivery_mode": "asap", "connection_ids": ["selected"]}
        assert (await client.get("/api/settings/general")).json() == saved
        assert (await client.get("/api/me")).json()["preferences"] == saved
        async with runtime.database.sessions() as db:
            document = json.loads((await db.get(User, actor.id)).preferences)
            assert document["unrelated"] == {"keep": True}
            assert document["general"]["future"] == "preserve"
        async with client_for(runtime, other) as other_client:
            assert (await other_client.get("/api/settings/general")).json() == defaults
        assert (await client.patch("/api/settings/general", json={"connection_ids": []})).json()["connection_ids"] == []
        assert (await client.patch("/api/settings/general", json={"connection_ids": None})).json()["connection_ids"] is None
        for invalid in ({"delivery_mode": None}, {"delivery_mode": "invalid"}, {"unknown": True},
                        {"connection_ids": [""]}, {"connection_ids": ["x"] * 31}):
            assert (await client.patch("/api/settings/general", json=invalid)).status_code == 422
        client.headers.pop("X-Moyai-CSRF")
        assert (await client.patch("/api/settings/general", json={"delivery_mode": "when_idle"})).status_code == 403
        client.cookies.clear()
        assert (await client.get("/api/settings/general")).status_code == 401
