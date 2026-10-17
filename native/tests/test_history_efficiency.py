"""Lossless encoding and public-stream efficiency, independently of LLM quality."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import zlib
from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.live_events import PUBLIC_STREAM_VERSION, PUBLIC_TOPIC, root_events
from moyai.presentation import public_event
from moyai.storage import COMPRESSED_INLINE, MAX_COMPRESSED_INLINE_BYTES, EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


async def test_inline_compression_is_lossless_encrypted_and_does_not_add_object_io(runtime, monkeypatch):
    async def no_storage(*_):
        raise AssertionError("Inline compression must not create an object-store request.")

    monkeypatch.setattr(runtime.objects, "put", no_storage)
    original = Payload(metadata={"encoding": b"json/plain"}, data=json.dumps({
        "role": "tool", "output": "known-secret-test-value\n" * 500,
    }).encode())
    legacy = EncryptedPayloadCodec(runtime.security, runtime.objects)
    writer = EncryptedPayloadCodec(runtime.security, runtime.objects, compress=True)
    old, = await legacy.encode([original])
    new, = await writer.encode([original])
    assert new.metadata["encoding"] == COMPRESSED_INLINE
    assert b"known-secret-test-value" not in new.data
    assert new.ByteSize() < old.ByteSize() / 4
    # Readers support the new format even while new writes remain disabled.
    assert await legacy.decode([new, old]) == [original, original]


async def test_compression_keeps_large_payloads_offloaded_and_high_entropy_inline(runtime):
    writer = EncryptedPayloadCodec(runtime.security, runtime.objects, compress=True)
    values = [
        Payload(data=b"tiny"),
        Payload(data=os.urandom(8192)),
        Payload(data=b"compressible" * 10000),
    ]
    encoded = await writer.encode(values)
    assert [item.metadata["encoding"] for item in encoded] == [
        b"moyai/encrypted-inline-v1", b"moyai/encrypted-inline-v1", b"moyai/encrypted-reference-v1",
    ]
    assert await writer.decode(encoded) == values


@pytest.mark.parametrize("body", [
    b"", b"\0\0",
    struct.pack(">I", MAX_COMPRESSED_INLINE_BYTES + 1) + zlib.compress(b"x"),
    struct.pack(">I", 2) + zlib.compress(b"x" * 100000),
    struct.pack(">I", 2) + zlib.compress(b"xy")[:-1],
    struct.pack(">I", 2) + zlib.compress(b"xy") + b"unexpected-trailing-data",
])
async def test_compressed_payload_rejects_expansion_and_malformed_frames(runtime, body):
    codec = EncryptedPayloadCodec(runtime.security, runtime.objects)
    with pytest.raises(ValueError, match="Invalid compressed Temporal payload"):
        await codec.decode([Payload(
            metadata={"encoding": COMPRESSED_INLINE}, data=runtime.security.seal(body).encode(),
        )])


async def test_model_projection_discards_only_events_the_ui_already_hides():
    events = [
        PartStartEvent(index=0, part=ThinkingPart("private reasoning")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="not public")),
        PartStartEvent(index=1, part=TextPart("Hello")),
        PartDeltaEvent(index=1, delta=TextPartDelta(content_delta=" world")),
        PartEndEvent(index=1, part=TextPart("Hello world")),
        PartStartEvent(index=2, part=ToolCallPart("shell", {}, tool_call_id="call")),
        PartDeltaEvent(index=2, delta=ToolCallPartDelta(args_delta='{"command":"example"}')),
        FunctionToolCallEvent(ToolCallPart("shell", {}, tool_call_id="call")),
        FunctionToolResultEvent(ToolReturnPart("shell", "done", tool_call_id="call")),
    ]

    async def source():
        for event in events:
            yield event

    async def collect(enabled, run_id="turn"):
        ctx = SimpleNamespace(
            run_id=run_id, run_step=1,
            deps=RunScope("session", "turn", "actor", "test", workflow_public_events=enabled),
        )
        return [event async for event in root_events(ctx, source())]

    old, new = await collect(False), await collect(True)
    assert len(new) < len(old)
    assert [value for event in old if (value := public_event(event))] == [
        value for event in new if (value := public_event(event))
    ]
    assert await collect(True, "child-private-run") == []


@pytest.mark.parametrize("runtime", ["history"], indirect=True)
async def test_local_publication_reduces_history_and_preserves_visible_stream_and_replay(
    runtime, actor, monkeypatch,
):
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    original_patched = workflow.patched
    results = {}
    histories = {}
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        for enabled in (False, True):
            monkeypatch.setattr(workflow, "patched", lambda version, enabled=enabled: (
                False if version == PUBLIC_STREAM_VERSION and not enabled else original_patched(version)
            ))
            session = await runtime.database.create_session(actor, NewSession(visibility="private"))
            turn = await runtime.database.submit(actor, session.id, NewMessage(
                prompt="Perform the task.", request_key=f"projection-{enabled}",
            ))
            await runtime.database.claim_next()
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                seen = []

                async def observe(handle, seen):
                    async with runtime.durability.stream_agent_events(
                        runtime.client, handle, topic=PUBLIC_TOPIC,
                    ) as stream:
                        async for event in stream:
                            if value := public_event(event):
                                # The sessions have different root IDs.
                                # Shell results contain process IDs and private
                                # temporary paths. Fixed-result equivalence is
                                # covered separately by the projection test.
                                seen.append({
                                    k: v for k, v in value.items()
                                    if k != "run_id" and not (value["type"] == "tool_result" and k == "content")
                                })

                observer = asyncio.create_task(observe(handle, seen))
                assert (await asyncio.wait_for(handle.result(), 60))["status"] == "completed"
                await asyncio.wait_for(observer, 10)
                history = await handle.fetch_history()
                histories[enabled] = history
                event_activities = sum(
                    event.activity_task_scheduled_event_attributes.activity_type.name.endswith("__event_stream_handler")
                    for event in history.events
                )
                results[enabled] = seen, len(history.events), event_activities
        monkeypatch.setattr(workflow, "patched", original_patched)
        for history in histories.values():
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()],
                interceptors=[ActivityCapacity()], data_converter=converter,
            ).replay_workflow(history)
    old, new = results[False], results[True]
    assert new[0] == old[0]
    assert old[2] > 0 and new[2] == 0
    # Poll/update batching varies by runner. The deterministic guarantee above
    # is removal of the per-event activities; report the measured event delta.
    assert new[1] < old[1]
    print({"legacy_events": old[1], "direct_events": new[1], "removed_event_activities": old[2]})
