"""Safe delivery feedback and retries for Slack's explicit rejections only."""

import json
import re

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .models import Delivery, DeliverySchedule
from .slack_file_delivery import SlackFileDelivery


async def status(database, db, session_id):
    counts = dict((await db.execute(select(Delivery.status, func.count()).where(
        Delivery.session_id == session_id, Delivery.identity.startswith('product-slack:'),
        Delivery.status.in_(['pending', 'sending', 'rejected']),
    ).group_by(Delivery.status))).all())
    result = {key: counts.get(key, 0) for key in ('pending', 'sending', 'rejected')}
    rejected_files, codes = 0, set()
    for row in await db.scalars(select(Delivery).where(
        Delivery.session_id == session_id, Delivery.identity.startswith('product-slack:'),
        Delivery.status == 'rejected',
    )):
        if not SlackFileDelivery.is_content(json.loads(database.security.unseal(row.content))):
            continue
        rejected_files += 1
        receipt = json.loads(database.security.unseal(row.receipt)) if row.receipt else {}
        match = re.fullmatch(r'Slack [A-Za-z.]+ rejected the upload: ([a-z0-9_]{1,80})\.', receipt.get('error', ''))
        if match:
            codes.add(match[1])
    return {**result, 'rejected_files': rejected_files, 'file_error_codes': sorted(codes)}


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
