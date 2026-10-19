"""Keep failed action attempts as evidence without requiring manual review."""

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from .models import Effect, Turn, now


def failed_action_attempt() -> ColumnElement[bool]:
    # An agent can report a failed tool in an otherwise completed answer. Once
    # the turn has ended, missing confirmation is an unsuccessful attempt, not
    # a verification task for the user.
    return or_(
        Effect.state == "failed",
        Effect.turn_id.in_(select(Turn.id).where(Turn.status.in_(("completed", "failed", "stopped")))),
    )


async def settle_failed_action_attempts(
    db: AsyncSession, session_id: str, *, accepted_turn_id: str | None = None,
) -> None:
    # Preserve the original arguments and dispatch evidence. "failed" describes
    # the attempt, not proof that its external action completed or did not run.
    # Keeping the row also prevents redelivery from dispatching its identity again.
    ended = failed_action_attempt()
    if accepted_turn_id is not None:
        # A final answer ends tool execution before computer saving completes.
        # Settle its unsuccessful attempts at that boundary, not on the next
        # user message. Deferred approvals do not use this path.
        ended = or_(ended, Effect.turn_id.in_(select(Turn.id).where(
            Turn.id == accepted_turn_id, Turn.session_id == session_id,
            Turn.status == "saving", Turn.result_accepted.is_(True),
            Turn.result_fingerprint.is_not(None), Turn.finished_at.is_not(None),
        )))
    await db.execute(update(Effect).where(
        Effect.session_id == session_id,
        Effect.state == "pending",
        ended,
    ).values(state="failed", settled_at=now()))
