"""Persistence and capability boundaries for a Kubernetes-backed session."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError
from temporalio.exceptions import ApplicationError

from moyai.answer_settlement import ProviderSnapshots
from moyai.computer import lease
from moyai.computer_keepalive import keep_computer_alive
from moyai.computer_startup import OnDemandRepository
from moyai.continuations.product_continuation_restore import ProductWorkspaceRestorer
from moyai.continuations.product_continuation_workspace import ProductWorkspaceCompatibility
from moyai.contracts import NewMessage, RunScope
from moyai.history_handoff import AgentCheckpoint
from moyai.kubernetes_workspace import KubernetesSandbox
from moyai.lambda_workspace import WorkspaceProviders
from moyai.maintenance_workspace import preserve, wait_reason
from moyai.models import ComputerDesktop, ComputerLease, ComputerOperation, Effect, Session
from moyai.product_drain import ProductDrainService
from moyai.product_drain_models import ProductDrainReceipt, ProductDrainState
from moyai.workspace_recovery import prepare_workspace

REF = WorkspaceRef(provider="kubernetes", id="workspace-" + "b" * 40 + ".00000000-0000-4000-8000-000000000001")
REFERENCE = {"provider": REF.provider, "id": REF.id}


@pytest.mark.parametrize(("default", "prepared_provider", "ref"), [
    ("kubernetes", None, None),
    ("modal", "kubernetes", None),
    ("lambda", None, REF),
])
def test_dispatch_uses_recorded_provider(default, prepared_provider, ref, monkeypatch):
    scope = RunScope("session", "turn", "actor", "test", workspace_provider=prepared_provider)
    selected = Mock(return_value=object())
    unused = Mock()
    monkeypatch.setattr(KubernetesSandbox, "get_workspace", selected)
    providers = WorkspaceProviders(SimpleNamespace(sandbox_provider=default), unused)
    ctx = SimpleNamespace(deps=scope)
    assert providers.get_workspace(ctx, ref=ref) is selected.return_value
    selected.assert_called_once_with(ctx, ref=ref)
    unused.get_workspace.assert_not_called()


async def test_settle_delete_restore_and_resume_the_recorded_volume(runtime, actor, active, monkeypatch):
    session, turn, prepared = active
    await runtime.database.save_workspace_reference(prepared.scope, REFERENCE)
    with pytest.raises(PermissionError, match="different workspace"):
        await runtime.database.save_workspace_reference(prepared.scope, {**REFERENCE, "id": "another-workspace"})
    conversation = ConversationTypeAdapter.dump_json(Conversation(conversation_id=prepared.scope.conversation_id)).decode()
    assert await runtime.database.save_result(
        prepared.scope, conversation=conversation, output="Completed", workspace=REFERENCE,
        usage={}, checkpoint_workspace=True,
    )
    snapshots = AsyncMock()
    runtime.database.answers.snapshots = SimpleNamespace(capture=snapshots)
    await runtime.database.answers.save_computer(turn.id, REFERENCE, product=False)
    await runtime.database.delete_session(actor, session.id)
    await runtime.database.restore_session(actor, session.id)
    followup = await runtime.admission.submit(actor, session.id, NewMessage(prompt="Read saved files", request_key="read"))
    assert (await runtime.database.claim_next()).id == followup.id
    prepared = await runtime.admission.prepare(followup.id)
    assert prepared.scope.workspace_provider == "kubernetes"
    ready = AsyncMock(return_value="/workspace")
    resume = Mock(return_value=SimpleNamespace(working_dir=ready))
    monkeypatch.setattr(KubernetesSandbox, "resume", resume)
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    assert (await prepare_workspace(runtime, prepared)).workspace == REFERENCE
    assert (await keep_computer_alive(prepared.scope)).active
    assert ready.await_count == resume.call_count == 2
    resume.assert_called_with(prepared.scope, REF)
    snapshots.assert_not_awaited()
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        assert json.loads(current.workspace) == REFERENCE
        assert current.snapshot is None and not current.save_warning
    ready.side_effect = WorkspaceUnavailableError("Original claim missing")
    with pytest.raises(ApplicationError) as error:
        await prepare_workspace(runtime, prepared)
    assert error.value.details == ("workspace_volume_unavailable",)
    assert not (await keep_computer_alive(prepared.scope)).active


async def test_unsettled_command_blocks_volume_reattachment(runtime, active, monkeypatch):
    session, turn, prepared = active
    await runtime.database.save_workspace_reference(prepared.scope, REFERENCE)
    async with runtime.database.sessions.begin() as db:
        db.add(Effect(
            id="uncertain-command", session_id=session.id, turn_id=turn.id, actor_id=prepared.scope.actor_id,
            tool_name="shell", arguments=runtime.security.seal("{}"), fingerprint="original", state="pending",
        ))
    resume = Mock()
    monkeypatch.setattr(KubernetesSandbox, "resume", resume)
    with pytest.raises(ApplicationError) as denied:
        await prepare_workspace(runtime, replace(prepared, workspace=REFERENCE))
    assert denied.value.details == ("workspace_uncertain_effect",)
    resume.assert_not_called()


async def test_desktop_and_capture_refuse_before_mutating_the_session(runtime, actor, active):
    session, _, prepared = active
    await runtime.database.save_workspace_reference(prepared.scope, REFERENCE)
    async with runtime.database.sessions() as db:
        revision = (await db.get(Session, session.id)).revision
    with pytest.raises(HTTPException) as denied:
        await lease(runtime, session.id, actor, "start")
    assert denied.value.status_code == 409 and "Kubernetes" in denied.value.detail
    async with runtime.database.sessions() as db:
        assert (await db.get(Session, session.id)).revision == revision
        for table in (ComputerLease, ComputerOperation, ComputerDesktop):
            assert await db.get(table, session.id) is None
    with pytest.raises(ApplicationError) as capture:
        await ProviderSnapshots(runtime.database).capture(REF, require_live=True)
    assert capture.value.details == ("workspace_snapshot_unsupported",)


async def test_desktop_tool_on_fresh_session_does_not_allocate(active):
    _, _, prepared = active
    ctx = SimpleNamespace(deps=replace(prepared.scope, workspace_provider="kubernetes"), workspace=AsyncMock())
    with pytest.raises(ToolFailed, match="live desktop"):
        await OnDemandRepository(Mock(), AsyncMock(), deferred=True).before_tool_execute(
            ctx, call=Mock(), tool_def=ToolDefinition(name="desktop_action"), args={},
        )
    ctx.workspace.working_dir.assert_not_awaited()


async def test_repository_selection_preserves_identity_and_blocks_new_kubernetes_imports():
    runtime = SimpleNamespace(
        settings=SimpleNamespace(sandbox_provider="kubernetes"),
        product_identity=SimpleNamespace(schema="source", authorize_admission=AsyncMock()),
    )
    workspace = ProductWorkspaceCompatibility(runtime)
    selection = {"repo_url": "https://github.com/example/project", "environment_id": "none"}
    context = await workspace._selection(None, selection, require_ready=True)
    assert context["repo_url"] == selection["repo_url"]
    blockers = await workspace.automation_blockers(None, "owner", selection)
    with pytest.raises(HTTPException) as refused:
        await workspace.capture_workspace_context(None, SimpleNamespace(grant=None), selection)
    assert refused.value.status_code == 409
    assert blockers == [refused.value.detail]


async def test_import_refusal_precedes_capturing_or_draining_the_source(runtime, active):
    _, _, prepared = active
    transport = Mock()
    restorer = ProductWorkspaceRestorer(runtime, transport=transport)
    prepared.workspace = REFERENCE
    with pytest.raises(ApplicationError) as denied:
        await restorer.prepare(prepared, {"repo_url": "https://repository.invalid/project"})
    assert denied.value.details == ("workspace_import_unsupported",)
    assert transport.mock_calls == []


async def test_maintenance_never_marks_a_live_volume_as_preserved(runtime, active, monkeypatch):
    session, turn, prepared = active
    await runtime.database.save_workspace_reference(prepared.scope, REFERENCE)
    service = ProductDrainService(
        sessions=lambda: runtime.database.sessions, authority=lambda: None, native_schema="", task_queue="local",
    )
    runtime.product_drain = service
    generation = 2
    async with runtime.database.sessions.begin() as db:
        db.add(ProductDrainState(id=1, generation=generation, paused=True))
    graph = AgentCheckpoint(
        prepared=prepared, conversation="{}", workspace=REFERENCE, run_step=0, output_retries_used=0,
    )
    saved_graph = AsyncMock()
    monkeypatch.setattr("moyai.maintenance_workspace.save_history_checkpoint", saved_graph)
    decision = await service.checkpoint(turn.id, workflow_id=turn.workflow_id, workflow_run_id="run", boundary="before_prepare")
    assert decision.paused
    with pytest.raises(ApplicationError) as denied:
        await preserve(runtime, graph, workflow_id=turn.workflow_id, workflow_run_id="run", boundary="settled")
    assert wait_reason(denied.value) == "workspace_snapshot_unsupported"
    async with runtime.database.sessions.begin() as db:
        assert (await db.get(ProductDrainReceipt, (turn.id, generation))).phase == "blocked"
        assert (await db.get(Session, session.id)).snapshot is None
        (await db.get(ProductDrainState, 1)).paused = False
    result = await preserve(runtime, graph, workflow_id=turn.workflow_id, workflow_run_id="run", boundary="settled")
    assert result.reference is None
    saved_graph.assert_not_awaited()


async def test_standalone_http_coding_session_reuses_kubernetes_after_worker_replacement(runtime, actor, monkeypatch):
    """Exercise the worker and public session routes with a local command transport."""
    import asyncio

    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.workspaces import LocalWorkspaceBackend
    from temporalio.testing import WorkflowEnvironment
    from test_application_boundaries import sign_in
    from test_bundled_skills_startup import scripted_model

    from moyai.agent import make_agent
    from moyai.kubernetes_workspace import KubernetesBackend
    from moyai.storage import encrypted_converter
    from moyai.worker import make_worker
    from moyai.workflow import TurnWorkflow

    for key in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    runtime.settings.sandbox_provider = "kubernetes"
    runtime.settings.sandbox_installation_id = "session-test"
    root = runtime.settings.data_dir.parent / "computer"
    local = LocalWorkspaceBackend(root)
    creations = []
    attachments = []

    async def working_dir(backend):
        if backend.ref is None:
            backend._ref = WorkspaceRef(provider="kubernetes", id=backend.name + ".00000000-0000-4000-8000-000000000001")
            creations.append(backend.ref)
        else:
            attachments.append(backend.ref)
        return await local.working_dir()

    async def run(backend, command, **options):
        assert backend.ref in creations
        return await local.run(command, **options)

    monkeypatch.setattr(KubernetesBackend, "working_dir", working_dir)
    monkeypatch.setattr(KubernetesBackend, "run", run)
    forbidden = AsyncMock(side_effect=AssertionError("Unexpected cloud workspace dispatch"))
    monkeypatch.setattr("modal.Sandbox.create.aio", forbidden)
    monkeypatch.setattr("modal.Sandbox.from_id.aio", forbidden)
    monkeypatch.setattr("moyai.lambda_control.LambdaControl.create", forbidden)

    def respond(messages, info):
        reading = "Read the volume" in str(messages)
        identity = "read-volume" if reading else "write-volume"
        returns = [part for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart) and part.tool_call_id == identity]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("shell", {
                "command": "cat proof.txt" if reading else "printf written-once >> proof.txt",
            }, tool_call_id=identity)])
        if reading:
            assert "written-once" in str(returns[-1].content)
        return ModelResponse(parts=[TextPart("Verified the volume.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, skills_directory="skills",
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with (
        await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter) as environment,
        sign_in(runtime) as client,
    ):
        runtime.client = environment.client
        created = await client.post("/api/sessions", json={})
        created.raise_for_status()
        session_id = created.json()["id"]
        for request_key, prompt in (("write", "Write the volume"), ("read", "Read the volume")):
            async with make_worker(runtime):
                submitted = await client.post(f"/api/sessions/{session_id}/messages", json={
                    "request_key": request_key, "prompt": prompt,
                })
                submitted.raise_for_status()
                turn = await runtime.database.claim_next()
                assert turn.id == submitted.json()["id"]
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 60))["status"] == "completed"
        assert len(creations) == 1
        assert attachments and {ref.id for ref in attachments} == {creations[0].id}
        assert (root / "proof.txt").read_text() == "written-once"
        session = await runtime.database.session(actor, session_id)
        assert json.loads(session.workspace) == {"provider": "kubernetes", "id": creations[0].id}
        forbidden.assert_not_awaited()
