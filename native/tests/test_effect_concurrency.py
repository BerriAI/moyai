"""SDK/Temporal regressions; controlled tools supplement the separate live-app proof."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import durable_operation
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import SubAgents
from sqlalchemy import select
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.agent import HarnessActivityContext
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.effects import EffectReceipts, PendingEffectError, requires_receipt
from moyai.live_events import root_event_stream
from moyai.models import Effect, User
from moyai.runtime import Runtime
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


class BeforeSiblingGate(EffectReceipts):
    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        return await handler(args)

    @durable_operation("reserve")
    async def reserve(
        self, scope: RunScope, identity: str, name: str, args: dict[str, Any],
        shell_readiness: bool = False,
    ) -> None:
        try:
            await super().reserve(scope, identity, name, args, shell_readiness)
        except PendingEffectError as error:
            # Preserve the original activity failure in the legacy replay
            # fixture, before pending reservations became recoverable tool errors.
            raise PermissionError(str(error)) from error


class ParallelChildren:
    """Barriers prove model overlap and control cancellation without timing guesses."""

    def __init__(self, count=5, *, complete_first=False):
        self.count = count
        self.models_started: set[int] = set()
        self.models_ready = asyncio.Event()
        self.first_shell_dispatched = asyncio.Event()
        self.shell_started = asyncio.Event()
        self.release_shell = asyncio.Event()
        self.executed: list[int] = []
        self.active_shells = 0
        self.peak_shells = 0
        self.complete_first = complete_first
        self.executed_sessions: set[str] = set()
        self.parallel_sessions = asyncio.Event()

    async def respond(self, messages, info):
        prompt = next(
            str(part.content) for message in messages for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if returned:
            return ModelResponse(parts=[TextPart("Completed.")])
        elif prompt.startswith("CHILD "):
            child = int(prompt.split()[1])
            self.models_started.add(child)
            if len(self.models_started) == self.count:
                self.models_ready.set()
            # Serializing delegate_task or model execution deadlocks this barrier.
            await asyncio.wait_for(self.models_ready.wait(), 15)
            if child != 0:
                # Make the old failure deterministic: one genuine tool is in
                # flight before siblings attempt their reservations.
                await asyncio.wait_for(self.first_shell_dispatched.wait(), 15)
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"child": child}, tool_call_id=f"shell-{child}",
            )])
        else:
            return ModelResponse(parts=[
                ToolCallPart(
                    "delegate_task", {"agent_name": "self", "task": f"CHILD {child}"},
                    tool_call_id=f"delegate-{child}",
                ) for child in range(self.count)
            ])

    async def stream(self, messages, info):
        for index, part in enumerate((await self.respond(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    async def shell(self, ctx: RunContext[RunScope], child: int) -> str:
        self.executed.append(child)
        self.first_shell_dispatched.set()
        self.executed_sessions.add(ctx.deps.session_id)
        if len(self.executed_sessions) == 2:
            self.parallel_sessions.set()
        self.active_shells += 1
        self.peak_shells = max(self.peak_shells, self.active_shells)
        if not self.complete_first or len(self.executed) == 2:
            self.shell_started.set()
        try:
            if not self.complete_first or len(self.executed) != 1:
                await self.release_shell.wait()
            return f"Child {child} wrote once."
        finally:
            self.active_shells -= 1


def install_agent(runtime, scenario, *, legacy=False, stream_events=True):
    model = FunctionModel(scenario.respond, stream_function=scenario.stream)
    runtime.models = {"test": model}
    runtime.durability = TemporalDurability(
        models=runtime.models, run_context_type=HarnessActivityContext,
        activity_config={
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=1),
        },
        event_stream_handler=root_event_stream if stream_events else None,
    )
    tools = FunctionToolset(id="concurrent_effect_test")
    tools.add_function(scenario.shell, name="shell")
    runtime.agent = Agent(
        model, name="moyai", deps_type=RunScope,
        capabilities=[
            (BeforeSiblingGate if legacy else EffectReceipts)(runtime.database),
            SubAgents(include_self=True),
            runtime.durability,
        ],
        toolsets=[tools],
    )


async def effect_rows(runtime, session_id):
    async with runtime.database.sessions() as db:
        return list(await db.scalars(select(Effect).where(
            Effect.session_id == session_id,
        ).order_by(Effect.created_at)))


@pytest.mark.parametrize("legacy", [False, True])
async def test_five_native_children_keep_models_parallel_and_replay(runtime, active, legacy):
    await qualify_parallel_children(runtime, active, legacy=legacy)


async def qualify_parallel_children(runtime, active, *, legacy=False):
    session, turn, _ = active
    scenario = ParallelChildren()
    install_agent(runtime, scenario, legacy=legacy)
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(scenario.models_ready.wait(), 20)
            await asyncio.wait_for(scenario.shell_started.wait(), 20)
            assert len(scenario.models_started) == 5
            rows = await effect_rows(runtime, session.id)
            assert len(rows) == 1 and rows[0].state == "pending"
            if legacy:
                # The pre-fix source must fail while its first shell is live.
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 20)
                assert len(scenario.executed) == 1
            else:
                scenario.release_shell.set()
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                assert sorted(scenario.executed) == list(range(5))
                assert scenario.peak_shells == 1
                assert [r.state for r in await effect_rows(runtime, session.id)] == ["confirmed"] * 5
            history = await handle.fetch_history()
            scenario.release_shell.set()
        # New code must replay both new successful and pre-fix failed histories.
        install_agent(runtime, scenario)
        TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
        count = len(scenario.executed)
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
        assert len(scenario.executed) == count


@pytest.mark.parametrize("complete_first", [False, True])
async def test_cancel_waiting_children_leaves_only_dispatched_effect_uncertain(runtime, active, complete_first):
    session, turn, prepared = active
    scenario = ParallelChildren(complete_first=complete_first)
    # Isolate tool lifecycle cancellation from the SDK's streamed-event teardown.
    install_agent(runtime, scenario, stream_events=False)
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(scenario.models_ready.wait(), 20)
            await asyncio.wait_for(scenario.shell_started.wait(), 20)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 20)
            rows = await effect_rows(runtime, session.id)
            expected = ["confirmed", "pending"] if complete_first else ["pending"]
            assert len(scenario.executed) == len(expected)
            assert [row.state for row in rows] == expected and rows[-1].resolution is None
            scenario.release_shell.set()
            history = await handle.fetch_history()
        # A newly authorized turn still cannot bypass the uncertain operation.
        async with runtime.database.sessions() as db:
            actor = await db.get(User, prepared.scope.actor_id)
        following = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Do not replay the unknown effect.", request_key="after-cancel",
        ))
        await runtime.database.claim_next()
        admitted = await runtime.database.prepare(following.id)
        receipts = EffectReceipts(runtime.database)
        with pytest.raises(PermissionError, match="reconciliation"):
            await receipts.reserve(admitted.scope, "never-replay-unknown", "shell", {"child": 99})
        assert [r.state for r in await effect_rows(runtime, session.id)] == expected
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
        assert len(scenario.executed) == len(expected)


async def test_independent_workflows_execute_guarded_tools_in_parallel(runtime, active, actor):
    first_session, first_turn, _ = active
    second_session = await runtime.database.create_session(actor, NewSession())
    second_turn = await runtime.database.submit(actor, second_session.id, NewMessage(
        prompt="Independent parallel workspace.", request_key="second-workflow",
    ))
    await runtime.database.claim_next()
    scenario = ParallelChildren()
    install_agent(runtime, scenario)
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handles = [
                await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                ) for turn in (first_turn, second_turn)
            ]
            # A process-global lock would prevent the second workspace entering.
            await asyncio.wait_for(scenario.parallel_sessions.wait(), 20)
            assert scenario.active_shells == 2
            scenario.release_shell.set()
            results = await asyncio.wait_for(asyncio.gather(*(handle.result() for handle in handles)), 30)
            assert all(result["status"] == "completed" for result in results)
    for session in (first_session, second_session):
        assert [r.state for r in await effect_rows(runtime, session.id)] == ["confirmed"] * 5
    assert len(scenario.executed) == 10


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set an isolated local PostgreSQL URL.")
async def test_five_children_with_real_postgres_uncertainty_index(tmp_path):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = f"moyai_effect_test_{uuid4().hex}"
    admin = await asyncpg.connect(urlunsplit(base))
    await admin.execute(f'CREATE DATABASE {name} ENCODING \'UTF8\' TEMPLATE template0')
    runtime = Runtime(Settings(
        _env_file=None, data_dir=tmp_path / "data",
        database_url=urlunsplit(base._replace(path=f"/{name}")),
        encryption_key="effect-concurrency-local-test-key-only",
        workspace_password="test-password-12345", default_model="test",
    ))
    from pydantic_ai.capabilities import LocalWorkspace
    from scenarios import integration_model

    computer = tmp_path / "computer"
    computer.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", computer / "skills")
    try:
        await runtime.start(models={"test": integration_model("test")}, workspace=LocalWorkspace(computer))
        async with runtime.database.sessions() as db:
            actor = await db.scalar(select(User))
        session = await runtime.database.create_session(actor, NewSession())
        turn = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Five native children.", request_key="postgres-effects",
        ))
        await runtime.database.claim_next()
        prepared = await runtime.database.prepare(turn.id)
        await qualify_parallel_children(runtime, (session, turn, prepared))
    finally:
        await runtime.close()
        await admin.execute(f'DROP DATABASE {name} WITH (FORCE)')
        await admin.close()


def test_receipt_registration_preserves_parallel_non_effect_tools():
    assert not requires_receipt(ToolDefinition(name="delegate_task"))
    assert not requires_receipt(ToolDefinition(name="read_file"))
    for name in ("automation_grant", "automation_self_update", "automation_claim_item", "automation_webhook_setup"):
        assert requires_receipt(ToolDefinition(name=name))


@pytest.mark.parametrize("enabled", [False, True])
def test_credential_command_receipt_keeps_old_history_branch(monkeypatch, enabled):
    from moyai.effects import workflow

    markers = []
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)

    def patched(marker):
        markers.append(marker)
        return enabled

    monkeypatch.setattr(workflow, "patched", patched)
    assert requires_receipt(ToolDefinition(name="credentials_run")) is enabled
    assert markers == ["moyai-credential-command-receipts-v1"]
