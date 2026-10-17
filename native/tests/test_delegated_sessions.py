"""Session admission, isolation and immutable handoff; live proof is separate."""

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from sqlalchemy import select

from moyai.contracts import NewMessage
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import Message, User, new_id


async def finish(runtime, prepared, output):
    assert await runtime.database.save_result(
        prepared.scope, conversation=ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id,
        )).decode(), output=output, workspace=None, usage={},
    )
    await runtime.database.settle(prepared.scope.turn_id)


async def test_three_delegates_are_real_private_sessions_and_retries_are_idempotent(runtime, actor, active):
    parent, parent_turn, prepared = active
    service = DelegationService(runtime.database)
    children = await asyncio.gather(*(
        service.start(prepared.scope, f"call-{i}", f"Investigate item {i}") for i in range(3)
    ))
    assert len({child["session_id"] for child in children}) == 3
    assert len({child["name"] for child in children}) == 3
    assert await service.start(prepared.scope, "call-0", "Investigate item 0") == children[0]
    with pytest.raises(ValueError):
        await service.start(prepared.scope, "call-0", "A different task")
    claimed = await runtime.database.claim_batch(10)
    assert {turn.session_id for turn in claimed} == {child["session_id"] for child in children}
    for turn in claimed:
        child = await runtime.database.session(actor, turn.session_id)
        assert child.visibility == "private"
        context = await runtime.database.prepare(turn.id)
        assert context.scope.delegation_depth == 1 and context.scope.delegation_generated
        assert context.scope.conversation_id != prepared.scope.conversation_id
        assert context.conversation is None
        async with runtime.database.sessions() as db:
            link = await db.get(DelegatedSession, child.id)
            assert link.parent_session_id == parent.id and link.parent_turn_id == parent_turn.id
            message = await db.scalar(select(Message).where(Message.turn_id == turn.id))
            assert message.role == "system" and message.author_id is None


async def test_direct_followups_and_steering_target_only_the_child(runtime, actor, active):
    parent, parent_turn, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "one", "Original assignment")
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    steer = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Use the corrected approach", request_key="correction", mode="asap",
    ))
    assert steer.status == "steering" and steer.parent_turn == initial.id
    assert steer.parent_turn != parent_turn.id
    assert (await runtime.database.prepare_steering(initial.id, steer.id)).prompt == "Use the corrected approach"
    await runtime.database.steering_receipt(steer.id, "accepted-correction")
    await finish(runtime, context, "Corrected result")
    assert (await runtime.database.session(actor, parent.id)).active_turn == parent_turn.id
    follow = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Explain the result", request_key="followup",
    ))
    await runtime.database.claim_next()
    next_context = await runtime.database.prepare(follow.id)
    assert next_context.conversation is not None
    assert not next_context.scope.delegation_generated
    assert next_context.scope.conversation_id == context.scope.conversation_id


async def test_private_followups_never_replace_the_original_parent_handoff(runtime, actor, active):
    _, _, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "one", "Compute the result")
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    assert await service.report(prepared.scope, child["session_id"]) is None
    follow = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Include units", request_key="follow",
    ))
    await finish(runtime, context, "42")
    result = await service.report(prepared.scope, child["session_id"])
    assert result["output"] == "42" and result["status"] == "completed"
    await runtime.database.claim_next()
    follow_context = await runtime.database.prepare(follow.id)
    await finish(runtime, follow_context, "42 metres")
    assert await service.report(prepared.scope, child["session_id"]) == result
    later = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Change to feet", request_key="later",
    ))
    await runtime.database.claim_next()
    await finish(runtime, await runtime.database.prepare(later.id), "138 feet")
    assert await service.report(prepared.scope, child["session_id"]) == result


async def test_requester_isolation_stop_and_depth_limit(runtime, actor, active):
    _, _, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "one", "Child work")
    other = User(id=new_id(), username="other", display_name="Other", password="unused", role="admin")
    async with runtime.database.sessions.begin() as db:
        db.add(other)
    with pytest.raises(HTTPException) as error:
        await runtime.database.session(other, child["session_id"])
    assert error.value.status_code == 404
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    grandchild = await service.start(context.scope, "two", "Grandchild work")
    grandturn = await runtime.database.claim_next()
    grandcontext = await runtime.database.prepare(grandturn.id)
    with pytest.raises(PermissionError, match="another level"):
        await service.start(grandcontext.scope, "three", "Too deep")
    await runtime.database.stop(actor, child["session_id"])
    await runtime.database.stopped(initial.id, None)
    result = await service.report(prepared.scope, child["session_id"])
    assert result["status"] == "stopped"
    assert (await runtime.database.session(actor, grandchild["session_id"])).active_turn == grandturn.id


async def test_stopped_parent_cannot_create_more_children(runtime, actor, active):
    parent, _, prepared = active
    await runtime.database.stop(actor, parent.id)
    with pytest.raises(PermissionError):
        await DelegationService(runtime.database).start(prepared.scope, "one", "No new work")


async def test_report_cannot_read_another_parents_child(runtime, active):
    _, _, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "one", "Work")
    async with runtime.database.sessions.begin() as db:
        link = await db.get(DelegatedSession, child["session_id"])
        link.actor_id = prepared.scope.actor_id
    with pytest.raises(PermissionError):
        await service.report(replace(prepared.scope, actor_id="not-the-requester"), child["session_id"])


async def test_workspace_opens_child_as_session_and_exposes_parent_link(runtime, active):
    from test_workspace_overview import sign_in

    parent, _, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "one", "Independent task")
    async with sign_in(runtime) as client:
        overview = (await client.get(f"/api/sessions/{parent.id}/workspace")).json()
        assert overview["subagents"][0]["session_id"] == child["session_id"]
        response = await client.get(f"/api/sessions/{child['session_id']}")
        assert response.status_code == 200
        assert response.json()["session"]["parent_session_id"] == parent.id
        transcript = await client.get(f"/api/sessions/{child['session_id']}/messages")
        assert "Independent task" in json.dumps(transcript.json())


async def test_saved_child_artifact_is_previewable_from_parent_but_uploads_are_not(runtime, actor, active):
    from test_file_preview import saved_file

    from moyai.file_preview import FilePreviewService

    parent, _, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "one", "Write an output")
    session = await runtime.database.session(actor, child["session_id"])
    _, artifact, _ = await saved_file(runtime, actor, session=session)
    _, upload, _ = await saved_file(runtime, actor, session=session, kind="upload")
    preview = FilePreviewService(runtime)
    assert (await preview.record(actor, parent.id, "artifact", artifact.id)).id == artifact.id
    with pytest.raises(HTTPException):
        await preview.record(actor, parent.id, "upload", upload.id)


async def test_question_continuations_keep_their_original_input_provenance(runtime, actor, active):
    _, _, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "one", "Ask for a color")

    async def pause(context):
        assert await runtime.database.save_result(
            context.scope, conversation=ConversationTypeAdapter.dump_json(Conversation(
                conversation_id=context.scope.conversation_id,
            )).decode(), output="", workspace=None, usage={}, deferred=True,
        )

    initial = await runtime.database.claim_next()
    await pause(await runtime.database.prepare(initial.id))
    resumed = await runtime.database.resume(actor, child["session_id"], initial.id, "answer", {"calls": {}})
    await runtime.database.claim_next()
    context = await runtime.database.prepare(resumed.id)
    assert context.scope.delegation_generated
    assert await service.report(prepared.scope, child["session_id"]) is None
    await finish(runtime, context, "Original assignment completed with the answer.")
    report = await service.report(prepared.scope, child["session_id"])
    assert report["status"] == "completed"
    assert report["output"] == "Original assignment completed with the answer."

    human = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Ask me a new question", request_key="human-question",
    ))
    await runtime.database.claim_next()
    await pause(await runtime.database.prepare(human.id))
    answered = await runtime.database.resume(actor, child["session_id"], human.id, "human-answer", {"calls": {}})
    await runtime.database.claim_next()
    human_context = await runtime.database.prepare(answered.id)
    assert not human_context.scope.delegation_generated
    assert await service.report(prepared.scope, child["session_id"]) == report
