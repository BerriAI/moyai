"""Real PostgreSQL credential approval/resume and canonical receiver receipts."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
import test_product_automation_events as event_fixtures
import test_product_automations as fixtures
import test_product_credential_execution as credential_fixtures
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import delete, select, text, update

from moyai.models import Effect, Message, Turn
from moyai.product_automation_models import ProductAutomationOperation as Operation
from moyai.product_automation_tools import GrantRequest, WebhookRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations
events = event_fixtures.events
credentials = credential_fixtures.credentials


async def resumed_context(ctx, credentials):
    caller = await fixtures.tool_context(ctx)
    original = caller.deps.turn_id
    credentials.scope = caller.deps
    pending = await credential_fixtures.wait_for_credential(credentials)
    supplied = credential_fixtures.provide().model_copy(update={"scope": "organization"})
    result = await credentials.service.resolve(None, ctx.product.principal, pending["request_id"], supplied)
    assert (await ctx.product.database.claim_next()).id == result["turn_id"]
    caller.deps = (await ctx.product.database.prepare(result["turn_id"])).scope
    caller.run_id, caller.tool_call_id = caller.deps.turn_id, "configure-receiver"
    return caller, original, pending["request_id"]


async def test_real_credential_resume_configures_receiver_without_secret_in_tool_evidence(events, credentials):
    ctx = events
    ctx.service.credentials = credentials.service
    row = await fixtures.create(ctx, triggers=[{"id": "event", "event": {"provider": "webhook", "event": "*"}}])
    caller, original, request_id = await resumed_context(ctx, credentials)
    async with ctx.product.database.sessions() as db:
        assert await ctx.service.human_origin(db, caller.deps) == original
    await ctx.service.authorize_tool(caller.deps, caller.run_id)
    body = WebhookRequest(
        automation_id=row["id"], revision=1, request_key="configure-approved-receiver",
        turn_id=caller.deps.turn_id, provider="webhook", credential_request_id=request_id,
    )
    tools, selected, receipt = await fixtures.reserved_call(ctx, caller, "automation_webhook_setup", body)
    result = await tools.call_tool("automation_webhook_setup", {"request": body}, caller, selected)
    assert result["configured"] and result["paused"] and result["revision"] == 2
    assert "secret" not in result and not result["registered_at_provider"]
    async with ctx.product.database.sessions() as db:
        effect = await db.get(Effect, receipt)
        operation = await db.scalar(select(Operation).where(Operation.request_key == body.request_key))
        evidence = ctx.product.database.security.unseal(effect.resolution).decode()
        assert effect.state == "confirmed"
        for value in (json.dumps(result), evidence, operation.result, operation.fingerprint):
            assert "synthetic-signing-material" not in value
    async with ctx.product.source.connect() as db:
        encrypted = await db.scalar(text("SELECT encrypted FROM moyai.automation_webhooks"))
        assert ctx.product.adapter.legacy_security.decrypt(encrypted) == "synthetic-signing-material-123456"
        assert "synthetic-signing-material" not in encrypted

    # A previously committed identical operation can recover its receipt without
    # rematerializing a credential which may have been revoked in the meantime.
    async def do_not_resolve(*args, **kwargs):
        pytest.fail("An identical confirmed operation rematerialized a secret.")
    ctx.service.credentials.webhook_secret = do_not_resolve
    caller.tool_call_id = "recover-receiver-result"
    tools, selected, receipt = await fixtures.reserved_call(ctx, caller, "automation_webhook_setup", body)
    assert await tools.call_tool("automation_webhook_setup", {"request": body}, caller, selected) == result
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, receipt)).state == "confirmed"


@pytest.mark.parametrize("invalid", ["unknown_handle", "revision", "delegate"])
async def test_receiver_tool_refusal_records_no_dispatch(events, credentials, invalid):
    ctx = events
    ctx.service.credentials = credentials.service
    row = await fixtures.create(ctx, triggers=[{"id": "event", "event": {"provider": "webhook", "event": "*"}}])
    caller = await fixtures.tool_context(ctx, delegate=invalid == "delegate")
    body = WebhookRequest(
        automation_id=row["id"], revision=2 if invalid == "revision" else 1,
        request_key="refused-receiver", turn_id=caller.deps.turn_id,
        provider="webhook", credential_request_id="0" * 32,
    )
    tools, selected, receipt = await fixtures.reserved_call(ctx, caller, "automation_webhook_setup", body)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool("automation_webhook_setup", {"request": body}, caller, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, receipt)).state == "not_dispatched"
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automation_webhooks")) == 0


async def test_owner_turn_grant_survives_verified_resume_but_not_forged_prefix(events, credentials):
    ctx = events
    row = await fixtures.create(ctx)
    caller = await fixtures.tool_context(ctx)
    body = GrantRequest(
        automation_id=row["id"], revision=1, request_key="human-turn-grant",
        turn_id=caller.deps.turn_id, permissions=["instructions"],
    )
    tools, selected, _ = await fixtures.reserved_call(ctx, caller, "automation_grant", body)
    await tools.call_tool("automation_grant", {"request": body}, caller, selected)
    original = caller.deps.turn_id
    credentials.scope = caller.deps
    pending = await credential_fixtures.wait_for_credential(credentials)
    result = await credentials.service.resolve(
        None, ctx.product.principal, pending["request_id"],
        credential_fixtures.provide().model_copy(update={"scope": "organization"}),
    )
    await ctx.product.database.claim_next()
    scope = (await ctx.product.database.prepare(result["turn_id"])).scope
    assert (await ctx.service.access.read(scope))["automations"][0]["id"] == row["id"]
    async with ctx.product.database.sessions() as db:
        assert await ctx.service.human_origin(db, scope) == original
    # Neither a guessed native request-key prefix nor an arbitrary copied
    # deferred payload is server evidence of a direct human turn.
    forged = await fixtures.tool_context(ctx)
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(delete(Message).where(Message.turn_id == forged.deps.turn_id))
        await db.execute(update(Turn).where(Turn.id == forged.deps.turn_id).values(
            request_key="resume:" + original,
            deferred_results=ctx.product.database.security.seal(b'{"calls":{}}'),
            request_fingerprint="f" * 64,
        ))
    with pytest.raises(HTTPException):
        await ctx.service.authorize_tool(forged.deps, forged.run_id)
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.service.authorize_tool(replace(scope, actor_id="someone-else"), scope.turn_id)


async def test_secure_request_resume_and_receiver_mutation_use_real_temporal_activities(
    events, credentials, tmp_path, monkeypatch,
):
    """Exercise the exact optional-capability seam before parent app assembly."""
    import asyncio
    import shutil
    from pathlib import Path

    from pydantic_ai import ConversationTypeAdapter
    from pydantic_ai.capabilities import LocalWorkspace
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer

    import moyai.runtime as runtime_module
    from moyai.contracts import NewMessage, NewSession
    from moyai.product_credential_tools import capability as credential_capability
    from moyai.runtime import Runtime
    from moyai.storage import encrypted_converter
    from moyai.stores import NativeStores
    from moyai.worker import make_worker
    from moyai.workflow import TurnWorkflow

    ctx = events
    ctx.service.credentials = credentials.service
    row = await fixtures.create(ctx, triggers=[{"id": "event", "event": {"provider": "webhook", "event": "*"}}])
    settings = ctx.product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    runtime.product_identity = runtime.database.product_identity = ctx.product.adapter
    runtime.product_automations = ctx.service
    original = runtime_module.make_agent

    def compose(*args, **kwargs):
        kwargs["product_capabilities"].append(credential_capability(credentials.service))
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime_module, "make_agent", compose)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", workspace / "skills")
    calls, request_id, resumed_turn = 0, "", ""

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool = next(tool for tool in info.function_tools if tool.name == "credentials_request")
            assert "provider" in tool.parameters_json_schema["properties"]
            return ModelResponse(parts=[ToolCallPart(
                "credentials_request", credential_fixtures.request_body().model_dump(mode="json"),
                tool_call_id="temporal-credential",
            )])
        if calls == 2:
            return ModelResponse(parts=[ToolCallPart("automation_webhook_setup", {
                "automation_id": row["id"], "revision": 1, "request_key": "temporal-webhook-setup",
                "turn_id": resumed_turn, "provider": "webhook", "credential_request_id": request_id,
            }, tool_call_id="temporal-receiver")])
        return ModelResponse(parts=[TextPart("The canonical receiver is configured and paused.")])

    async def stream(messages, info):
        for part in model(messages, info).parts:
            if isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}
            elif isinstance(part, TextPart):
                yield part.content

    try:
        await runtime.start(
            models={"test": FunctionModel(model, stream_function=stream)},
            workspace=LocalWorkspace(workspace), skills_directory="skills",
        )
        session = await runtime.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
        turn = await runtime.database.submit(ctx.product.actor, session.id, NewMessage(
            prompt="Configure the authorized receiver through the secure form.", request_key="credential-temporal",
        ))
        assert (await runtime.database.claim_next()).id == turn.id
        converter = encrypted_converter(runtime.security, runtime.objects)
        async with await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ) as environment:
            runtime.client = environment.client
            ctx.service.connect = runtime.connect
            histories = []
            async with make_worker(runtime):
                first = await environment.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=settings.temporal_queue,
                )
                assert (await asyncio.wait_for(first.result(), 45))["status"] == "waiting"
                histories.append(await first.fetch_history())
                async with runtime.database.sessions() as db:
                    waiting = await db.get(Turn, turn.id)
                    conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(waiting.conversation))
                    request_id = conversation.deferred_tool_requests.metadata["temporal-credential"]["request_id"]
                result = await credentials.service.resolve(None, ctx.product.principal, request_id,
                    credential_fixtures.provide().model_copy(update={"scope": "organization"}))
                resumed_turn = result["turn_id"]
                current = await runtime.database.claim_next()
                assert current.id == resumed_turn
                second = await environment.client.start_workflow(
                    TurnWorkflow.run, current.id, id=current.workflow_id, task_queue=settings.temporal_queue,
                )
                assert (await asyncio.wait_for(second.result(), 45))["status"] == "completed"
                histories.append(await second.fetch_history())
            for history in histories:
                await Replayer(
                    workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                ).replay_workflow(history)
        assert calls == 3
        assert "moyai_product_credentials_v1" in json.dumps(histories[0].to_json_dict())
        async with runtime.database.sessions() as db:
            effect = await db.scalar(select(Effect).where(
                Effect.turn_id == resumed_turn, Effect.tool_name == "automation_webhook_setup",
            ))
            assert effect is not None and effect.state == "confirmed"
            completed = await db.get(Turn, resumed_turn)
            assert "synthetic-signing-material" not in runtime.security.unseal(completed.conversation).decode()
        async with ctx.product.source.connect() as db:
            assert await db.scalar(text("SELECT revision FROM moyai.automations WHERE id=:id"), {"id": row["id"]}) == 2
    finally:
        await runtime.close()
