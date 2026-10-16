"""Connector construction and credential access belong to activities, including on replay."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall
from pydantic_ai.toolsets import FunctionToolset
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.connections import SERVICES
from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection, Turn, new_id
from moyai.product_connections import ProductGitHubToolset
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize(("product", "read_only"), [
    pytest.param(False, True, id="encrypted-account"),
    pytest.param(True, True, id="canonical-github-read"),
    pytest.param(True, False, id="canonical-github-publication"),
])
async def test_connections_resolve_in_activities_and_replay_without_provider_access(
    runtime, actor, monkeypatch, product, read_only,
):
    constructions: list[str] = []
    calls: list[str] = []
    replaying = False

    def require_activity():
        assert not replaying, "Replay must consume recorded tool schemas and results."
        assert not workflow.in_workflow(), "Provider construction cannot run in the workflow sandbox."
        assert activity.in_activity(), "Resolve connectors in the SDK's durable activities."
        constructions.append(activity.info().activity_type)

    def provider_tools():
        tools = FunctionToolset(id="provider")

        @tools.tool_plain(metadata={"annotations": {"readOnlyHint": True}})
        async def read_repository(owner: str, repo: str) -> str:
            """Read the selected repository."""
            assert activity.in_activity() and not workflow.in_workflow()
            calls.append(f"{owner}/{repo}")
            return "Repository read completed."

        return tools

    if product:
        async def check(scope, grant):
            await runtime.database.authorize_scope(scope)

        service = SimpleNamespace(
            database=runtime.database,
            connectors=SimpleNamespace(github=SimpleNamespace(target=lambda name: 7)),
            check=check,
        )

        def resolve(scope, grant):
            require_activity()
            # Exercise the real canonical publication adapter's lazy import and schemas.
            return ProductGitHubToolset(service, scope, grant)

        async def native(self, *, write=False):
            require_activity()
            assert not write
            return provider_tools()

        monkeypatch.setattr(ProductGitHubToolset, "_native", native)
        runtime.product_connections = SimpleNamespace(toolset=resolve)
    else:
        class Provider:
            def __init__(self, **kwargs):
                require_activity()
                assert kwargs["auth"] == "synthetic-account-token"

            def get_toolset(self):
                return provider_tools()

        monkeypatch.setitem(SERVICES, "github", Provider)
        unseal = runtime.security.unseal

        def decrypt(value):
            if value == credential:
                require_activity()
            return unseal(value)

        monkeypatch.setattr(runtime.security, "unseal", decrypt)

    credential = runtime.security.seal("synthetic-account-token")
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Selected repository",
            credential=credential, read_only=read_only,
            policy=json.dumps({"product": {"repository_id": 7}} if product else {}),
        )
        db.add(connection)

    tool_name = f"github_{connection.id[:8]}_read_repository"

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert tool_name in names
        if product:
            prefix = f"github_{connection.id[:8]}_"
            assert prefix + "publication_status" in names
            for name in ("create_pull_request", "update_pull_request", "request_pull_request_write_access"):
                assert (prefix + name in names) is not read_only
        if any(
            isinstance(part, ToolReturnPart) and part.tool_name == tool_name
            for message in messages for part in message.parts
        ):
            yield "Repository read completed."
        else:
            yield {0: DeltaToolCall(
                name=tool_name, json_args=json.dumps({"owner": "test", "repo": "selected"}),
                tool_call_id="selected-repository-read",
            )}

    runtime.models["test"].stream_function = stream
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read the selected repository.", request_key="connection-read",
        connections=[connection.id],
    ))
    await runtime.database.claim_next()
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            result = await asyncio.wait_for(handle.result(), timeout=60)
            history = await handle.fetch_history()
        assert result["status"] == "completed"
        assert calls == ["test/selected"]
        assert any(name.endswith("__get_tools") for name in constructions)
        assert any(name.endswith("__call_tool") for name in constructions)
        replaying = True
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)

    assert calls == ["test/selected"]
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "completed"
        assert runtime.security.unseal(saved.result).decode() == "Repository read completed."
