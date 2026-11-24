"""Gateway contract and authority tests; real-app proof is recorded separately."""

import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from sqlalchemy import update
from temporalio.testing import ActivityEnvironment
from test_native_settings import automation, client_for, member
from test_product_skill_catalog import product as product  # noqa: PLC0414
from test_product_skill_catalog import skill_product as skill_product  # noqa: PLC0414

from moyai.activities import prepare_turn
from moyai.api_views import InvocableSkillPage
from moyai.connections import connection_tools
from moyai.contracts import NewMessage, NewSession, PreparedTurn
from moyai.delegation import DelegationService
from moyai.gateway import GatewayClient, gateway_client, gateway_rows, unpack_skill
from moyai.gateway_config import (
    GatewayCapability,
    GatewayConfig,
    GatewayDiscovery,
    GatewaySkill,
)
from moyai.gateway_tools import gateway_toolset
from moyai.models import Connection, User
from moyai.preferences.settings_models import SettingAutomation
from moyai.preferences.settings_runtime import admit_automation
from moyai.runtime import Runtime, configure
from moyai.skill_mentions import marker


def discovery(**kwargs):
    return GatewayDiscovery(
        skills=GatewayCapability(status="ready", count=1),
        mcp=GatewayCapability(status="unavailable"),
        skill_catalog=[GatewaySkill(name="incident-review", description="Review an incident", version="1.0.0")],
        **kwargs,
    )


async def connect(runtime, actor, monkeypatch, *, shared=False, read_only=True, allowed_tools=None, require_approval=True):
    async def discover():
        return discovery()

    def client(*args, **kwargs):
        instance = GatewayClient(*args, **kwargs)
        monkeypatch.setattr(instance, "discover", discover)
        return instance

    monkeypatch.setattr("moyai.gateway.GatewayClient", client)
    async with client_for(runtime, actor) as client:
        result = await client.post("/api/connections", json={
            "name": "Engineering gateway", "kind": "litellm", "credential": "test-gateway-key",
            "shared": shared, "read_only": read_only,
            "policy": {"gateway": {"url": "https://gateway.example.com/prefix/v1"},
                       "allowed_tools": allowed_tools, "require_approval": require_approval},
        })
        assert result.status_code == 200, result.text
        return result.json()


@pytest.mark.parametrize(("url", "root"), [
    ("https://gateway.example.com", "https://gateway.example.com"),
    ("https://gateway.example.com/v1/", "https://gateway.example.com"),
    ("https://gateway.example.com/prefix/v1", "https://gateway.example.com/prefix"),
])
def test_normalizes_one_gateway_root_without_losing_prefix(url, root):
    assert GatewayConfig(url=url).url == root


async def test_discovery_is_independent_and_only_reads_metadata(monkeypatch):
    calls = []
    async def request(self, method, path, **kwargs):
        calls.append((method, path))
        req = httpx2.Request(method, self.config.url + path)
        raise httpx2.HTTPStatusError("private provider error", request=req, response=httpx2.Response(403, request=req))
    monkeypatch.setattr(GatewayClient, "request", request)
    result = await GatewayClient(GatewayConfig(url="https://example.com", mcp=False), "secret", []).discover()
    assert result.skills.status == "unauthorized" and result.skill_catalog == []
    assert result.mcp.status == "disabled"
    assert calls == [("GET", "/skill-hub/skills")]
    assert "secret" not in result.model_dump_json()


async def test_personal_connection_leaves_inference_unchanged_and_never_exposes_key(runtime, actor, monkeypatch):
    row = await connect(runtime, actor, monkeypatch)
    assert "test-gateway-key" not in json.dumps(row)
    async with client_for(runtime, actor) as client:
        assert (await client.get("/api/me")).json()["models"] == runtime.settings.models
        assert "test-gateway-key" not in (await client.get("/api/gateway/defaults")).text
        result = await client.get("/api/skills", params={"shared": False})
        assert result.status_code == 200, result.text
        skills = result.json()["skills"]
        assert any(skill["reference"] == f"gateway-{row['id']}:incident-review" for skill in skills)
    other = await member(runtime)
    assert await gateway_rows(runtime.database, other.id) == []
    assert await gateway_rows(runtime.database, actor.id, shared=True) == []
    async with runtime.database.sessions() as db:
        stored = await db.get(Connection, row["id"])
        assert stored.credential != "test-gateway-key"
        assert runtime.security.unseal(stored.credential) == b"test-gateway-key"


async def test_personal_gateway_cannot_be_shared_even_by_admin(runtime, actor, monkeypatch):
    row = await connect(runtime, actor, monkeypatch)
    async with client_for(runtime, actor) as client:
        body = {"name": "Shared gateway", "kind": "litellm", "credential": "key", "shared": True,
                "policy": {"gateway": {"url": "https://example.com"}}}
        assert (await client.post("/api/connections", json=body)).status_code == 422
        assert (await client.put(f"/api/connections/{row['id']}", json=body)).status_code == 422
    other = await member(runtime)
    async with runtime.database.sessions() as db:
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(db, other.id, [row["id"]])
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(db, actor.id, [row["id"]], shared_session=True)


async def test_skill_requires_explicit_invocation_and_downloads_only_at_execution(runtime, actor, monkeypatch):
    row = await connect(runtime, actor, monkeypatch)
    calls = []
    async def skill(self, name):
        calls.append(name)
        return GatewaySkill(name=name), {"SKILL.md": "Return the violet code.", "references/code.txt": "VIOLET-42"}
    monkeypatch.setattr(GatewayClient, "skill", skill)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    prompt = marker(f"gateway-{row['id']}:incident-review") + " Summarize this incident."
    normalized = await runtime.skill_invocations.normalize(prompt, actor, session)
    assert calls == []
    turn = await runtime.admission.submit(actor, session.id, NewMessage(prompt=normalized, request_key="skill"))
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    assert await runtime.skill_invocations.expand("Summarize this incident.", prepared.scope) == "Summarize this incident."
    assert calls == []
    expanded = await runtime.skill_invocations.expand(normalized, prepared.scope)
    assert calls == ["incident-review"] and "VIOLET-42" in expanded and "Return the violet code." in expanded
    assert [item.id for item in prepared.scope.grants] == [row["id"]]
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Connection).where(Connection.id == row["id"]).values(enabled=False, revision=2))
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.skill_invocations.expand(normalized, prepared.scope)
    assert calls == ["incident-review"]


async def test_connection_url_change_requires_key_and_client_cannot_invent_skills(runtime, actor, monkeypatch):
    row = await connect(runtime, actor, monkeypatch)
    body = {
        "name": row["name"], "kind": "litellm", "credential": "", "policy": row["policy"],
    }
    body["policy"]["gateway_discovery"]["skill_catalog"] = [{"name": "injected-skill"}]
    async with client_for(runtime, actor) as client:
        saved = await client.put(f"/api/connections/{row['id']}", json=body)
        assert saved.status_code == 200
        assert [skill["name"] for skill in saved.json()["policy"]["gateway_discovery"]["skill_catalog"]] == ["incident-review"]
        body["policy"]["gateway"]["url"] = "https://different.example.com"
        assert (await client.put(f"/api/connections/{row['id']}", json=body)).status_code == 422


@pytest.mark.parametrize("files", [
    {"../SKILL.md": "bad"}, {"SKILL.md": "ok", "/etc/passwd": "bad"},
    {"SKILL.md": "ok", "references/../../bad": "bad"}, {"README.md": "missing"},
    {"SKILL.md": "x" * 450_001}, {"SKILL.md": b"\xff"},
])
def test_archives_are_bounded_and_cannot_escape(files):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)
    with pytest.raises(HTTPException):
        unpack_skill(content.getvalue())


async def test_gateway_skills_use_deferred_connection_tools(runtime, actor, monkeypatch):
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    from moyai.connections import connection_tools
    from moyai.runtime import configure

    row = await connect(runtime, actor, monkeypatch)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Find a skill", request_key="tools", connections=[row["id"]],
    ))
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    configure(runtime)
    ctx = RunContext(deps=prepared.scope, model=runtime.models["test"], usage=RunUsage())
    capability = connection_tools(runtime.security)
    registered = capability.get_toolset()
    registered = await registered.for_run(ctx)
    async with registered:
        await registered.for_run_step(ctx)
        definitions = await registered.get_tools(ctx)
        assert definitions
        assert all(tool.tool_def.defer_loading for tool in definitions.values())
    direct = gateway_toolset(runtime.database, prepared.scope, prepared.scope.grants[0])
    async with direct:
        definitions = await direct.get_tools(ctx)
        assert {"skills_search", "skills_pull", "skills_upload"} == set(definitions)


async def gateway_turn(runtime: Runtime, actor: User, connection_id: str, *, prompt: str = "Inspect the hub.") -> PreparedTurn:
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt=prompt, request_key="gateway-regression", connections=[connection_id],
    ))
    await runtime.database.claim_next()
    return await runtime.admission.prepare(turn.id)


def stub_gateway_client(monkeypatch: pytest.MonkeyPatch, **methods: Any) -> None:
    def client(*args: Any, **kwargs: Any) -> GatewayClient:
        instance = gateway_client(*args, **kwargs)
        for name, method in methods.items():
            monkeypatch.setattr(instance, name, method)
        return instance

    monkeypatch.setattr("moyai.gateway.gateway_client", client)
    monkeypatch.setattr("moyai.gateway_tools.gateway_client", client)


@pytest.mark.parametrize(("case", "requests", "count"), [
    ("complete", 10, 1000), ("overflow", 10, 0), ("repeated-offset", 1, 0),
    ("duplicate-name", 2, 0), ("oversized-page", 1, 0),
])
async def test_discovery_bounds_pagination_and_never_releases_partial_catalogs(
    monkeypatch: pytest.MonkeyPatch, case: str, requests: int, count: int,
) -> None:
    client = GatewayClient(GatewayConfig(url="https://gateway.example.com", mcp=False), "synthetic-key", [])
    offsets: list[int] = []

    async def request(method: str, path: str, **kwargs: Any) -> httpx2.Response:
        assert (method, path) == ("GET", "/skill-hub/skills")
        params = kwargs["params"]
        assert params["limit"] == 100 and params["query"] == ""
        offset = params["offset"]
        offsets.append(offset)
        size = 101 if case == "oversized-page" else 100
        start = 0 if case == "duplicate-name" else offset
        next_offset = offset + 100
        if (case == "complete" and offset == 900) or (case == "duplicate-name" and offset):
            next_offset = None
        elif case == "repeated-offset":
            next_offset = offset
        return httpx2.Response(200, json={
            "skills": [{"name": f"skill-{start + index}", "description": None} for index in range(size)],
            "next_offset": next_offset,
        }, request=httpx2.Request(method, client.config.url + path))

    monkeypatch.setattr(client, "request", request)
    result = await client.discover()
    assert len(offsets) == requests
    assert result.skills.status == ("ready" if count else "error")
    assert result.skills.count == len(result.skill_catalog) == count
    assert result.mcp.status == "disabled"
    if count:
        assert result.skill_catalog[-1].name == "skill-999"
        assert all(skill.description == "" for skill in result.skill_catalog)


@pytest.mark.parametrize("when", ["before-download", "during-download"])
@pytest.mark.parametrize("change", ["credential", "disabled", "owner"])
async def test_prepared_gateway_rechecks_access_before_download_and_before_releasing_content(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch, when: str, change: str,
) -> None:
    row = await connect(runtime, actor, monkeypatch)
    other = await member(runtime)
    prompt = marker(f"gateway-{row['id']}:incident-review")
    prepared = await gateway_turn(runtime, actor, row["id"], prompt=prompt)
    downloads: list[str] = []

    async def revoke() -> None:
        values = {"revision": 2}
        if change == "credential":
            values["credential"] = runtime.security.seal("synthetic-rotated-key")
        elif change == "disabled":
            values["enabled"] = False
        else:
            values["owner_id"] = other.id
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(Connection).where(Connection.id == row["id"]).values(**values))

    async def download(name: str) -> tuple[GatewaySkill, dict[str, str]]:
        downloads.append(name)
        await revoke()
        return GatewaySkill(name=name), {"SKILL.md": "Content must not survive revoked access."}

    stub_gateway_client(monkeypatch, skill=download)
    if when == "before-download":
        await revoke()
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.skill_invocations.expand(prompt, prepared.scope)
    assert downloads == (["incident-review"] if when == "during-download" else [])
    # Replaying the persisted preparation must also reject the old grant.
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.admission.prepare(prepared.scope.turn_id)


async def test_gateway_prefix_preserves_canonical_picker_pages_and_metadata(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch, skill_product: SimpleNamespace,
) -> None:
    row = await connect(runtime, actor, monkeypatch)
    runtime.product_skill_catalog = skill_product.catalog
    actor.product_grant = skill_product.principal.grant
    original = skill_product.catalog.listing
    canonical = await original(actor.product_grant)
    shared = await original(actor.product_grant, shared=True)
    gateway = {
        "id": f"{row['id']}:incident-review", "name": "incident-review",
        "description": "Review an incident", "scope": row["name"],
        "reference": f"gateway-{row['id']}:incident-review", "icon": "sparkles",
    }
    expected = [gateway, *canonical["skills"]]
    calls: list[tuple[int, int, bool]] = []

    async def listing(grant: Any, *, shared: bool, offset: int, limit: int) -> dict[str, Any]:
        calls.append((offset, limit, shared))
        return await original(grant, shared=shared, offset=offset, limit=limit)

    monkeypatch.setattr(skill_product.catalog, "listing", listing)
    for offset, limit in [(0, 1), (0, 2), (1, 2), (2, 2), (3, 1)]:
        page = await runtime.skill_invocations.listing(actor, shared=False, offset=offset, limit=limit)
        assert page["skills"] == expected[offset:offset + limit]
        assert page["has_more"] is (offset + limit < len(expected))
        assert page["next_offset"] == (offset + limit if page["has_more"] else None)
        # Exercise the API's response model, including opaque IDs and all canonical metadata.
        wire = InvocableSkillPage.model_validate(page).model_dump()
        for key in ("can_publish", "capabilities", "source", "note"):
            assert wire[key] == canonical[key]
        for skill in wire["skills"]:
            if skill["source"] == "canonical":
                assert skill == next(item for item in canonical["skills"] if item["id"] == skill["id"])
    assert calls == [(0, 1, False), (0, 1, False), (0, 2, False), (1, 2, False), (2, 1, False)]
    page = await runtime.skill_invocations.listing(actor, shared=True, offset=0, limit=2)
    assert page["skills"] == shared["skills"]
    assert not page["has_more"] and page["next_offset"] is None


@pytest.mark.parametrize(("read_only", "require_approval", "allowed_tools"), [
    (True, True, None), (False, True, None), (False, False, None),
    (False, True, ["skills_search"]),
])
async def test_gateway_upload_uses_connection_filter_and_approval_before_dispatch(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch,
    read_only: bool, require_approval: bool, allowed_tools: list[str] | None,
) -> None:
    row = await connect(runtime, actor, monkeypatch, read_only=read_only,
                        require_approval=require_approval, allowed_tools=allowed_tools)
    prepared = await gateway_turn(runtime, actor, row["id"])
    configure(runtime)
    ctx = RunContext(deps=prepared.scope, model=runtime.models["test"], usage=RunUsage())
    calls: list[str] = []

    async def request(method: str, path: str, **kwargs: Any) -> httpx2.Response:
        calls.append(path)
        assert method == "POST" and path == "/skill-hub/skills/upload"
        files = unpack_skill(kwargs["files"]["file"][1])
        assert files["references/check.txt"] == "Synthetic evidence"
        assert "Use synthetic evidence." in files["SKILL.md"]
        return httpx2.Response(200, json={"status": "pending_review", "name": "created-note"})

    stub_gateway_client(monkeypatch, request=request)
    tools = await connection_tools(runtime.security).get_toolset().for_run(ctx)
    async with tools:
        await tools.for_run_step(ctx)
        definitions = await tools.get_tools(ctx)
        assert definitions and all(tool.tool_def.defer_loading for tool in definitions.values())
        prefix = f"litellm_{row['id'][:8]}_"
        assert prefix + "skills_search" in definitions
        upload = prefix + "skills_upload"
        if read_only or allowed_tools is not None:
            assert upload not in definitions
            assert calls == []
            return
        args = {
            "name": "created-note", "description": "Synthetic check", "instructions": "Use synthetic evidence.",
            "supporting_files": {"references/check.txt": "Synthetic evidence"},
        }
        if require_approval:
            assert definitions[upload].tool_def.kind == "unapproved"
            with pytest.raises(ApprovalRequired):
                await tools.call_tool(upload, args, ctx, definitions[upload])
            assert calls == []
        result = await tools.call_tool(upload, args, replace(ctx, tool_call_approved=True), definitions[upload])
        assert result == {"status": "pending_review", "name": "created-note"}
        assert calls == ["/skill-hub/skills/upload"]


async def test_delegated_gateway_selection_cannot_activate_skills_or_upload(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await connect(runtime, actor, monkeypatch, read_only=False)
    parent = await gateway_turn(runtime, actor, row["id"])
    assignment = marker(f"gateway-{row['id']}:incident-review") + " Agent-selected instructions."
    await DelegationService(runtime.database).start(parent.scope, "gateway-child", assignment)
    turn = await runtime.database.claim_next()
    configure(runtime)
    prepared = await ActivityEnvironment().run(prepare_turn, turn.id)
    assert prepared.scope.delegation_generated and prepared.scope.grants == parent.scope.grants
    assert assignment in prepared.prompt and "Selected skills (JSON)" not in prepared.prompt
    ctx = RunContext(deps=prepared.scope, model=runtime.models["test"], usage=RunUsage(), tool_call_approved=True)
    async with gateway_toolset(runtime.database, prepared.scope, prepared.scope.grants[0]) as tools:
        definition = (await tools.get_tools(ctx))["skills_upload"]
        with pytest.raises(ToolFailed, match="direct web conversation"):
            await tools.call_tool("skills_upload", {
                "name": "delegate-note", "description": "Synthetic", "instructions": "Synthetic",
            }, ctx, definition)


async def test_approval_resume_does_not_download_or_reinvoke_the_gateway_skill(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await connect(runtime, actor, monkeypatch)
    initial = await gateway_turn(runtime, actor, row["id"], prompt=marker(f"gateway-{row['id']}:incident-review"))
    downloads: list[str] = []

    async def download(name: str) -> tuple[GatewaySkill, dict[str, str]]:
        assert not downloads, "A deferred reply must not reactivate the original skill."
        downloads.append(name)
        return GatewaySkill(name=name), {"SKILL.md": "Ask for approval, then report its result."}

    stub_gateway_client(monkeypatch, skill=download)
    configure(runtime)
    environment = ActivityEnvironment()
    prepared = await environment.run(prepare_turn, initial.scope.turn_id)
    assert "Ask for approval" in prepared.prompt
    assert await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="", workspace=None, usage={}, deferred=True,
    )
    results = {"approvals": {"gateway-upload": True}}
    resumed = await runtime.database.resume(actor, prepared.scope.session_id, prepared.scope.turn_id, "approval", results)
    await runtime.database.claim_next()
    continuation = await environment.run(prepare_turn, resumed.id)
    assert continuation.prompt == "" and continuation.deferred_results == results
    assert continuation.conversation == '{"messages":[]}'
    assert await environment.run(prepare_turn, resumed.id) == continuation
    assert downloads == ["incident-review"]


async def test_queued_edit_admits_the_gateway_selected_by_the_new_skill(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await connect(runtime, actor, monkeypatch)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Inspect later.", request_key="edit-gateway-selection",
    ))
    async with client_for(runtime, actor) as client:
        response = await client.patch(f"/api/turns/{turn.id}", json={
            "prompt": marker(f"gateway-{row['id']}:incident-review") + " Inspect now.",
        })
        assert response.status_code == 200, response.text
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    assert [grant.id for grant in prepared.scope.grants] == [row["id"]]


async def test_native_scheduled_gateway_upload_requires_a_direct_human_turn(
    runtime: Runtime, actor: User, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await connect(runtime, actor, monkeypatch, read_only=False)
    schedule = await automation(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SettingAutomation).where(SettingAutomation.id == schedule.id).values(
            connections=json.dumps([row["id"]]),
        ))
    admitted = await admit_automation(runtime, schedule.id, "gateway-upload")
    assert admitted["status"] == "admitted"
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(admitted["turn_id"])
    calls: list[str] = []

    async def request(method: str, path: str, **kwargs: Any) -> httpx2.Response:
        calls.append(path)
        return httpx2.Response(200, json={"status": "pending_review"})

    stub_gateway_client(monkeypatch, request=request)
    ctx = RunContext(deps=prepared.scope, model=runtime.models["test"], usage=RunUsage(), tool_call_approved=True)
    async with gateway_toolset(runtime.database, prepared.scope, prepared.scope.grants[0]) as tools:
        definition = (await tools.get_tools(ctx))["skills_upload"]
        with pytest.raises(ToolFailed, match="direct web conversation"):
            await tools.call_tool("skills_upload", {
                "name": "scheduled-note", "description": "Synthetic", "instructions": "Synthetic",
            }, ctx, definition)
    assert calls == []
