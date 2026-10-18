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
    discovery_activities: set[str] = set()
    dispatched_connections: list[str] = []
    calls: list[str] = []
    replaying = False

    def require_activity():
        assert not replaying, "Replay must consume recorded tool schemas and results."
        assert not workflow.in_workflow(), "Provider construction cannot run in the workflow sandbox."
        assert activity.in_activity(), "Resolve connectors in the SDK's durable activities."
        constructions.append(activity.info().activity_type)
        if activity.info().activity_type.endswith("__get_tools"):
            discovery_activities.add(activity.info().activity_id)

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
            connectors=SimpleNamespace(github=SimpleNamespace(
                target=lambda name: 7, repository_name=lambda identity: "test/selected",
            )),
            check=check,
        )

        def resolve(scope, grant):
            require_activity()
            if activity.info().activity_type.endswith("__call_tool"):
                dispatched_connections.append(grant.id)
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
                if activity.info().activity_type.endswith("__call_tool"):
                    dispatched_connections.append(kwargs["id"].removeprefix("connection_"))

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
        unrelated = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Another selected repository",
            credential=credential, read_only=read_only, policy=connection.policy,
        )
        db.add(unrelated)

    tool_name = f"github_{connection.id[:8]}_read_repository"
    prefix = f"github_{connection.id[:8]}_"
    publication_writes = ("create_pull_request", "update_pull_request", "request_pull_request_write_access")
    required_tools = {
        tool_name, *(
            [prefix + "publication_status", *(prefix + name for name in publication_writes if not read_only)]
            if product else []
        ),
    }

    async def stream(messages, info):
        names = {tool.name for tool in info.function_tools}
        if not required_tools <= names:
            assert "search_tools" in names
            target = min(required_tools - names)
            search_id = "discover-" + target
            assert not any(
                isinstance(part, ToolReturnPart) and part.tool_call_id == search_id
                for message in messages for part in message.parts
            ), f"Targeted discovery did not reveal {target}."
            yield {0: DeltaToolCall(
                name="search_tools", json_args=json.dumps({"queries": [target]}),
                tool_call_id=search_id,
            )}
            return
        if product:
            assert "test/selected" in next(
                tool.description for tool in info.function_tools if tool.name == tool_name
            )
            assert prefix + "publication_status" in names
            for name in publication_writes:
                assert (prefix + name in names) is not read_only
        reads = sum(
            isinstance(part, ToolReturnPart) and part.tool_name == tool_name
            for message in messages for part in message.parts
        )
        if reads == 2:
            yield "Repository read completed."
        else:
            yield {0: DeltaToolCall(
                name=tool_name, json_args=json.dumps({"owner": "test", "repo": "selected"}),
                tool_call_id=f"selected-repository-read-{reads}",
            )}

    runtime.models["test"].stream_function = stream
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read the selected repository.", request_key="connection-read",
        connections=[connection.id, unrelated.id],
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
        assert calls == ["test/selected"] * 2
        assert any(name.endswith("__get_tools") for name in constructions)
        assert any(name.endswith("__call_tool") for name in constructions)
        assert len(discovery_activities) == 1
        assert dispatched_connections == [connection.id] * 2
        replaying = True
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)

    assert calls == ["test/selected"] * 2
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "completed"
        assert runtime.security.unseal(saved.result).decode() == "Repository read completed."


async def test_large_connection_catalog_preserves_all_tools_selection_and_replay(runtime, actor, monkeypatch):
    """The observed 19-account/712-tool shape stays discoverable and replayable."""
    from temporalio.api.enums.v1 import EventType

    counts = {"github": 43, "linear": 8, "slack": 5, "notion": 11}
    accounts = ["github"] * 16 + ["linear", "slack", "notion"]
    discoveries, calls, requests = [], [], []
    replaying = False

    def provider(kind):
        class Provider:
            def __init__(self, **kwargs):
                assert activity.in_activity() and not replaying
                self.identity = kwargs["id"].removeprefix("connection_")

            def get_toolset(self):
                if activity.info().activity_type.endswith("__get_tools"):
                    discoveries.append((self.identity, counts[kind]))
                tools = FunctionToolset(id=self.identity)

                def invoke(number: int) -> str:
                    assert activity.in_activity() and not replaying
                    calls.append((self.identity, number))
                    return f"Selected account {self.identity}, item {number}"

                for index in range(counts[kind]):
                    tools.add_function(
                        invoke, name=f"read_item_{index}",
                        description=(
                            f"Read item {index} for account {self.identity}. "
                            + "Full provider parameter and operation documentation. " * 46
                        ),
                        metadata={"annotations": {"readOnlyHint": True}},
                    )
                return tools
        return Provider

    for kind in counts:
        monkeypatch.setitem(SERVICES, kind, provider(kind))
    async with runtime.database.sessions.begin() as db:
        connections = [Connection(
            id=new_id(), owner_id=actor.id, kind=kind, name=f"Account {index}",
            credential=runtime.security.seal("synthetic-account-token"), read_only=True,
        ) for index, kind in enumerate(accounts)]
        db.add_all(connections)

    selected = connections[15]
    name = f"github_{selected.id[:8]}_read_item_42"

    async def stream(messages, info):
        requests.append(1)
        names = {tool.name for tool in info.function_tools}
        if name not in names:
            assert "search_tools" in names
            yield {0: DeltaToolCall(name="search_tools", json_args=json.dumps({"queries": [name]}))}
        elif not any(
            isinstance(part, ToolReturnPart) and part.tool_name == name
            for message in messages for part in message.parts
        ):
            yield {0: DeltaToolCall(name=name, json_args='{"number": 42}')}
        else:
            yield "Large catalog read completed."

    runtime.models["test"].stream_function = stream
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read item 42 on the selected account.", request_key="large-catalog",
        connections=[connection.id for connection in connections],
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
        assert len(discoveries) == 19 and sum(count for _, count in discoveries) == 712
        assert calls == [(selected.id, 42)]
        assert len(requests) == 3
        assert not any(event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED for event in history.events)
        replaying = True
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    assert len(discoveries) == 19 and calls == [(selected.id, 42)]
