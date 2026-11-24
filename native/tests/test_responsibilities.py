from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update

from moyai.contracts import NewMessage, NewSession
from moyai.models import Session, User
from moyai.responsibilities import Responsibilities
from moyai.responsibility_contracts import NewResponsibility, ResponsibilityControl, ResponsibilityEvidence
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
    pending = await runtime.database.responsibilities.history(actor, second.id)
    assert pending[0].validation == "pending" and pending[0].decision is None
    await tick(runtime, second.id)
    validated = await runtime.database.responsibilities.history(actor, second.id)
    assert validated[0].validation == "valid" and validated[0].decision is not None
    assert len(await runtime.database.responsibilities.inbox(actor)) == 1
    # A still-running independent worker does not block that result.
    await finish(runtime, waiting, json.dumps({
        "action": "notify", "summary": "Forged", "evidence": [{"message_id": 999999, "quote": "invented"}],
    }))
    await tick(runtime, first.id)
    rejected = await runtime.database.responsibilities.history(actor, first.id)
    assert rejected[0].validation == "invalid" and rejected[0].decision is None
    from test_application_boundaries import sign_in
    async with sign_in(runtime) as client:
        rejected_json = (await client.get(f"/api/responsibilities/{first.id}/reviews")).json()
        assert rejected_json["items"][0]["validation"] == "invalid"
        assert rejected_json["items"][0]["decision"] is None
        assert "invented" not in json.dumps(rejected_json)
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


def test_whitespace_is_not_source_evidence():
    with pytest.raises(ValidationError):
        ResponsibilityEvidence(message_id=1, quote=" \n\t")


async def test_dedupe_uses_captured_packet_not_model_chosen_excerpt(runtime, actor):
    source, row = await enroll(runtime, actor)
    for answer, quote in [
        ("Release ready. QA approved.", "Release ready."),
        ("Release ready. QA approved.", "QA approved."),
        ("Release ready. Documentation incomplete.", "Release ready."),
    ]:
        await source_answer(runtime, actor, source, answer)
        await tick(runtime, row.id)
        _, prepared, decision = await worker(runtime, row.id)
        parsed = json.loads(decision)
        parsed["evidence"][0]["quote"] = quote
        await finish(runtime, prepared, json.dumps(parsed))
        await tick(runtime, row.id)
    assert len(await runtime.database.responsibilities.inbox(actor)) == 2


async def test_failed_reviews_remain_readable_after_active_pointer_is_cleared(runtime, actor):
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding, _, _ = await worker(runtime, row.id)
    await runtime.database.fail(binding.turn_id, "The gateway could not finish this review.")
    await tick(runtime, row.id)
    assert await assignment(runtime, row.id) is None
    history = await runtime.database.responsibilities.history(actor, row.id)
    assert len(history) == 1 and history[0].status == "failed"
    assert history[0].error == "The gateway could not finish this review."
    await runtime.database.delete_session(actor, source.id)
    with pytest.raises(HTTPException):
        await runtime.database.responsibilities.history(actor, row.id)


async def test_audit_session_has_no_generic_chat_or_export_controls(runtime, actor):
    from moyai.debug_trace import debug_trace

    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding = await assignment(runtime, row.id)
    from moyai.models import Turn
    async with runtime.database.sessions() as db:
        review = await db.get(Turn, binding.turn_id)
        review_id = review.session_id
    operations = [
        runtime.database.steer_queued(actor, binding.turn_id),
        runtime.database.stop(actor, review_id),
        runtime.database.delete_session(actor, review_id),
        runtime.database.restore_session(actor, review_id),
        debug_trace(runtime, actor, review_id),
    ]
    for operation in operations:
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 409
    async with runtime.database.sessions.begin() as db:
        with pytest.raises(HTTPException):
            await runtime.database.automation_session(db, actor.id, review_id, visibility="private")


async def test_http_audit_controls_are_rejected_and_typed_history_survives_failure(runtime, actor):
    from test_application_boundaries import sign_in
    from test_sidebar import member_client

    from moyai.models import Turn

    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding = await assignment(runtime, row.id)
    async with runtime.database.sessions() as db:
        review_id = (await db.get(Turn, binding.turn_id)).session_id
    path = f"/api/sessions/{review_id}"
    async with sign_in(runtime) as client:
        for method, route, body in [
            ("GET", f"{path}/debug-trace", None),
            ("PATCH", path, {"archived": True}),
            ("POST", f"{path}/stop", None),
            ("DELETE", path, None),
            ("POST", f"{path}/links", {"kind": "source", "url": "https://example.com"}),
            ("POST", f"/api/turns/{binding.turn_id}/steer", None),
            ("DELETE", f"/api/turns/{binding.turn_id}", None),
        ]:
            response = await client.request(method, route, **({"json": body} if body else {}))
            assert response.status_code == 409, (method, route, response.text)
        await runtime.database.fail(binding.turn_id, "Temporary model failure.")
        await tick(runtime, row.id)
        result = await client.get(f"/api/responsibilities/{row.id}/reviews")
        assert result.status_code == 200
        assert result.json()["items"][0]["error"] == "Temporary model failure."
        assert result.json()["items"][0]["status"] == "failed"
    async with member_client(runtime) as (other, _):
        assert (await other.get(f"/api/responsibilities/{row.id}/reviews")).status_code == 404
    await runtime.database.delete_session(actor, source.id)
    async with sign_in(runtime) as client:
        assert (await client.get(f"/api/responsibilities/{row.id}/reviews")).status_code == 404


async def test_audit_is_never_a_pull_request_helper_target(runtime, actor):
    from moyai.pull_requests import PullRequestService

    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    _, prepared, _ = await worker(runtime, row.id)
    service = PullRequestService(runtime)
    assert await service.contexts(actor, [prepared.scope.session_id]) == {}
    await runtime.database.delete_session(actor, source.id)
    assert await service.contexts(actor, [prepared.scope.session_id]) == {}


async def test_active_assignment_reconciles_before_long_source_interval(runtime, actor):
    source, row = await enroll(runtime, actor, interval_seconds=3600)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding = await assignment(runtime, row.id)
    waiting = await runtime.database.responsibilities.tick(row.id, generation=row.generation)
    assert waiting.wake_at == binding.expires_at and not waiting.finished
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(ResponsibilityAssignment).where(
            ResponsibilityAssignment.turn_id == binding.turn_id,
        ).values(expires_at="2000"))
    await tick(runtime, row.id)
    blocked = (await runtime.database.responsibilities.list(actor))[0]
    assert blocked.status == "blocked" and blocked.active_turn_id is None


async def test_accepted_result_waits_for_settlement_after_expiry(runtime, actor):
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding, prepared, decision = await worker(runtime, row.id)
    assert await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output=decision, workspace=None, usage={},
    )
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(ResponsibilityAssignment).where(
            ResponsibilityAssignment.turn_id == binding.turn_id,
        ).values(expires_at="2000"))
    await tick(runtime, row.id)
    assert (await assignment(runtime, row.id)).turn_id == binding.turn_id
    assert await runtime.database.responsibilities.inbox(actor) == []
    await runtime.database.settle(binding.turn_id)
    await tick(runtime, row.id)
    assert len(await runtime.database.responsibilities.inbox(actor)) == 1
