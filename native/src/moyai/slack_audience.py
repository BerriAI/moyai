"""Current Slack audience checks shared by every native read surface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from app.db import database as source_database
from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from .product_slack_models import ProductSlackBinding


async def can_read(slack, db, session, actor) -> bool:
    binding = await db.get(ProductSlackBinding, session.id)
    if binding is None:
        return False
    return await can_read_binding(slack, db, binding, actor)


async def can_read_binding(slack, db, binding, actor) -> bool:
    identity = slack.runtime.database._identity()
    principal = await identity.authorize_admission(db, getattr(actor, "product_grant", None))
    installed = await source_database(slack.installation)
    if installed["authority"] != binding.connection_authority or installed["team_id"] != binding.team_id:
        return False
    if binding.channel_id.startswith("D"):
        return await slack.runtime.database.same_person(db, actor, binding.requester_id)
    # Only the current verified ingress may use signed membership evidence.
    if getattr(actor, "slack_channel_evidence", None) == (binding.team_id, binding.channel_id):
        return principal.kind == "slack"
    if principal.kind == "slack":
        slack_ids = [principal.id]
    elif principal.individual:
        candidates = (await db.execute(text(
            f"SELECT id FROM {identity._table('users')} WHERE kind='slack' AND email=:email",
        ), {"email": principal.email})).scalars().all()
        slack_ids = [value for value in candidates if
                     await identity.personal_memory_owner(db, value) == principal.id]
    else:
        return False
    users = [value.split(":")[2] for value in slack_ids if value.startswith(f"slack:{binding.team_id}:")]
    if not users:
        return False
    key = ("slack_audience", actor.id, binding.team_id, binding.channel_id, installed["authority"])
    cache = getattr(actor, "slack_read_cache", db.info)
    if key in cache:
        return cache[key]
    try:
        async with asyncio.timeout(8):
            token = await slack.connectors.slack_bot_token()
            headers = {"Authorization": "Bearer " + token}
            info = await slack.connectors.request("GET", "https://slack.com/api/conversations.info",
                                                  headers=headers, params={"channel": binding.channel_id})
            channel = info.get("channel", {})
            if (not info.get("ok") or channel.get("id") != binding.channel_id
                    or channel.get("is_ext_shared") is not False):
                return False
            allowed = channel.get("is_private") is False and channel.get("is_channel") is True
            cursor, seen = "", set()
            for _ in range(50):
                if allowed:
                    break
                page = await slack.connectors.request("GET", "https://slack.com/api/conversations.members",
                    headers=headers, params={"channel": binding.channel_id, "cursor": cursor, "limit": 200})
                if not page.get("ok"):
                    return False
                allowed = bool(set(users) & set(page.get("members", [])))
                cursor = (page.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor or cursor in seen:
                    break
                seen.add(cursor)
            current = await source_database(slack.installation)
            allowed = allowed and current["authority"] == installed["authority"]
    except (TimeoutError, HTTPException):
        return False
    except Exception:  # noqa: BLE001 - provider failure must fail closed at this read boundary
        # A failed provider read never broadens the audience. No provider data
        # or credentials enter a browser-facing error or logs here.
        return False
    cache[key] = allowed
    return allowed


async def historical_access(slack, conn, principal, ids: list[str]) -> set[str]:
    """Apply the same audience to original history, files and continuation reads."""
    if not ids:
        return set()
    history = slack.runtime.product_history
    t = history.table
    rows = (await conn.execute(text(f"""
        SELECT roots.run_id,s.team_id,s.channel,s.thread_ts,r.owner_id,
          (SELECT e.user_id FROM {t('slack_events')} e WHERE e.run_id=s.run_id
           ORDER BY e.created_at LIMIT 1) AS sender
        FROM {t('run_roots')} roots LEFT JOIN {t('slack_threads')} s ON s.run_id=roots.root_id
        JOIN {t('runs')} r ON r.id=roots.root_id WHERE roots.run_id IN :ids
          AND (s.run_id IS NOT NULL OR EXISTS(SELECT 1 FROM {t('slack_events')} e WHERE e.run_id=roots.root_id))
    """).bindparams(bindparam("ids", expanding=True)), {"ids": ids})).mappings().all()
    if not rows:
        return set(ids)
    installed = await source_database(slack.installation)

    async def check(db):
        from .models import User
        actor = await db.get(User, principal.id)
        allowed = set(ids) - {row["run_id"] for row in rows}
        if actor is None:
            return allowed
        cast(Any, actor).product_grant = principal.grant
        for row in rows:
            if not row["team_id"] or not row["channel"]:
                continue  # Orphaned Slack history has no verifiable audience.
            binding = SimpleNamespace(
                team_id=row["team_id"], channel_id=row["channel"],
                connection_authority=installed["authority"],
                requester_id=f"slack:{row['team_id']}:{row['sender']}" if row["sender"] else row["owner_id"],
            )
            if await can_read_binding(slack, db, binding, actor):
                allowed.add(row["run_id"])
        return allowed

    if isinstance(conn, AsyncSession):
        return await check(conn)
    # Source history normally has a separate engine/search path. Native controls
    # must use the native schema. Reuse the connection only when it is native.
    database = slack.runtime.database
    reader = (AsyncSession(bind=conn, expire_on_commit=False) if conn.engine is database.engine
              else database.sessions())
    async with reader as db:
        return await check(db)


async def historical_messages(slack, conn, principal, run_id: str, rows: list[dict]) -> None:
    """Decorate only receipt-backed historical Slack input; never infer from body text."""
    from .message_sources import source_view
    history = slack.runtime.product_history
    ids = [row['id'] for row in rows if row['role'] == 'user']
    if not ids:
        return
    receipts = (await conn.execute(text(f"""
        SELECT m.id,m.user_id,u.name,r.team_id,r.channel,r.message_ts,r.user_id AS slack_user
        FROM {history.table('messages')} m
        JOIN {history.table('slack_receipts')} r ON r.message_id=m.id AND r.run_id=m.run_id
        LEFT JOIN {history.table('users')} u ON u.id=m.user_id
        WHERE m.run_id=:run AND m.id IN :ids AND m.user_id='slack:'||r.team_id||':'||r.user_id
    """).bindparams(bindparam('ids', expanding=True)), {'run': run_id, 'ids': ids})).mappings().all()
    sources = {row['id']: row for row in receipts}
    async def decorate(db):
        relationships = {}
        for row in rows:
            author = row.get('author_id', row.get('user_id'))
            row['is_self'] = author == principal.id
            origin = sources.get(row['id'])
            if not origin:
                continue
            if author not in relationships:
                relationships[author] = await slack.runtime.database._identity().personal_memory_owner(db, author)
            row['is_self'] |= relationships[author] == principal.id
            row['author_name'] = origin['name']
            row.update(source_view({'kind': 'slack', 'team_id': origin['team_id'],
                                    'channel_id': origin['channel'], 'message_ts': origin['message_ts']}))
            prefix = f"Slack reply from {origin['slack_user']}:\n"
            row['content'] = row['content'].removeprefix(prefix)
    if isinstance(conn, AsyncSession):
        await decorate(conn)
    else:
        database = slack.runtime.database
        reader = (AsyncSession(bind=conn, expire_on_commit=False) if conn.engine is database.engine
                  else database.sessions())
        async with reader as db:
            await decorate(db)
