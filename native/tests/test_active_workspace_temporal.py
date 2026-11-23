"""Real Temporal recovery/replay with disposable files; not live-provider proof."""
from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic_ai import RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef, WorkspaceUnavailableError
from sqlalchemy import select
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.active_workspace import RECOVERY_NOTICE
from moyai.agent import make_agent
from moyai.computer_keepalive import Keepalive, keep_computer_alive
from moyai.contracts import RunScope
from moyai.models import Effect, Session
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


async def test_dead_computer_continues_current_graph_without_repeating_completed_tools(
    runtime, active, monkeypatch,
):
    from moyai import worker

    session, turn, _prepared = active
    root = runtime.settings.data_dir.parent
    old, image, restored = root / "computer", root / "image", root / "replacement"
    thinking, release = asyncio.Event(), asyncio.Event()
    expired = False
    commands = []
    model_calls = 0
    observed_recovery = False

    class Backend(LocalWorkspaceBackend):
        def __init__(self, ref):
            self.identity = ref.id if ref else "old"
            super().__init__(restored if self.identity == "replacement" else old)
            self.attached = ref is not None

        @property
        def ref(self):
            return WorkspaceRef(provider="modal", id=self.identity) if self.attached else None

        async def working_dir(self):
            self.attached = True
            if self.identity == "old" and expired:
                raise WorkspaceUnavailableError("The source computer expired.")
            return await super().working_dir()

        async def run(self, command, **kwargs):
            if self.identity == "old" and expired:
                raise WorkspaceUnavailableError("The source computer expired.")
            result = await super().run(command, **kwargs)
            commands.append((self.identity, command))
            return result

    class Computer(LocalWorkspace[RunScope]):
        def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None):
            return Backend(ref) if ref is None or ref.provider == "modal" else None

    async def respond(messages, info):
        nonlocal model_calls, observed_recovery
        model_calls += 1
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        ids = {part.tool_call_id for part in returns}
        if "write-once" not in ids:
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "printf preserved >> checkpoint-file.txt"}, "write-once",
            )])
        if "refused-read" not in ids:
            thinking.set()
            await release.wait()
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "cat checkpoint-file.txt"}, "refused-read",
            )])
        observed_recovery = any(
            isinstance(part, UserPromptPart) and part.content == RECOVERY_NOTICE
            for message in messages for part in message.parts
        )
        assert observed_recovery
        if "read-on-replacement" not in ids:
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "cat checkpoint-file.txt"}, "read-on-replacement",
            )])
        assert restored.joinpath("checkpoint-file.txt").read_text() == "preserved"
        return ModelResponse(parts=[TextPart("The same task finished with its saved file.")])

    async def stream(messages, info):
        for index, part in enumerate((await respond(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=Computer(old), skills_directory="skills",
    )

    async def capture(reference, **kwargs):
        if image.exists():
            shutil.rmtree(image)
        shutil.copytree(restored if reference.id == "replacement" else old, image)
        return "saved-image"

    async def restore(*args):
        shutil.copytree(image, restored, dirs_exist_ok=True)
        return SimpleNamespace(object_id="replacement", poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    @activity.defn(name="moyai_keep_computer_alive_v1")
    async def keepalive(scope: RunScope) -> Keepalive:
        return Keepalive(True, 60)

    async def live(reference):
        return reference["id"] == "replacement" or not expired

    monkeypatch.setattr(worker, "ACTIVITIES", [
        keepalive if item is keep_computer_alive else item for item in worker.ACTIVITIES
    ])
    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(capture=capture))
    monkeypatch.setattr("moyai.active_workspace.live_sandbox", live)
    monkeypatch.setattr("moyai.active_workspace.restored_sandbox", restore)
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            first_run = handle.result_run_id
            try:
                await asyncio.wait_for(thinking.wait(), 30)
                assert image.joinpath("checkpoint-file.txt").read_text() == "preserved"
                expired = True
                shutil.rmtree(old)
            finally:
                release.set()
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            first = await runtime.client.get_workflow_handle(turn.workflow_id, run_id=first_run).fetch_history()
            next_run = next(
                event.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                for event in first.events if event.HasField("workflow_execution_continued_as_new_event_attributes")
            )
            second = await runtime.client.get_workflow_handle(turn.workflow_id, run_id=next_run).fetch_history()
        assert observed_recovery
        assert restored.joinpath("checkpoint-file.txt").read_text() == "preserved"
        assert sum("printf preserved" in str(command) for _, command in commands) == 1
        calls = model_calls
        for history in (first, second):
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
        assert model_calls == calls
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        assert current.status == "idle" and json.loads(current.workspace)["id"] == "replacement"
        effects = list(await db.scalars(select(Effect).where(Effect.turn_id == turn.id)))
        assert sorted(effect.state for effect in effects) == ["confirmed", "confirmed", "not_dispatched"]
