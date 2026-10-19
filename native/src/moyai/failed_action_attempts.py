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


async def settle_failed_action_attempts(db: AsyncSession, session_id: str) -> None:
    # Preserve the original arguments and dispatch evidence. "failed" describes
    # the attempt, not proof that its external action completed or did not run.
    # Keeping the row also prevents redelivery from dispatching its identity again.
    await db.execute(update(Effect).where(
        Effect.session_id == session_id,
        Effect.state == "pending",
        failed_action_attempt(),
    ).values(state="failed", settled_at=now()))
