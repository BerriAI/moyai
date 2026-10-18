"""Real Temporal routing tests; scripted models supplement the actual-app demo."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.run import AgentRunResultEvent
from sqlalchemy import select
from temporalio.api.deployment.v1 import WorkerDeploymentVersion as ProtoVersion
from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentRequest,
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client
from temporalio.common import WorkerDeploymentVersion
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

from moyai.deployment_handoff import DeploymentHandoff
from moyai.history_handoff import AgentCheckpoint, save_history_checkpoint
from moyai.inference_usage import InferenceUsage
from moyai.live_events import PUBLIC_TOPIC, ResponseStart
from moyai.models import Effect, Event, Turn
from moyai.runtime import configure
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


async def promote(client: Client, deployment: str, build: str) -> None:
    """Wait for all four real pollers, then change routing with a CAS token."""
    async with asyncio.timeout(30):
        while True:
            try:
                version = await client.workflow_service.describe_worker_deployment_version(
                    DescribeWorkerDeploymentVersionRequest(
                        namespace=client.namespace,
                        deployment_version=ProtoVersion(deployment_name=deployment, build_id=build),
                    ),
                )
                if len({queue.name for queue in version.worker_deployment_version_info.task_queue_infos}) == 4:
                    break
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            await asyncio.sleep(.1)
    state = await client.workflow_service.describe_worker_deployment(DescribeWorkerDeploymentRequest(
        namespace=client.namespace, deployment_name=deployment,
    ))
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=client.namespace, deployment_name=deployment, build_id=build,
            conflict_token=state.conflict_token, identity="moyai-checkpoint-test",
        ),
    )


@pytest.mark.parametrize("runtime", ["history"], indirect=True)
async def test_pinned_turn_moves_at_settled_checkpoint_without_repeating_effects(
    runtime, active, monkeypatch,
):
    _session, turn, _prepared = active
    entered, release = asyncio.Event(), asyncio.Event()
    model = runtime.models["test"]
    original = model.stream_function

    async def gated(messages, info):
        if not entered.is_set():
            entered.set()
            await release.wait()
        async for part in original(messages, info):
            yield part

    monkeypatch.setattr(model, "stream_function", gated)
    deployment = f"handoff-{uuid4().hex}"
    runtime.settings.worker_deployment = deployment
    runtime.settings.worker_build_id = "blue"
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=encrypted_converter(runtime.security, runtime.objects),
    )
    async with environment:
        runtime.client = environment.client
        blue = make_worker(runtime)
        blue_identity = blue.workers[0].config()["identity"]
        async with blue:
            await promote(runtime.client, deployment, "blue")
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            first_run = handle.result_run_id
            await asyncio.wait_for(entered.wait(), 30)
            assert (await handle.query(TurnWorkflow.deployment_checkpoint))["build_id"] == "blue"
            streamed = []

            async def observe():
                offset = 0
                while True:
                    previous = offset
                    async with runtime.durability.stream_agent_events(
                        runtime.client, handle, topic=PUBLIC_TOPIC, from_offset=offset,
                    ) as events:
                        async for event in events:
                            streamed.append((events.offset, event))
                            offset = events.offset + 1
                            if isinstance(event, AgentRunResultEvent):
                                return
                    if offset == previous:
                        return

            observer = asyncio.create_task(observe())
            runtime.settings.worker_build_id = "green"
            green = make_worker(runtime)
            green_identity = green.workers[0].config()["identity"]
            async with green:
                await promote(runtime.client, deployment, "green")
                # Changing new-work routing must not move an active model call.
                assert (await handle.query(TurnWorkflow.deployment_checkpoint))["build_id"] == "blue"
                await handle.signal(TurnWorkflow.checkpoint_handoff, DeploymentHandoff(
                    "wrong-source", deployment, "green",
                ))
                assert (await handle.query(TurnWorkflow.deployment_checkpoint))["request_id"] is None
                request = DeploymentHandoff("release-1", deployment, "blue")
                await handle.signal(TurnWorkflow.checkpoint_handoff, request)
                assert (await handle.query(TurnWorkflow.deployment_checkpoint))["request_id"] == "release-1"
                release.set()
                result = await asyncio.wait_for(handle.result(), 90)
                await asyncio.wait_for(observer, 15)
                state = await handle.query(TurnWorkflow.deployment_checkpoint)
                assert state["build_id"] == "green"
                assert state["previous_request_id"] == "release-1"
                history = await runtime.client.get_workflow_handle(
                    turn.workflow_id, run_id=first_run,
                ).fetch_history()
                assert history.events[-1].HasField("workflow_execution_continued_as_new_event_attributes")
                final_history = await runtime.client.get_workflow_handle(turn.workflow_id).fetch_history()
                for run_history, identity in [(history, blue_identity), (final_history, green_identity)]:
                    activity_owners = {
                        event.activity_task_started_event_attributes.identity
                        for event in run_history.events if event.HasField("activity_task_started_event_attributes")
                    }
                    assert activity_owners == {identity}
    assert result["status"] == "completed"
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        effects = list(await db.scalars(select(Effect).where(Effect.turn_id == turn.id)))
        segments = list(await db.scalars(select(Event).where(
            Event.turn_id == turn.id, Event.kind == "history_segment",
        )))
    assert saved.status == "completed"
    assert json.loads(saved.usage)["requests"] == 8
    assert len(effects) == 6 and all(effect.state == "confirmed" for effect in effects)
    assert len(segments) == 1
    assert (Path(runtime.settings.data_dir).parent / "computer/handoff-ledger.txt").read_text().splitlines() == [
        "1", "2", "3", "4", "5", "6",
    ]
    assert [offset for offset, _ in streamed] == sorted({offset for offset, _ in streamed})
    steps = [event.step for _, event in streamed if isinstance(event, ResponseStart)]
    assert steps == sorted(set(steps)) and len(steps) == 8


@pytest.mark.parametrize("deployment,build", [("fleet", ""), ("", "blue")])
def test_partial_worker_identity_cannot_enable_versioned_polling(deployment, build):
    with pytest.raises(ValidationError, match="configured together"):
        Settings(_env_file=None, worker_deployment=deployment, worker_build_id=build)


def test_stale_or_unversioned_requests_cannot_move_another_release():
    request = DeploymentHandoff("release-1", "fleet", "blue")
    assert request.applies_to(WorkerDeploymentVersion("fleet", "blue"))
    assert not request.applies_to(WorkerDeploymentVersion("fleet", "green"))
    assert not request.applies_to(WorkerDeploymentVersion("another-fleet", "blue"))
    assert not request.applies_to(None)


@pytest.mark.parametrize("status", ["pending", "unknown"])
async def test_unsettled_gateway_attempt_refuses_deployment_checkpoint(runtime, active, status):
    configure(runtime)
    session, turn, prepared = active
    checkpoint = AgentCheckpoint(
        prepared=prepared,
        conversation=ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id,
        )).decode(),
        workspace=None, run_step=1, output_retries_used=0,
        deployment_handoff=DeploymentHandoff("release-1", "fleet", "blue"),
    )
    async with runtime.database.sessions.begin() as db:
        db.add(InferenceUsage(
            id="attempt", request_key="attempt", session_id=session.id,
            turn_id=turn.id, actor_id=turn.actor_id, model=turn.model,
            provider_scope="test-gateway", created_at=turn.created_at, status=status,
        ))
    assert (await save_history_checkpoint(checkpoint)).reference is None
    # A separately confirmed outcome makes the same saved agent state eligible.
    async with runtime.database.sessions.begin() as db:
        attempt = await db.get(InferenceUsage, "attempt")
        attempt.status = "completed"
    assert (await save_history_checkpoint(checkpoint)).reference is not None
