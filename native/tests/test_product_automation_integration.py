"""Assembled native API/agent/worker with real PostgreSQL and Temporal.

The controlled model and identity-only product fixture supplement the parent's
deployed product/provider proof; they do not qualify a production cutover.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import test_product_automations as fixtures
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import select, text
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_product_deployment import configured
from test_product_deployment import environment as source_environment
from test_product_identity import google_cookie

from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Effect
from moyai.product_automation_tools import TOOLSET_ID
from moyai.product_automations import ProductAutomationService
from moyai.runtime import Runtime
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations
environment = source_environment


async def test_existing_runtime_constructs_one_canonical_automation_service(automations, environment):
    ctx = automations
    environment.update(
        MOYAI_DATABASE_URL=ctx.product.settings.database_url,
        MOYAI_NATIVE_SCHEMA=ctx.product.schema,
        MOYAI_PRODUCT_LOCAL_REHEARSAL="true", MOYAI_RUNTIME_ROLE="standalone",
        MOYAI_COORDINATOR_HANDOFF="false", MOYAI_SEPARATE_BROKER="false",
    )
    runtime = Runtime(Settings(_env_file=None), product_config=configured(environment))
    try:
        assert runtime.product is not None
        assert runtime.product_automations is not None
        assert runtime.product_automations.database is runtime.database
        assert runtime.product_automations.identity is runtime.product_identity
        assert runtime.product_automations.connections is runtime.product_connections
        assert runtime.database.product_connections is runtime.product_connections
        assert runtime.product_automations.events is not None
        assert runtime.product_automations.queue == runtime.settings.temporal_queue
        assert not runtime.settings.data_dir.exists()
    finally:
        await runtime.close()


@pytest.mark.parametrize(("visibility", "operation", "interceptor_refusal"), [
    ("shared", "automation_create", False), ("private", "automation_create", False),
    ("private", "automation_list", False), ("shared", "automation_create", True),
])
async def test_native_runtime_api_tools_and_worker_replay(
    automations, tmp_path, monkeypatch, visibility, operation, interceptor_refusal,
):
    ctx = automations
    settings = ctx.product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    # This fixture supplies only canonical identity/automation tables. Inject at
    # the same documented factory boundary; Runtime.start/agent/API/worker are real.
    runtime.product_identity = ctx.product.adapter
    runtime.product_settings = ctx.service.settings
    runtime.database.product_identity = ctx.product.adapter
    runtime.product_automations = ProductAutomationService.from_runtime(runtime)
    assert runtime.product_automations is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", workspace / "skills")
    calls = 0
    turn_id = ""

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "automation_create" in {tool.name for tool in info.function_tools}
            arguments = {
                "request": {
                    "revision": 0, "request_key": "assembled-create", "turn_id": turn_id,
                    "definition": fixtures.definition().model_dump(mode="json"),
                },
            } if operation == "automation_create" else {}
            return ModelResponse(parts=[ToolCallPart(operation, arguments, tool_call_id="assembled-create")])
        return ModelResponse(parts=[TextPart("The saved or refused result was observed.")])

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
            models={"test": FunctionModel(model, stream_function=stream)}, workspace=LocalWorkspace(workspace),
            skills_directory="skills",
        )
        if interceptor_refusal:
            original = runtime.database.authorize_scope

            async def reject_tool_dispatch(scope, **kwargs):
                await original(scope, **kwargs)
                if activity.in_activity():
                    name = activity.info().activity_type
                    if TOOLSET_ID in name and name.endswith("__call_tool"):
                        raise PermissionError("Synthetic pre-body authority refusal.")

            monkeypatch.setattr(runtime.database, "authorize_scope", reject_tool_dispatch)
        cookie, csrf = google_cookie(ctx.product.adapter, sub=ctx.product.subject)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
            base_url="http://127.0.0.1:8787", cookies={"workspace_session": cookie},
        ) as client:
            assert (await client.get("/api/product/automations")).json()["automations"] == []
            body = {
                "revision": 0, "request_key": "assembled-http-create",
                "definition": fixtures.definition().model_dump(mode="json"),
            }
            assert (await client.post("/api/product/automations", json=body)).status_code == 403
            saved = await client.post(
                "/api/product/automations", json=body,
                headers={"x-csrf-token": csrf, "origin": "http://127.0.0.1:8787"},
            )
            assert saved.status_code == 200, saved.text
            preview = await client.get(f"/api/product/automations/{saved.json()['id']}/preview")
            assert preview.status_code == 200 and preview.json()["legacy_paused"]
            listing = await client.get("/api/product/automations")
            assert len(listing.json()["automations"]) == 1

        session = await runtime.database.create_session(ctx.product.actor, NewSession(visibility=visibility))
        turn = await runtime.database.submit(
            ctx.product.actor, session.id, NewMessage(prompt="Create synthetic scheduled work.", request_key="assembled"),
        )
        await runtime.database.claim_next()
        turn_id = turn.id
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=converter,
        )
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                execution = await environment.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=settings.temporal_queue,
                )
                result = await asyncio.wait_for(execution.result(), 45)
                assert result["status"] == "completed"
                history = await execution.fetch_history()
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
        async with runtime.database.sessions() as db:
            receipt = await db.scalar(select(Effect).where(Effect.turn_id == turn.id))
            if operation == "automation_create":
                assert receipt is not None
                confirmed = visibility == "shared" and not interceptor_refusal
                assert receipt.state == ("confirmed" if confirmed else "not_dispatched")
            else:
                assert receipt is None
            assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == (
                2 if visibility == "shared" and not interceptor_refusal else 1
            )
        assert calls == 2  # Replay did not invoke the model or mutation again.
        assert TOOLSET_ID in json.dumps(history.to_json_dict())
    finally:
        await runtime.close()
