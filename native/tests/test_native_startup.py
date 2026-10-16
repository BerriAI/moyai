"""Real Temporal startup/cancellation regressions; controlled model/workspace."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef
from sqlalchemy import func, select, update
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from moyai.agent import make_agent
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.models import Effect, Turn, User
from moyai.settings_runtime import SETTINGS_ACTIVITIES, SettingsSkills, settings_skill_catalog
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("failure", ["revoked_catalog", "failed_workspace_read"])
async def test_startup_failure_drains_sibling_reads_without_temporal_cancel_spin(
    runtime, actor, monkeypatch, tmp_path, failure,
):
    """Both directions of CombinedCapability.for_run's concurrent cancellation."""
    root = tmp_path / "startup-workspace"
    skill = root / "skills" / "synthetic"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: synthetic\ndescription: Synthetic skill\n---\nInstructions.\n"
    )
    catalog_started, catalog_release, catalog_denied = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workspace_started, workspace_release, workspace_failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    counts = {"models": 0, "writes": 0, "catalogs": 0}

    class ReadBackend(LocalWorkspaceBackend):
        async def list_dir(self, path):
            workspace_started.set()
            if failure == "revoked_catalog":
                await workspace_release.wait()
            else:
                workspace_failed.set()
                raise ApplicationError("Synthetic workspace read failed.", non_retryable=True)
            return await super().list_dir(path)

        async def write_bytes(self, *args, **kwargs):
            counts["writes"] += 1
            raise AssertionError("Startup may not perform business writes.")

    class ReadWorkspace(LocalWorkspace[RunScope]):
        def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None):
            backend = ReadBackend(self.working_dir)
            return backend if ref is None or ref == backend.ref else None

    @activity.defn(name="moyai_settings_skill_catalog")
    async def checked_catalog(scope: RunScope) -> list[dict]:
        counts["catalogs"] += 1
        catalog_started.set()
        await catalog_release.wait()
        try:
            return await SettingsSkills(runtime.database).catalog(scope)
        except PermissionError as error:
            catalog_denied.set()
            # Keep this test's live revocation decisive on the first attempt.
            raise ApplicationError("Synthetic authority revoked.", non_retryable=True) from error

    monkeypatch.setattr("moyai.worker.SETTINGS_ACTIVITIES", [
        checked_catalog if item is settings_skill_catalog else item for item in SETTINGS_ACTIVITIES
    ])

    def respond(messages, info):
        counts["models"] += 1
        return ModelResponse(parts=[TextPart("Must never infer after startup failure.")])

    async def stream(messages, info):
        counts["models"] += 1
        yield "Must never infer after startup failure."

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=ReadWorkspace(root), skills_directory="skills",
    )
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Synthetic startup probe", request_key="startup-probe"),
    )
    await runtime.database.claim_next()
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    )
    history = None
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            settled = False
            try:
                await asyncio.wait_for(catalog_started.wait(), 5)
                if failure == "revoked_catalog":
                    await asyncio.wait_for(workspace_started.wait(), 5)
                    async with runtime.database.sessions.begin() as db:
                        await db.execute(update(User).where(User.id == actor.id).values(enabled=False))
                    catalog_release.set()
                    await asyncio.wait_for(catalog_denied.wait(), 5)
                else:
                    await asyncio.wait_for(workspace_failed.wait(), 5)
                # Deliver the startup failure while the other real activity is
                # still blocked. The worker's normal two-second watchdog stays on.
                await asyncio.sleep(0.25)
                assert counts["models"] == 0
                catalog_release.set()
                workspace_release.set()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 10)
                settled = True
                history = await handle.fetch_history()
            finally:
                catalog_release.set()
                workspace_release.set()
                if not settled:
                    with suppress(RPCError):
                        await handle.terminate("Isolated startup regression cleanup")
    assert history is not None
    assert not any(event.HasField("workflow_task_failed_event_attributes") for event in history.events)
    scheduled = [
        event.activity_task_scheduled_event_attributes for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    workspace_calls = [item for item in scheduled if item.activity_type.name.endswith("__workspace__call")]
    assert workspace_calls
    assert all(item.schedule_to_close_timeout.seconds == 600 for item in workspace_calls)
    assert not any("__model__" in item.activity_type.name for item in scheduled)
    assert counts == {"models": 0, "writes": 0, "catalogs": 1}
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "failed"
        assert await db.scalar(select(func.count()).select_from(Effect)) == 0
