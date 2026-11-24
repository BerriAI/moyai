from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from moyai.contracts import NewMessage, NewSession
from moyai.models import Session, User
from moyai.responsibilities import Responsibilities
from moyai.responsibility_contracts import NewResponsibility, ResponsibilityControl
from moyai.responsibility_models import Responsibility, ResponsibilityAssignment


async def enroll(runtime, actor, source=None, **values):
    source = source or await runtime.database.create_session(actor, NewSession(visibility="private"))
    row = await runtime.database.responsibilities.create(actor, NewResponsibility(
        source_session_id=source.id, request_key=str(uuid4()), model="test",
        objective="Tell me when launch readiness changes.", **values,
    ))
    return source, row


async def source_answer(runtime, actor, source, output="The release is now ready.", settle=True):
    turn = await runtime.admission.submit(actor, source.id, NewMessage(prompt="Check readiness.", request_key=str(uuid4())))
    await runtime.database.claim_batch(20)
    prepared = await runtime.admission.prepare(turn.id)
    await runtime.database.save_result(prepared.scope, conversation='{"messages":[]}', output=output, workspace=None, usage={})
    if settle:
        await runtime.database.settle(turn.id)
    return turn


async def tick(runtime, identity):
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Responsibility).where(Responsibility.id == identity).values(next_check_at="2000"))
    await runtime.database.responsibilities.tick(identity)


async def assignment(runtime, identity):
    async with runtime.database.sessions() as db:
        row = await db.get(Responsibility, identity)
        return await db.get(ResponsibilityAssignment, row.active_turn_id) if row.active_turn_id else None


async def worker(runtime, identity):
    binding = await assignment(runtime, identity)
    assert binding
    await runtime.database.claim_batch(20)
    prepared = await runtime.admission.prepare(binding.turn_id)
    evidence = json.loads(runtime.security.unseal(binding.evidence))
    decision = json.dumps({
        "action": "notify", "summary": "Launch readiness changed. Review the release.",
        "evidence": [{"message_id": evidence[0]["message_id"], "quote": evidence[0]["text"]}],
    })
    return binding, prepared, decision


async def finish(runtime, prepared, decision):
    accepted = await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output=decision, workspace=None, usage={"input_tokens": 50},
    )
    if accepted:
        await runtime.database.settle(prepared.scope.turn_id)
    return accepted


async def test_silent_baseline_and_inflight_completion_are_not_lost(runtime, actor):
    source = await runtime.database.create_session(actor, NewSession(visibility="private"))
    old = await source_answer(runtime, actor, source, settle=False)
    _, responsibility = await enroll(runtime, actor, source)
    await tick(runtime, responsibility.id)
    assert await assignment(runtime, responsibility.id) is None
    # Enrollment during "saving" must not skip the later committed completion.
    await runtime.database.settle(old.id)
    await tick(runtime, responsibility.id)
    assert await assignment(runtime, responsibility.id)


async def test_idle_scans_use_no_evaluations_and_no_worker_turns(runtime, actor):
    source = await runtime.database.create_session(actor, NewSession(visibility="private"))
    await source_answer(runtime, actor, source)
    _, responsibility = await enroll(runtime, actor, source)
    for _ in range(100):
        await tick(runtime, responsibility.id)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ResponsibilityAssignment)) == 0
        assert (await db.get(Responsibility, responsibility.id)).evaluations == 0


async def test_restart_concurrent_wakes_and_inbox_are_idempotent(runtime, actor):
    source, responsibility = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Responsibility).values(next_check_at="2000"))
    await asyncio.gather(*(runtime.database.responsibilities.tick(responsibility.id) for _ in range(3)))
    binding, prepared, decision = await worker(runtime, responsibility.id)
    assert prepared.scope.grants == [] and prepared.conversation is None and prepared.workspace is None
    assert prepared.scope.responsibility_id == responsibility.id
    runtime.database.responsibilities = Responsibilities(runtime.database)
    assert await finish(runtime, prepared, decision)
    await tick(runtime, responsibility.id)
    await tick(runtime, responsibility.id)
    inbox = await runtime.database.responsibilities.inbox(actor)
    assert len(inbox) == 1 and inbox[0].turn_id == binding.turn_id
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ResponsibilityAssignment)) == 1
        assert (await db.get(Responsibility, responsibility.id)).evaluations == 1


@pytest.mark.parametrize("barrier", ["before_prepare", "during_model", "after_save"])
async def test_pause_fences_each_publication_barrier(runtime, actor, barrier):
    source, responsibility = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, responsibility.id)
    if barrier != "before_prepare":
        binding, prepared, decision = await worker(runtime, responsibility.id)
        if barrier == "after_save":
            await finish(runtime, prepared, decision)
    else:
        binding = await assignment(runtime, responsibility.id)
        await runtime.database.claim_batch(20)
    await runtime.database.responsibilities.control(actor, responsibility.id, ResponsibilityControl(
        generation=1, action="pause",
    ))
    if barrier == "before_prepare":
        with pytest.raises((PermissionError, HTTPException)):
            await runtime.admission.prepare(binding.turn_id)
    elif barrier == "during_model":
        with pytest.raises((PermissionError, HTTPException)):
            await runtime.database.authorize_scope(prepared.scope)
        assert not await finish(runtime, prepared, decision)
    await tick(runtime, responsibility.id)
    assert await runtime.database.responsibilities.inbox(actor) == []


@pytest.mark.parametrize("change", ["deleted", "archived", "visibility", "auth_version"])
async def test_revocation_blocks_execution_inbox_and_worker_chat(runtime, actor, change):
    source, responsibility = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, responsibility.id)
    _, prepared, decision = await worker(runtime, responsibility.id)
    await finish(runtime, prepared, decision)
    await tick(runtime, responsibility.id)
    async with runtime.database.sessions.begin() as db:
        if change == "auth_version":
            await db.execute(update(User).where(User.id == actor.id).values(auth_version=2))
        else:
            await db.execute(update(Session).where(Session.id == source.id).values(
                **{change: "shared" if change == "visibility" else True},
            ))
    assert await runtime.database.responsibilities.inbox(actor) == []
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.database.session(actor, prepared.scope.session_id)


async def test_independent_sources_budget_and_invalid_citations(runtime, actor):
    one, first = await enroll(runtime, actor, max_evaluations=1)
    two, second = await enroll(runtime, actor)
    await source_answer(runtime, actor, one)
    await source_answer(runtime, actor, two)
    await tick(runtime, first.id)
    await tick(runtime, second.id)
    _, waiting, _ = await worker(runtime, first.id)
    _, finished, decision = await worker(runtime, second.id)
    await finish(runtime, finished, decision)
    await tick(runtime, second.id)
    assert len(await runtime.database.responsibilities.inbox(actor)) == 1
    # A still-running independent worker does not block that result.
    await finish(runtime, waiting, json.dumps({
        "action": "notify", "summary": "Forged", "evidence": [{"message_id": 999999, "quote": "invented"}],
    }))
    await tick(runtime, first.id)
    assert len(await runtime.database.responsibilities.inbox(actor)) == 1
    with pytest.raises(HTTPException, match="budget is exhausted"):
        await runtime.database.responsibilities.control(actor, first.id, ResponsibilityControl(action="resume", generation=1))


async def test_worker_is_not_a_new_source_or_human_authority(runtime, actor):
    source, responsibility = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, responsibility.id)
    _, prepared, _ = await worker(runtime, responsibility.id)
    async with runtime.database.sessions() as db:
        assert await runtime.database.human_origin(db, prepared.scope) is None
        worker_session = await db.get(Session, prepared.scope.session_id)
    with pytest.raises(HTTPException):
        await enroll(runtime, actor, worker_session)
    with pytest.raises(HTTPException, match="immutable responsibility review"):
        await runtime.admission.submit(actor, worker_session.id, NewMessage(prompt="Override", request_key="attack"))


async def test_other_owner_cannot_control_or_read_finding(runtime, actor):
    _, responsibility = await enroll(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        other = User(username="other", display_name="Other", password="unused", role="admin")
        db.add(other)
        await db.flush()
    assert await runtime.database.responsibilities.list(other) == []
    with pytest.raises(HTTPException):
        await runtime.database.responsibilities.control(other, responsibility.id, ResponsibilityControl(action="pause", generation=1))


async def test_wait_advances_without_notification_and_stops_at_budget(runtime, actor):
    source, responsibility = await enroll(runtime, actor, max_evaluations=1)
    await source_answer(runtime, actor, source, "No material change.")
    await tick(runtime, responsibility.id)
    _, prepared, _ = await worker(runtime, responsibility.id)
    await finish(runtime, prepared, json.dumps({"action": "wait", "summary": "No material change.", "evidence": []}))
    await tick(runtime, responsibility.id)
    assert await runtime.database.responsibilities.inbox(actor) == []
    view = (await runtime.database.responsibilities.list(actor))[0]
    assert view.watermark == 1 and view.status == "budget_limited" and view.evaluations == 1


async def test_identical_evidence_is_not_notified_again(runtime, actor):
    source, responsibility = await enroll(runtime, actor)
    for _ in range(2):
        await source_answer(runtime, actor, source)
        await tick(runtime, responsibility.id)
        _, prepared, decision = await worker(runtime, responsibility.id)
        await finish(runtime, prepared, decision)
        await tick(runtime, responsibility.id)
    assert len(await runtime.database.responsibilities.inbox(actor)) == 1
    assert (await runtime.database.responsibilities.list(actor))[0].evaluations == 2
