"""Authority and disclosure checks; real-app/model proof is documented separately."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from test_product_connections import bridge as bridge  # noqa: PLC0414
from test_product_connections import github_accounts, gitlab_accounts, save

from moyai.connection_guidance import ConnectionGuide, connection_guidance
from moyai.contracts import ExecutionContext
from moyai.delegation_models import DelegatedSession
from moyai.goal_models import Goal, GoalTurn
from moyai.models import Connection, Message, Session, SlackThread, Turn, User, new_id
from moyai.swarm_models import SwarmMember, SwarmMission, SwarmTurn


async def connection(runtime, actor, **values):
    row = Connection(
        id=new_id(), owner_id=actor.id, kind="mcp", name="Personal research",
        credential=runtime.database.security.seal("private-token"), read_only=True, shared=False,
        policy='{"mcp":{"url":"https://secret-endpoint.example.test/mcp"}}',
    )
    for key, value in values.items():
        setattr(row, key, value)
    async with runtime.database.sessions.begin() as db:
        db.add(row)
    return row


async def test_unselected_catalog_is_safe_and_does_not_select(runtime, actor, active):
    row = await connection(runtime, actor)
    scope = active[2].scope
    result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    assert result.status == "unselected"
    assert result.connections[0].model_dump() == {
        "label": row.name, "provider": "mcp", "read_only": True, "selected": False,
    }
    serialized = result.model_dump_json()
    for private in (row.id, actor.id, "private-token", "secret-endpoint", "credential", "policy", "tools"):
        assert private not in serialized
    assert "Choose connections" in result.guidance
    assert result.settings_link == "#settings/connections"
    assert result.selection_link == "#settings/general"
    assert scope.grants == []
    async with runtime.database.sessions() as db:
        assert (await db.get(Connection, row.id)).revision == row.revision
        assert (await db.get(Turn, scope.turn_id)).connections == "[]"


async def test_empty_disabled_and_other_user_connections(runtime, actor, active):
    await connection(runtime, actor, enabled=False)
    async with runtime.database.sessions.begin() as db:
        other = User(id=new_id(), username="other", display_name="Other", password="unused")
        db.add(other)
    await connection(runtime, other, name="Other person's account")
    scope = active[2].scope
    result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    assert result.status == "empty" and result.connections == []
    assert "revoked" in result.guidance and "Settings" in result.guidance


async def test_selected_status_and_current_revocation(runtime, actor, active):
    row = await connection(runtime, actor)
    scope = active[2].scope
    async with runtime.database.sessions() as db:
        scope.grants = await runtime.database.native_grants(db, actor.id, [row.id])
    result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    assert result.status == "available" and result.connections[0].selected
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, row.id)).enabled = False
    with pytest.raises((HTTPException, PermissionError)):
        await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)


@pytest.mark.parametrize("change", [
    {"delegation_generated": True}, {"delegation_depth": 1}, {"swarm_id": "child"},
    {"surface": "slack"}, {"visibility": "shared"},
    {"execution_context": ExecutionContext("shared", "slack")},
    {"execution_context": ExecutionContext("private", "automation")},
    {"goal_generation": 1},
])
async def test_non_direct_scopes_cannot_list_accounts(runtime, actor, active, change):
    await connection(runtime, actor)
    scope = replace(active[2].scope, **change)
    try:
        result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    except PermissionError:
        return  # Persisted execution-context mismatches fail closed before release.
    assert result.status == "private_chat_required" and result.connections == []


@pytest.mark.parametrize("mutation", ["shared", "slack", "owner", "deleted", "stopped", "disabled", "human"])
async def test_persisted_scope_rechecked_after_read(runtime, actor, active, monkeypatch, mutation):
    await connection(runtime, actor)
    scope = active[2].scope
    guide = ConnectionGuide(runtime.database)
    original = guide.private_caller
    calls = 0

    async def changing(scope, run_id, candidates=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with runtime.database.sessions.begin() as db:
                session = await db.get(Session, scope.session_id)
                if mutation in {"shared", "slack"}:
                    setattr(session, "visibility" if mutation == "shared" else "surface", mutation)
                elif mutation == "owner":
                    other = User(id=new_id(), username="other", display_name="Other", password="unused")
                    db.add(other)
                    await db.flush()
                    session.owner_id = other.id
                elif mutation == "deleted":
                    session.deleted = True
                elif mutation == "stopped":
                    (await db.get(Turn, scope.turn_id)).status = "cancelled"
                elif mutation == "disabled":
                    (await db.get(User, scope.actor_id)).enabled = False
                else:
                    message = await db.scalar(select(Message).where(Message.identity == f"user:{scope.turn_id}"))
                    await db.delete(message)
        return await original(scope, run_id, candidates)

    monkeypatch.setattr(guide, "private_caller", changing)
    try:
        result = await guide.read(scope, scope.root_run_id)
    except (PermissionError, HTTPException):
        pass
    else:
        assert result.status == "private_chat_required" and result.connections == []
    assert calls == 2


async def test_sdk_registration_read_only_and_delegate_restriction(runtime, actor, active):
    await connection(runtime, actor)
    scope = active[2].scope
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), run_id="delegate")
    tools = connection_guidance(runtime.database).get_toolset()
    definitions = await tools.get_tools(ctx)
    tool = definitions["connection_list"]
    assert tool.tool_def.metadata["annotations"]["readOnlyHint"] is True
    result = await tools.call_tool("connection_list", {}, ctx, tool)
    assert result.status == "private_chat_required" and result.connections == []


async def test_saved_catalog_never_refreshes_legacy_github(bridge, monkeypatch):
    # A legacy GitHub row normally invokes ensure_connection from accounts().
    save(bridge, "github", repositories=["legacy/repo"])
    save(bridge, "linear")
    migration = AsyncMock(side_effect=AssertionError("Unexpected provider request"))
    monkeypatch.setattr(bridge.connectors.github, "ensure_connection", migration)
    rows = await bridge.catalog("google:owner", refresh_metadata=False)
    assert any(row["kind"] == "linear" for row in rows)
    assert not any(row["kind"] == "github" for row in rows)
    migration.assert_not_called()


@pytest.mark.parametrize("mutation", ["disabled", "deleted", "revision", "ownership"])
async def test_unselected_account_rechecked_before_release(runtime, actor, active, monkeypatch, mutation):
    row = await connection(runtime, actor)
    scope = active[2].scope
    guide = ConnectionGuide(runtime.database)
    original = guide.private_caller

    async def changing(scope, run_id, candidates=None):
        if candidates is not None:
            async with runtime.database.sessions.begin() as db:
                saved = await db.get(Connection, row.id)
                if mutation == "deleted":
                    await db.delete(saved)
                elif mutation == "disabled":
                    saved.enabled = False
                elif mutation == "revision":
                    saved.revision += 1
                else:
                    other = User(id=new_id(), username="other", display_name="Other", password="unused")
                    db.add(other)
                    await db.flush()
                    saved.owner_id = other.id
        return await original(scope, run_id, candidates)

    monkeypatch.setattr(guide, "private_caller", changing)
    with pytest.raises(ToolFailed, match="accounts changed"):
        await guide.read(scope, scope.root_run_id)


@pytest.mark.parametrize("provenance", ["delegate", "member", "swarm", "goal", "slack"])
async def test_persisted_provenance_without_scope_flags(runtime, actor, active, provenance):
    await connection(runtime, actor)
    scope = active[2].scope
    async with runtime.database.sessions.begin() as db:
        if provenance == "delegate":
            db.add(DelegatedSession(
                session_id=scope.session_id, parent_session_id=scope.session_id, parent_turn_id=scope.turn_id,
                initial_turn_id=scope.turn_id, actor_id=actor.id, tool_call_id="test",
                fingerprint="test", grants=runtime.security.seal({
                    "context": {"visibility": "private", "surface": "web"}, "grants": [],
                }), depth=1, name="Child", avatar=0,
            ))
        elif provenance in {"member", "swarm"}:
            db.add(SwarmMission(
                id=scope.session_id, owner_id=actor.id, auth_version=actor.auth_version,
                request_key="mission", fingerprint="test", source_turn_id=scope.turn_id,
                goal="Synthetic", connections="[]", grants_key=scope.grants_key,
            ))
            await db.flush()
            if provenance == "member":
                db.add(SwarmMember(
                    session_id=scope.session_id, mission_id=scope.session_id,
                    name="Member", position=0, model="test",
                ))
            else:
                db.add(SwarmTurn(turn_id=scope.turn_id, mission_id=scope.session_id, generation=1, generated=True))
        elif provenance == "goal":
            db.add(Goal(
                session_id=scope.session_id, owner_id=actor.id, auth_version=actor.auth_version,
                source_turn_id=scope.turn_id, objective="Synthetic", model="test", grants_key=scope.grants_key,
            ))
            await db.flush()
            db.add(GoalTurn(turn_id=scope.turn_id, session_id=scope.session_id, generation=1, generated=True))
        else:
            db.add(SlackThread(team_id="T", channel_id="C", thread_ts="1", session_id=scope.session_id))
    try:
        result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    except (PermissionError, HTTPException):
        return
    assert result.status == "private_chat_required" and result.connections == []


async def test_catalog_bounds_grant_generation(bridge):
    github_accounts(bridge, count=100)
    # Bound the authority generation, not merely the serialized output.
    assert len(bridge._grants("google:owner", shared_session=False, surface="web", limit=7)) == 7
    assert len(await bridge.catalog("google:owner", refresh_metadata=False, limit=7)) == 7


async def test_saved_catalog_preserves_existing_provider_labels_and_eligibility(bridge):
    github_accounts(bridge, count=2)
    gitlab_accounts(bridge)
    for provider in ("linear", "notion", "slack"):
        save(bridge, provider)
    assert await bridge.catalog("google:owner", refresh_metadata=False) == await bridge.catalog("google:owner")


async def test_visibility_changes_during_final_candidate_resolution(runtime, actor, active, monkeypatch):
    row = await connection(runtime, actor)
    scope = active[2].scope
    original = runtime.database.resolve_grants
    changed = False

    async def resolving(db, actor_id, ids, **kwargs):
        nonlocal changed
        current = await original(db, actor_id, ids, **kwargs)
        if ids == [row.id]:
            # SQLite deliberately does not enforce PostgreSQL's shared row
            # lock: exercise the final reload itself, independently of locks.
            async with runtime.database.sessions.begin() as writer:
                (await writer.get(Session, scope.session_id)).visibility = "shared"
            changed = True
        return current

    monkeypatch.setattr(runtime.database, "resolve_grants", resolving)
    result = await ConnectionGuide(runtime.database).read(scope, scope.root_run_id)
    assert changed
    assert result.status == "private_chat_required" and result.connections == []
