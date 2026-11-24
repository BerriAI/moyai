"""Boundary and Temporal checks; actual-app/model evidence is separate."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelRequest, ModelResponse, TextContent, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import delete
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_native_settings import automation, client_for, member

from moyai.agent import make_agent
from moyai.automations.product_automation_models import ProductAutomationTurn
from moyai.contracts import ExecutionContext
from moyai.models import Message, Session, SlackThread, Turn, User
from moyai.preferences.settings_models import SettingAutomationRun
from moyai.private_instructions import PrivateInstructions
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

TEXT = "Begin every answer with PRIVATE_ORCHID_47. Keep sentences short."


async def save(runtime, actor, text=TEXT, revision=0):
    async with client_for(runtime, actor) as client:
        response = await client.patch("/api/settings/general", json={
            "personal_instructions": text, "personal_instructions_revision": revision,
        })
        assert response.status_code == 200, response.text
        return response.json()


async def test_encrypted_owner_bound_preferences_clear_cas_and_partial_merge(runtime, actor):
    other = await member(runtime)
    async with runtime.database.sessions.begin() as db:
        row = await db.get(User, actor.id)
        row.preferences = json.dumps({"future": 1, "general": {"future": "keep"}})
    async with client_for(runtime, actor) as client:
        responses = await asyncio.gather(
            client.patch("/api/settings/general", json={"delivery_mode": "asap"}),
            client.patch("/api/settings/general", json={
                "personal_instructions": TEXT, "personal_instructions_revision": 0,
            }),
        )
        assert all(r.status_code == 200 for r in responses)
        saved = (await client.get("/api/settings/general")).json()
        assert saved["delivery_mode"] == "asap"
        assert saved["personal_instructions"] == TEXT
        assert saved["personal_instructions_revision"] == 1
        assert (await client.get("/api/me")).json()["preferences"] == saved
        async with runtime.database.sessions.begin() as db:
            raw = (await db.get(User, actor.id)).preferences
            assert TEXT not in raw and "PRIVATE_ORCHID" not in raw
            document = json.loads(raw)
            assert document["future"] == 1 and document["general"]["future"] == "keep"
            assert json.loads(runtime.security.unseal(document["private_instructions"]["sealed"])) == {
                "owner_id": actor.id, "text": TEXT,
            }
            (await db.get(User, other.id)).preferences = raw
        async with client_for(runtime, other) as other_client:
            assert (await other_client.get("/api/settings/general")).json()["personal_instructions"] == ""
        stale = await client.patch("/api/settings/general", json={
            "personal_instructions": "Stale overwrite", "personal_instructions_revision": 0,
            "delivery_mode": "when_idle",
        })
        assert stale.status_code == 409
        assert (await client.get("/api/settings/general")).json() == saved
        for body in (
            {"personal_instructions": "missing revision"},
            {"personal_instructions": "sensitive" * 501, "personal_instructions_revision": 1},
            {"personal_instructions": None, "personal_instructions_revision": 1},
            {"personal_instructions_revision": 1},
        ):
            response = await client.patch("/api/settings/general", json=body)
            assert response.status_code == 422
            assert "sensitive" not in response.text
        cleared = await save(runtime, actor, "", 1)
        assert cleared["personal_instructions"] == "" and cleared["personal_instructions_revision"] == 2
        assert cleared["delivery_mode"] == "asap"
        async with runtime.database.sessions() as db:
            assert json.loads((await db.get(User, actor.id)).preferences)["private_instructions"]["sealed"] == ""


async def test_loading_rechecks_owner_scope_and_clear_without_cache(runtime, actor, active):
    session, _, prepared = active
    await save(runtime, actor)
    capability = PrivateInstructions(runtime.database)
    assert await capability.load(prepared.scope) == TEXT
    await save(runtime, actor, "", 1)
    assert await capability.load(prepared.scope) == ""
    await save(runtime, actor, TEXT, 2)
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Session, session.id)
        row.visibility = "shared"
    assert await capability.load(prepared.scope) == ""
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Session, session.id)
        row.visibility, row.surface = "private", "slack"
    assert await capability.load(prepared.scope) == ""


@pytest.mark.parametrize("change", ["shared", "clear", "disabled", "stopped"])
async def test_loading_rechecks_after_awaited_provenance(runtime, actor, active, monkeypatch, change):
    session, turn, prepared = active
    await save(runtime, actor)
    original = runtime.database.human_origin

    async def changed(db, scope):
        origin = await original(db, scope)
        async with runtime.database.sessions.begin() as writer:
            if change == "shared":
                (await writer.get(Session, session.id)).visibility = "shared"
            elif change == "clear":
                row = await writer.get(User, actor.id)
                document = json.loads(row.preferences)
                document["private_instructions"] = {"revision": 2, "sealed": ""}
                row.preferences = json.dumps(document)
            elif change == "disabled":
                (await writer.get(User, actor.id)).enabled = False
            else:
                (await writer.get(Turn, turn.id)).status = "stopped"
        return origin

    monkeypatch.setattr(runtime.database, "human_origin", changed)
    capability = PrivateInstructions(runtime.database)
    if change in {"disabled", "stopped"}:
        with pytest.raises(PermissionError):
            await capability.load(prepared.scope)
    else:
        assert await capability.load(prepared.scope) == ""


@pytest.mark.parametrize("patch", [
    {"visibility": "shared"}, {"surface": "slack"}, {"surface": "automation"},
    {"delegation_depth": 1}, {"delegation_generated": True}, {"swarm_id": "team"},
    {"goal_generation": 1}, {"execution_context": ExecutionContext()},
])
async def test_non_direct_scope_never_loads(runtime, actor, active, patch):
    await save(runtime, actor)
    assert await PrivateInstructions(runtime.database).load(replace(active[2].scope, **patch)) == ""


@pytest.mark.parametrize("provenance", ["product_schedule", "interval_prelink", "slack", "forged_resume"])
async def test_persisted_provenance_excludes_private_looking_runs(runtime, actor, active, provenance):
    session, turn, prepared = active
    await save(runtime, actor)
    schedule = await automation(runtime, actor) if provenance == "interval_prelink" else None
    async with runtime.database.sessions.begin() as db:
        if provenance == "product_schedule":
            db.add(ProductAutomationTurn(turn_id=turn.id, origin_turn_id=turn.id, visibility="private"))
        elif provenance == "interval_prelink":
            db.add(SettingAutomationRun(
                id="instruction-schedule", automation_id=schedule.id, request=runtime.security.seal({}),
            ))
            (await db.get(Turn, turn.id)).request_key = "automation:instruction-schedule"
        elif provenance == "slack":
            db.add(SlackThread(team_id="synthetic", channel_id="synthetic", thread_ts="1", session_id=session.id))
        else:
            await db.execute(delete(Message).where(Message.turn_id == turn.id))
            row = await db.get(Turn, turn.id)
            row.request_key = "resume:pretend"
            row.deferred_results = runtime.security.seal({"calls": {}})
            row.request_fingerprint = "f" * 64
    assert await PrivateInstructions(runtime.database).load(prepared.scope) == ""


async def test_request_only_user_role_never_mutates_history_or_authority(runtime, actor, active):
    await save(runtime, actor)
    scope = active[2].scope
    model = TestModel()
    history = [ModelRequest(parts=[UserPromptPart("Current question")])]
    ctx = RunContext(deps=scope, model=model, usage=RunUsage(), run_id=scope.root_run_id, messages=history)
    request = ModelRequestContext(model=model, messages=list(history), model_settings=None,
                                  model_request_parameters=ModelRequestParameters())
    capability = PrivateInstructions(runtime.database)
    changed = await capability.before_model_request(ctx, request)
    assert TEXT in changed.messages[0].parts[0].content[0].content
    assert isinstance(changed.messages[0].parts[0], UserPromptPart)
    assert changed.messages[0].parts[0].content[0].metadata == "moyai.private-context:preferences"
    assert changed.messages[1:] == history == request.messages
    assert len(ctx.messages) == 1
    assert changed.model_request_parameters is request.model_request_parameters
    ctx.run_id = "sdk-delegate"
    assert await capability.before_model_request(ctx, request) is request


async def test_saved_instructions_run_in_temporal_and_replay(runtime, actor, active):
    """Controlled-model integration; the delivered live recording uses a real model."""
    await save(runtime, actor)
    _, turn, _ = active

    def respond(messages, info):
        preferences = []
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        items = [part.content] if isinstance(part.content, str) else part.content
                        preferences.extend(
                            item.content if isinstance(item, TextContent) else item
                            for item in items if isinstance(item, str | TextContent)
                        )
        assert any(TEXT in content for content in preferences)
        assert TEXT not in str(info.instructions)
        return ModelResponse(parts=[TextPart("Private instructions reached the model as user preferences.")])

    async def stream(messages, info):
        yield respond(messages, info).parts[0].content

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"), skills_directory="skills",
    )
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            execution = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(execution.result(), timeout=45))["status"] == "completed"
            history = await execution.fetch_history()
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
