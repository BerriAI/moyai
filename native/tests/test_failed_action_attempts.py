"""Failed turns remain usable without asking users to attest tool outcomes."""

import time

import pytest
from test_application_boundaries import sign_in

from moyai.effects import EffectReceipts, claim_browser_dispatch
from moyai.models import ComputerOperation, Effect, Session, Turn, new_id, now
from moyai.product_drain import ProductDrainService


@pytest.mark.parametrize("name", [
    "browser_open", "browser_action", "shell", "github_create_pull_request", "slack_post_message",
])
@pytest.mark.parametrize("legacy", [False, True])
async def test_failed_turn_needs_no_review_and_accepts_follow_up(runtime, active, actor, name, legacy):
    session, turn, prepared = active
    receipts = EffectReceipts(runtime.database)
    await receipts.reserve(prepared.scope, "failed-browser", name, {"url": "http://localhost:8840"})
    if name in {"browser_open", "browser_action"}:
        await claim_browser_dispatch(runtime.database, prepared.scope, "failed-browser", name)
    async with runtime.database.sessions() as db:
        evidence = (await db.get(Effect, "failed-browser")).resolution
    await runtime.database.fail(turn.id, "The browser action failed.")
    if legacy:
        # Recreate a failed session saved before automatic failure settlement.
        async with runtime.database.sessions.begin() as db:
            effect = await db.get(Effect, "failed-browser")
            effect.state, effect.settled_at = "pending", None

    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200
        view = response.json()
        assert view["session"]["status"] == "failed"
        assert view["turns"][0]["error"] == "The browser action failed."
        assert not view["has_unsettled_effects"] and view["effects"] == []
        assert (await client.get(f"/api/sessions/{session.id}/effects")).json() == []
        submitted = await client.post(f"/api/sessions/{session.id}/messages", json={
            "prompt": "Continue with the code change.", "request_key": "after-browser-failure",
        })
        assert submitted.status_code == 202
    await runtime.database.claim_next()
    follow_up = await runtime.database.prepare(submitted.json()["id"])
    await receipts.reserve(follow_up.scope, "independent-coding", "shell", {"command": "inspect the code"})
    async with runtime.database.sessions() as db:
        effect = await db.get(Effect, "failed-browser")
        assert effect.state == "failed" and effect.settled_at is not None
        assert effect.resolution == evidence
        assert effect.tool_name == name and effect.turn_id == turn.id
    # Failure keeps the original identity; it never permits replay of that call.
    with pytest.raises(PermissionError):
        await receipts.reserve(prepared.scope, "failed-browser", name, {"url": "http://localhost:8840"})


@pytest.mark.parametrize("status", ["running", "stopped", "completed"])
async def test_ended_turns_skip_review_but_running_attempts_remain_pending(runtime, active, status):
    session, turn, prepared = active
    await EffectReceipts(runtime.database).reserve(
        prepared.scope, "unconfirmed-browser", "browser_action", {"command": {"action": "click"}},
    )
    if status != "running":
        async with runtime.database.sessions.begin() as db:
            saved_turn = await db.get(Turn, turn.id)
            saved_turn.status, saved_turn.finished_at = status, now()
            saved_session = await db.get(Session, session.id)
            saved_session.active_turn = None
            saved_session.status = "idle" if status == "completed" else status
    async with sign_in(runtime) as client:
        view = (await client.get(f"/api/sessions/{session.id}")).json()
        assert view["has_unsettled_effects"] is (status == "running")
        assert [row["id"] for row in view["effects"]] == (["unconfirmed-browser"] if status == "running" else [])


async def test_failed_browser_attempt_drain_waits_for_guest_deadline(runtime, active):
    session, turn, prepared = active
    await EffectReceipts(runtime.database).reserve(
        prepared.scope, "failed-browser", "browser_open", {"url": "http://localhost:8840"},
    )
    await runtime.database.fail(turn.id, "The browser action failed.")
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerOperation(session_id=session.id, identity=new_id(), expires_at=int(time.time()) + 60))
    async with runtime.database.sessions() as db:
        effects, _ = await ProductDrainService._unsettled(db, turn.id)
        assert effects == 1
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerOperation, session.id)).expires_at = 0
    async with runtime.database.sessions() as db:
        effects, _ = await ProductDrainService._unsettled(db, turn.id)
        assert effects == 0


async def test_late_confirmation_survives_failure_and_failure_cleanup_retry(runtime, active):
    _, turn, prepared = active
    receipts = EffectReceipts(runtime.database)
    await receipts.reserve(prepared.scope, "late-receipt", "slack_post_message", {"text": "Test message"})
    await runtime.database.fail(turn.id, "The tool result was unavailable.")
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, "late-receipt")).state == "failed"
    await receipts.confirm("late-receipt")
    await runtime.database.fail(turn.id, "Failure cleanup retried.")
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, "late-receipt")).state == "confirmed"
