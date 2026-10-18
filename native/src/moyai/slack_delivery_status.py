"""Safe delivery feedback and retries for Slack's explicit rejections only."""

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .models import Delivery, DeliverySchedule


async def status(database, db, session_id):
    counts = dict((await db.execute(select(Delivery.status, func.count()).where(
        Delivery.session_id == session_id, Delivery.identity.startswith('product-slack:'),
        Delivery.status.in_(['pending', 'sending', 'rejected']),
    ).group_by(Delivery.status))).all())
    return {key: counts.get(key, 0) for key in ('pending', 'sending', 'rejected')}


async def retry(runtime, actor, session_id):
    slack = runtime.product_slack
    if slack is None:
        raise HTTPException(409, 'Slack delivery is unavailable.')
    async with runtime.database.sessions() as db:
        session = await runtime.database.allowed_session(db, session_id, actor)
        if not await runtime.database.same_person(db, actor, session.owner_id):
            raise HTTPException(403, 'Only the conversation owner can retry Slack delivery.')
        rows = list(await db.scalars(select(Delivery).where(
            Delivery.session_id == session_id, Delivery.status == 'rejected',
            Delivery.identity.startswith('product-slack:'),
        )))
    retried = 0
    for row in rows:
        if not await slack.delivery_allowed(row):
            continue
        async with runtime.database.sessions.begin() as db:
            await runtime.database.allowed_session(db, session_id, actor)
            changed = await db.scalar(update(Delivery).where(
                Delivery.id == row.id, Delivery.status == 'rejected',
            ).values(status='pending', receipt=None).returning(Delivery.id))
            if changed:
                await db.execute(update(DeliverySchedule).where(
                    DeliverySchedule.delivery_id == row.id,
                ).values(next_attempt=0, checked_at=0))
                retried += 1
    if rows and not retried:
        raise HTTPException(409, 'Slack access changed. Check the Slack connection in Settings before retrying.')
    return {'retried': retried}
