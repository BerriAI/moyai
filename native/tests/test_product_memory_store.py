"""Native Harness tools against canonical memory; deterministic supplementary tests."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
import test_product_memory as memory_fixtures
from fastapi import HTTPException
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import StepPersistence
from pydantic_ai_harness.memory import InMemoryStore, MemoryOperation, MemoryOperationConflictError
from sqlalchemy import select, text, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.agent import Authority, HarnessActivityContext
from moyai.contracts import NewMessage, NewSession
from moyai.models import Message, User
from moyai.presentation import saved_activity
from moyai.product_memory import Preferences
from moyai.product_memory_models import ProductMemoryOperation
from moyai.product_memory_store import CHUNK_CHARS, ProductMemoryStore, memory_capability
from moyai.runtime import Runtime
from moyai.storage import encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = memory_fixtures.product_database
admitted_product = memory_fixtures.admitted_product
memory_product = memory_fixtures.memory_product


async def memory_execution(product, *, private=False, surface="web"):
    session = await product.database.create_session(
        product.actor, NewSession(visibility="private" if private else "shared"),
    )
    if surface != "web":
        from moyai.models import Session
        async with product.database.sessions.begin() as db:
            (await db.get(Session, session.id)).surface = surface
    turn = await product.database.submit(product.actor, session.id, NewMessage(
        prompt="Please remember: Prefer pytest for focused backend tests.", request_key="memory-input",
    ))
    assert (await product.database.claim_next()).id == turn.id
    prepared = await product.database.prepare(turn.id)
    async with product.database.sessions() as db:
        message_id = await db.scalar(select(Message.id).where(Message.turn_id == turn.id))
    return session, turn, prepared.scope, message_id


def captured_note(message_id, **changes):
    return json.dumps({
        "key": "backend-testing", "title": "Backend testing", "content": "Prefer pytest for focused backend tests.",
        "kind": "preference", "repo_url": "", "revision": 0,
        "source_message_id": message_id, "source_quote": "Prefer pytest for focused backend tests.",
        **changes,
    })


async def test_real_harness_recall_and_capture_reuse_canonical_rows_and_preserve_original_scope(memory_product):
    product = memory_product
    await product.memory.save(product.principal.grant, memory_fixtures.note())
    _, turn, scope, message_id = await memory_execution(product)
    seen = []

    def respond(messages, info):
        seen.append(messages)
        assert any("Use the existing formatter." in str(message) for message in messages)
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_memory", {
                "file": "personal-backend-testing.md", "content": captured_note(message_id),
            }, tool_call_id="native-memory-capture")])
        return ModelResponse(parts=[TextPart("Captured with the native memory tool.")])

    agent = Agent(
        FunctionModel(respond), deps_type=type(scope),
        capabilities=[memory_capability(product.memory)],
    )
    result = await agent.run("Carry out this authenticated memory request.", deps=scope, run_id=turn.id)
    assert result.output == "Captured with the native memory tool."
    assert len(seen) == 2
    activity = saved_activity(result.all_messages(), turn.id)
    assert activity and all(row.get("arguments") is None and row.get("content") is None for row in activity)
    view = await product.memory.listing(product.principal.grant)
    saved = next(note for note in view["memories"] if note["key"] == "backend-testing")
    assert saved["source"]["core"] == "pydantic-v1"
    assert saved["source"]["native_message_id"] == message_id
    assert saved["source"]["native_turn_id"] == turn.id
    assert saved["source"]["engine"] == "native"
    assert "run_id" not in saved["source"] and "message_id" not in saved["source"]
    assert view["runtime"]["recall"] and view["runtime"]["auto_save"]
    assert not view["runtime"]["background_review"]
    async with product.database.sessions() as db:
        receipt = await db.scalar(select(ProductMemoryOperation))
        assert receipt.authority_version == scope.product_grant.authority_version
    # Original manual source remains intact and did not become an agent observation.
    manual = next(note for note in view["memories"] if note["key"] == "editor-preference")
    assert manual["source"] == {"type": "manual"}


async def test_native_operation_replay_and_changed_arguments_do_not_duplicate_note(memory_product):
    product = memory_product
    _, turn, scope, message_id = await memory_execution(product)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    path = store.prefix + "personal-backend-testing.md"
    operation = MemoryOperation("same-activity-tool-call", "a" * 64)
    first = await store.write(path, captured_note(message_id), expected_version=None, operation=operation)
    again = await store.write(path, captured_note(message_id), expected_version=None, operation=operation)
    assert not first.replayed and again.replayed and first.version == again.version
    with pytest.raises(MemoryOperationConflictError):
        await store.get_operation(MemoryOperation(operation.id, "b" * 64))
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 1
        assert await db.scalar(text("SELECT count(*) FROM moyai.memory_operations")) == 1


async def test_native_manual_mode_failure_is_not_presented_as_success(memory_product):
    product = memory_product
    await product.memory.preferences(product.principal.grant, Preferences(enabled=True, auto_save=False, revision=0))
    _, turn, scope, message_id = await memory_execution(product)

    def respond(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("write_memory", {
                "file": "personal-backend-testing.md", "content": captured_note(message_id),
            }, tool_call_id="manual-mode-write")])
        assert returns[-1].outcome == "failed"
        return ModelResponse(parts=[TextPart("The note was not saved.")])

    agent = Agent(FunctionModel(respond), deps_type=type(scope), capabilities=[memory_capability(product.memory)])
    result = await agent.run("Try the requested capture under the current preferences.", deps=scope, run_id=turn.id)
    activity = saved_activity(result.all_messages(), turn.id)
    rejected = next(item for item in activity if item["type"] == "tool_result")
    assert rejected["failed"] is True
    assert rejected["content"] is None
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 0


@pytest.mark.parametrize("forbidden", ["foreign_message", "missing_quote", "root_file", "other_namespace", "child", "automation"])
async def test_untrusted_evidence_paths_and_nonhuman_mutations_fail_closed(memory_product, forbidden):
    product = memory_product
    _, turn, scope, message_id = await memory_execution(
        product, surface="automation" if forbidden == "automation" else "web",
    )
    store = ProductMemoryStore(product.memory, scope, "temporal:child" if forbidden == "child" else turn.id)
    path = store.prefix + "personal-backend-testing.md"
    changes = {}
    if forbidden == "foreign_message":
        changes["source_message_id"] = message_id + 500
    elif forbidden == "missing_quote":
        changes["source_quote"] = "Not a current requester statement."
    elif forbidden == "root_file":
        path = store.prefix + "MEMORY.md"
    elif forbidden == "other_namespace":
        path = "shared/somebody-else/main/personal-backend-testing.md"
    with pytest.raises(ToolFailed):
        await store.write(path, captured_note(message_id, **changes), expected_version=None,
                          operation=MemoryOperation("forbidden", "a" * 64))
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 0


async def test_private_execution_and_disabled_memory_do_not_recall_canonical_body(memory_product):
    product = memory_product
    await product.memory.save(product.principal.grant, memory_fixtures.note())
    session, turn, scope, _ = await memory_execution(product, private=True)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    main = await store.read(store.prefix + "MEMORY.md", max_chars=10000)
    assert "Use the existing formatter." not in main.content
    assert await store.read(store.prefix + "personal-editor-preference.md", max_chars=10000) is None
    await product.database.stop(product.actor, session.id)
    await product.database.stopped(turn.id)
    _, turn, scope, _ = await memory_execution(product)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    await product.memory.preferences(product.principal.grant, Preferences(enabled=False, revision=0))
    assert "Use the existing formatter." not in (await store.read(store.prefix + "MEMORY.md", max_chars=10000)).content


async def test_manual_edit_barrier_and_stop_prevent_late_capture(memory_product):
    product = memory_product
    session, turn, scope, message_id = await memory_execution(product)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    await product.memory.save(product.principal.grant, memory_fixtures.note())
    with pytest.raises(ToolFailed, match="manual memory change"):
        await store.write(store.prefix + "personal-backend-testing.md", captured_note(message_id),
                          expected_version=None, operation=MemoryOperation("after-edit", "a" * 64))
    await product.database.stop(product.actor, session.id)
    with pytest.raises((HTTPException, PermissionError, ToolFailed)):
        await store.write(store.prefix + "personal-backend-testing.md", captured_note(message_id),
                          expected_version=None, operation=MemoryOperation("after-stop", "b" * 64))
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 1


async def test_native_backing_notebook_is_readable_in_exact_namespace_and_never_copied_or_rewritten(memory_product):
    product = memory_product
    _, turn, scope, message_id = await memory_execution(product)
    backend = InMemoryStore({
        scope.memory_namespace + "/main/MEMORY.md": "Existing native notebook marker",
        "shared/another-owner/main/MEMORY.md": "Must not leak",
    })
    store = ProductMemoryStore(product.memory, scope, turn.id, native_store=backend)
    paths = await store.list_paths(store.prefix, limit=100)
    native = next(path for path in paths if path.startswith(store.prefix + "native-"))
    assert (await store.read(native, max_chars=1000)).content == "Existing native notebook marker"
    assert "Must not leak" not in str(await store.read(store.prefix + "MEMORY.md", max_chars=10000))
    with pytest.raises(ToolFailed):
        await store.write(native, captured_note(message_id), expected_version=None,
                          operation=MemoryOperation("native-readonly", "a" * 64))
    assert backend.files[scope.memory_namespace + "/main/MEMORY.md"] == "Existing native notebook marker"
    matches = await store.search(
        store.prefix, "notebook marker", limit=10, max_files=100, max_chars=1000, max_file_chars=1000,
    )
    assert [match.path for match in matches.matches] == [native]
    assert "Existing native notebook marker" in matches.matches[0].snippet
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 0


async def test_verified_repository_automatic_recall_is_scoped_to_exact_turn_but_explicit_reads_are_personal(memory_product):
    product = memory_product
    for key, repository in [
        ("general-rule", ""), ("current-project", "https://github.com/owner/current"),
        ("other-project", "https://github.com/owner/other"),
    ]:
        await product.memory.save(product.principal.grant, memory_fixtures.note(
            key=key, title=key, content=f"Prefer pytest for {key}.", request_id=key, repo_url=repository,
        ))
    session, turn, scope, _ = await memory_execution(product)
    calls = []

    async def repository_scope(session_id, grant, *, turn_id=None):
        calls.append((session_id, grant, turn_id))
        return {
            "repository_id": 42, "repo_url": "https://github.com/Owner/Current.git",
            "source_schema": product.adapter.schema, "context_fingerprint": "f" * 64,
        }

    store = ProductMemoryStore(product.memory, scope, turn.id, repository_scope=repository_scope)
    context = (await store.read(store.prefix + "MEMORY.md", max_chars=10000)).content
    assert "general-rule" in context and "current-project" in context and "other-project" not in context
    explicit = await store.read(store.prefix + "personal-other-project.md", max_chars=10000)
    assert json.loads(explicit.content)["repo_url"] == "https://github.com/owner/other"
    assert calls == [(session.id, scope.product_grant, turn.id)] * 2


@pytest.mark.parametrize("descriptor", [
    {"repo_url": "https://github.com/owner/repo"},
    {"repository_id": True, "repo_url": "https://github.com/owner/repo"},
    {"repository_id": 42, "repo_url": "https://user:secret@github.com/owner/repo"},
    {"repository_id": 42, "repo_url": "https://github.com/owner/repo", "source_schema": "other"},
    {"repository_id": 42, "repo_url": "https://github.com/owner/repo", "context_fingerprint": "unknown"},
])
async def test_unknown_repository_provenance_never_falls_back_to_general_scope(memory_product, descriptor):
    product = memory_product
    _, turn, scope, _ = await memory_execution(product)

    async def repository_scope(*args, **kwargs):
        return {
            "source_schema": product.adapter.schema, "context_fingerprint": "f" * 64, **descriptor,
        }

    store = ProductMemoryStore(product.memory, scope, turn.id, repository_scope=repository_scope)
    with pytest.raises(ToolFailed):
        await store.read(store.prefix + "MEMORY.md", max_chars=10000)


async def test_large_library_keeps_current_input_and_relevant_context_in_native_injection(memory_product):
    product = memory_product
    async with product.memory.writer(product.principal.grant) as (db, owner):
        for index in range(40):
            await product.memory.save_in(db, owner, memory_fixtures.note(
                key=f"general-preference-{index}", request_id=f"general-preference-{index}",
                content="An existing preference. " * 90,
            ), manual=True)
        await product.memory.save_in(db, owner, memory_fixtures.note(
            key="pytest-rule", request_id="pytest-rule", content="Prefer pytest: preserve the important boundary test.",
        ), manual=True)
    _, turn, scope, message_id = await memory_execution(product)

    def respond(messages, info):
        content = str(messages)
        assert f"Current source message IDs: [{message_id}]" in content
        assert "preserve the important boundary test" in content
        return ModelResponse(parts=[TextPart("Verified the current bounded memory context.")])

    agent = Agent(FunctionModel(respond), deps_type=type(scope), capabilities=[memory_capability(product.memory)])
    await agent.run("Check current memory context.", deps=scope, run_id=turn.id)


async def test_long_canonical_note_can_be_read_in_full_and_searched_beyond_first_chunk(memory_product):
    product = memory_product
    content = "既存の文書. " * 12000 + "\nRare concluding boundary marker."
    await product.memory.save(product.principal.grant, memory_fixtures.note(content=content))
    _, turn, scope, _ = await memory_execution(product)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    pieces = []
    next_file = "part-editor-preference-00000.md"
    versions = set()
    while next_file:
        part = await store.read(store.prefix + next_file, max_chars=65536)
        assert not part.truncated
        body = json.loads(part.content)
        pieces.append(body["content"])
        assert body["offset"] == (len(pieces) - 1) * CHUNK_CHARS
        assert body["characters"] == len(content)
        versions.add(part.version)
        next_file = body["next_file"]
    assert "".join(pieces) == content and len(versions) == 1
    matches = await store.search(
        store.prefix, "Rare concluding", limit=10, max_files=100, max_chars=1000, max_file_chars=CHUNK_CHARS,
    )
    assert matches.matches and "Rare concluding" in matches.matches[0].snippet
    assert matches.matches[0].path.startswith(store.prefix + "part-editor-preference-")
    with pytest.raises(ToolFailed):
        await store.delete(matches.matches[0].path, expected_version=next(iter(versions)),
                           operation=MemoryOperation("cannot-forget-part", "a" * 64))


@pytest.mark.parametrize("action", ["read", "context", "list", "search"])
@pytest.mark.parametrize("revoke", ["preference", "authority"])
async def test_native_backing_await_cannot_release_content_after_revocation(memory_product, monkeypatch, action, revoke):
    product = memory_product
    _, turn, scope, _ = await memory_execution(product)
    backend = InMemoryStore({scope.memory_namespace + "/main/MEMORY.md": "Private backing notebook marker"})
    store = ProductMemoryStore(product.memory, scope, turn.id, native_store=backend)
    entered, release = asyncio.Event(), asyncio.Event()
    method = "search" if action == "search" else "list_paths"
    original = getattr(backend, method)

    async def held_result(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(backend, method, held_result)
    if action == "read":
        pending = store.read(store.prefix + store._native_name(store.prefix + "MEMORY.md"), max_chars=1000)
    elif action == "context":
        pending = store.read(store.prefix + "MEMORY.md", max_chars=10000)
    elif action == "list":
        pending = store.list_paths(store.prefix, limit=100)
    else:
        pending = store.search(
            store.prefix, "notebook", limit=10, max_files=100, max_chars=1000, max_file_chars=1000,
        )
    task = asyncio.create_task(pending)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if revoke == "preference":
            await product.memory.preferences(product.principal.grant, Preferences(enabled=False, revision=0))
        else:
            async with product.database.sessions.begin() as db:
                await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
        release.set()
        if revoke == "authority":
            with pytest.raises((HTTPException, PermissionError, ToolFailed)):
                await task
        else:
            result = await task
            assert "Private backing notebook marker" not in str(result)
            assert "native-" not in str(result)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_concurrent_delete_records_one_result_without_resurrection(memory_product):
    product = memory_product
    _, turn, scope, message_id = await memory_execution(product)
    store = ProductMemoryStore(product.memory, scope, turn.id)
    path = store.prefix + "personal-backend-testing.md"
    written = await store.write(path, captured_note(message_id), expected_version=None,
                                operation=MemoryOperation("create-once", "a" * 64))
    results = await asyncio.gather(*[
        store.delete(path, expected_version=written.version, operation=MemoryOperation("delete-once", "b" * 64))
        for _ in range(2)
    ])
    assert sum(result.replayed for result in results) == 1
    assert all(result.existed and result.version is None for result in results)
    assert await store.read(path, max_chars=1000) is None
    # Removing a note does not leak the current user's source contents to another namespace.
    other = ProductMemoryStore(product.memory, replace(scope, actor_id="google:other"), turn.id)
    with pytest.raises((HTTPException, PermissionError, ToolFailed)):
        await other.read(other.prefix + "MEMORY.md", max_chars=1000)


async def test_canonical_capture_in_real_temporal_replays_without_rewriting_memory(memory_product):
    """Real native durable tools/PG store, controlled model; not deployed-provider proof."""
    product = memory_product
    await product.memory.save(product.principal.grant, memory_fixtures.note())
    _, turn, scope, message_id = await memory_execution(product)
    settings = product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    runtime.database.product_identity = product.adapter
    try:
        await runtime.database.start()
        await runtime.stores.start()

        def respond(messages, info):
            assert any("Use the existing formatter." in str(message) for message in messages)
            if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
                return ModelResponse(parts=[ToolCallPart("write_memory", {
                    "file": "personal-backend-testing.md", "content": captured_note(message_id),
                }, tool_call_id="durable-memory")])
            return ModelResponse(parts=[TextPart("Canonical memory captured natively.")])

        async def stream(messages, info):
            for part in respond(messages, info).parts:
                if isinstance(part, TextPart):
                    yield part.content
                elif isinstance(part, ToolCallPart):
                    yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args),
                                           tool_call_id=part.tool_call_id)}

        runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
        runtime.durability = TemporalDurability(
            models=runtime.models, run_context_type=HarnessActivityContext,
        )
        runtime.agent = Agent(
            runtime.models["test"], name="moyai", deps_type=type(scope),
            capabilities=[
                Authority(runtime.database), memory_capability(product.memory, native_store=runtime.stores.memory),
                StepPersistence(runtime.stores.steps, capture_frontier=True), runtime.durability,
            ],
        )
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                execution = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=settings.temporal_queue,
                )
                assert (await asyncio.wait_for(execution.result(), 45))["status"] == "completed"
                history = await execution.fetch_history()
            await Replayer(
                interceptors=[ActivityCapacity()],
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
        view = await product.memory.listing(product.principal.grant)
        captured = next(item for item in view["memories"] if item["key"] == "backend-testing")
        assert captured["revision"] == 1 and captured["source"]["native_turn_id"] == turn.id
        async with product.database.sessions() as db:
            receipts = list(await db.scalars(select(ProductMemoryOperation)))
            assert len(receipts) == 1 and receipts[0].source_revision == 1
    finally:
        await runtime.close()
