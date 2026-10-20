"""Real Temporal replay with a disposable filesystem provider, not live-app proof."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef, WorkspaceUnavailableError
from sqlalchemy import delete, select
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model

from moyai.agent import make_agent
from moyai.computer_keepalive import Keepalive, keep_computer_alive
from moyai.contracts import RunScope
from moyai.maintenance_workspace import VERSION
from moyai.models import ComputerLease, Effect, Session, Turn
from moyai.product_drain import ProductDrainService
from moyai.product_drain_models import ProductDrainReceipt, ProductDrainState
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("refusal", ["none", "lease", "uncertain", "unpause", "expired"])
async def test_expired_files_resume_in_a_fresh_sdk_run_and_both_histories_replay(
    runtime, active, monkeypatch, refusal,
):
    from moyai import worker

    session, turn, prepared = active
    root = runtime.settings.data_dir.parent
    old, image, restored = root / "computer", root / "image", root / "restored"
    wrote, release, captured = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempted = asyncio.Event()
    expired = False
    operations = []
    captures = 0
    capture_activities = []

    class Backend(LocalWorkspaceBackend):
        def __init__(self, ref):
            self.identity = ref.id if ref else "old"
            super().__init__(restored if self.identity == "restored" else old)
            self.attached = ref is not None

        @property
        def ref(self):
            return WorkspaceRef(provider="modal", id=self.identity) if self.attached else None

        async def working_dir(self):
            self.attached = True
            if self.identity == "old" and expired:
                raise WorkspaceUnavailableError("The disposable source expired.")
            return await super().working_dir()

        async def run(self, command, **kwargs):
            if self.identity == "old" and expired:
                raise WorkspaceUnavailableError("The disposable source expired.")
            result = await super().run(command, **kwargs)
            operations.append((self.identity, command))
            if "printf preserved" in str(command) and not wrote.is_set():
                wrote.set()
                await release.wait()
            return result

    class Computer(LocalWorkspace[RunScope]):
        def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None):
            return Backend(ref) if ref is None or ref.provider == "modal" else None

    def respond(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not any(part.tool_call_id == "write-once" for part in returns):
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "printf preserved >> checkpoint-file.txt"}, tool_call_id="write-once",
            )])
        if not any(part.tool_call_id == "read-after-expiry" for part in returns):
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "cat checkpoint-file.txt"}, tool_call_id="read-after-expiry",
            )])
        completed_directory = old if refusal == "unpause" else restored
        assert completed_directory.joinpath("checkpoint-file.txt").read_text() == "preserved"
        return ModelResponse(parts=[TextPart("The original files survived.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=Computer(old), skills_directory="skills",
    )
    runtime.settings.history_continue_bytes = 10_000_000
    runtime.settings.history_continue_events = 10_000
    service = ProductDrainService(
        sessions=lambda: runtime.database.sessions, authority=lambda: None,
        native_schema="", task_queue=runtime.settings.temporal_queue,
    )
    service.register_worker = AsyncMock()
    runtime.product_drain = service
    async with runtime.database.sessions.begin() as db:
        db.add(ProductDrainState(id=1, generation=21, paused=False))

    async def capture(*_args, **_kwargs):
        nonlocal captures
        captures += 1
        capture_activities.append(activity.info().activity_type)
        attempted.set()
        if refusal == "expired":
            raise WorkspaceUnavailableError("The disposable source expired before capture.")
        if refusal == "uncertain":
            raise TimeoutError("The provider capture reply was lost.")
        if refusal == "unpause" and activity.info().activity_type == "moyai_save_maintenance_checkpoint_v1":
            async with runtime.database.sessions.begin() as db:
                state = await db.get(ProductDrainState, 1)
                state.paused, state.generation = False, 23
        if image.exists():
            shutil.rmtree(image)
        shutil.copytree(restored if restored.exists() else old, image)
        captured.set()
        return "image-files"

    async def restore(*_args):
        shutil.copytree(image, restored, dirs_exist_ok=True)
        return SimpleNamespace(object_id="restored", poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    @activity.defn(name="moyai_keep_computer_alive_v1")
    async def test_keepalive(scope: RunScope) -> Keepalive:
        return Keepalive(True, 60)

    async def capture_available(_runtime, _reference):
        nonlocal expired
        if refusal == "expired":
            # Expire at the settled capture boundary, after the persistent
            # shell has finished reading its own status/output files.
            expired = True
            shutil.rmtree(old)
            return False
        return True

    monkeypatch.setattr(worker, "ACTIVITIES", [
        test_keepalive if item is keep_computer_alive else item for item in worker.ACTIVITIES
    ])
    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(capture=capture))
    monkeypatch.setattr("moyai.maintenance_workspace.live_sandbox", AsyncMock(return_value=False))
    monkeypatch.setattr("moyai.maintenance_workspace.capture_available", capture_available)
    monkeypatch.setattr("moyai.maintenance_workspace.restored_sandbox", restore)
    converter = encrypted_converter(runtime.security, runtime.objects)
    histories = []
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            first_run = handle.result_run_id
            await asyncio.wait_for(wrote.wait(), 30)
            async with runtime.database.sessions.begin() as db:
                state = await db.get(ProductDrainState, 1)
                state.paused, state.generation = True, 22
                if refusal == "lease":
                    db.add(ComputerLease(
                        session_id=session.id, actor_id=prepared.scope.actor_id,
                        expires_at=int(time.time()) + 300,
                    ))
            release.set()
            if refusal == "expired":
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 30)
                async with runtime.database.sessions() as db:
                    failed = await db.get(Turn, turn.id)
                    assert failed.status == "failed" and "verified checkpoint" in failed.error
                    assert (await db.get(Session, session.id)).snapshot is None
                assert "moyai_save_maintenance_checkpoint_v1" not in capture_activities
                assert not restored.exists()
                return
            if refusal == "uncertain":
                await asyncio.wait_for(attempted.wait(), 30)
                async with runtime.database.sessions.begin() as db:
                    state = await db.get(ProductDrainState, 1)
                    state.paused, state.generation = False, 23
                await handle.signal("maintenance_changed", 23)
                await asyncio.sleep(7)
                assert captures == 1
                async with runtime.database.sessions() as db:
                    assert (await db.get(Turn, turn.id)).status == "running"
                    assert (await db.get(ProductDrainReceipt, (turn.id, 22))).phase == "blocked"
                    assert "outcome is unknown" in (await db.get(Session, session.id)).save_warning
                assert not any("cat checkpoint-file.txt" in str(command) for _, command in operations)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 30)
                return
            if refusal == "lease":
                # The non-retryable preparation refusal must leave the graph
                # running and blocked, then retry without a new generation hint.
                async with asyncio.timeout(30):
                    while True:
                        async with runtime.database.sessions() as db:
                            receipt = await db.get(ProductDrainReceipt, (turn.id, 22))
                            if receipt is not None:
                                assert receipt.phase == "blocked"
                                assert (await db.get(Turn, turn.id)).status == "running"
                                break
                        await asyncio.sleep(.05)
                await asyncio.sleep(.2)
                async with runtime.database.sessions.begin() as db:
                    await db.execute(delete(ComputerLease).where(ComputerLease.session_id == session.id))
            await asyncio.wait_for(captured.wait(), 30)
            if refusal == "unpause":
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                history = await handle.fetch_history()
                await Replayer(
                    workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                    interceptors=[ActivityCapacity()],
                ).replay_workflow(history)
                assert capture_activities == ["moyai_save_maintenance_checkpoint_v1", "moyai_save_computer_v1"]
                assert old.joinpath("checkpoint-file.txt").read_text() == "preserved"
                assert not restored.exists()
                async with runtime.database.sessions() as db:
                    assert (await db.get(Session, session.id)).save_warning is None
                return
            async with asyncio.timeout(30):
                while True:
                    async with runtime.database.sessions() as db:
                        receipt = await db.get(ProductDrainReceipt, (turn.id, 22))
                        if receipt and receipt.phase == "parked" and receipt.workflow_run_id != first_run:
                            break
                    await asyncio.sleep(.05)
            expired = True
            shutil.rmtree(old)
            async with runtime.database.sessions.begin() as db:
                state = await db.get(ProductDrainState, 1)
                state.paused, state.generation = False, 23
            await runtime.client.get_workflow_handle(turn.workflow_id).signal("maintenance_changed", 23)
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            run_id = first_run
            while run_id:
                history = await runtime.client.get_workflow_handle(turn.workflow_id, run_id=run_id).fetch_history()
                histories.append(history)
                last = history.events[-1]
                run_id = (
                    last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                    if last.HasField("workflow_execution_continued_as_new_event_attributes") else None
                )
        assert len(histories) == 2
        loads = [
            event.activity_task_scheduled_event_attributes
            for event in histories[1].events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name == "moyai_load_maintenance_checkpoint_v1"
        ]
        assert loads
        # The configured Lambda maximum allows running + health + initialize
        # phases totaling 21 minutes, before provider/storage overhead.
        assert all(item.start_to_close_timeout.seconds > 21 * 60 for item in loads)
        assert all(item.retry_policy.maximum_attempts == 1 for item in loads)
        for history in histories:
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
    assert restored.joinpath("checkpoint-file.txt").read_text() == "preserved"
    assert sum("printf preserved" in str(command) for _, command in operations) == 1
    assert any(identity == "restored" and "cat checkpoint-file.txt" in str(command) for identity, command in operations)
    async with runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, session.id)).workspace)["id"] == "restored"
        assert (await db.get(Turn, turn.id)).status == "completed"
        effects = list(await db.scalars(select(Effect).where(Effect.turn_id == turn.id)))
        assert len(effects) == 2 and all(row.state == "confirmed" for row in effects)


async def test_pre_preservation_workflow_replays_without_new_commands(runtime, active, monkeypatch):
    _, turn, _ = active
    converter = encrypted_converter(runtime.security, runtime.objects)
    original = workflow.patched
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as previous:
            previous.setattr(workflow, "patched", lambda identity: False if identity == VERSION else original(identity))
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert not any("maintenance_checkpoint" in name or "maintenance_boundary" in name for name in scheduled)
