"""Current Slack audience checks shared by every native read surface."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .product_slack_models import ProductSlackBinding
from .slack_audience_reads import SlackAudienceReads

logger = logging.getLogger(__name__)
DENIED_ERRORS = frozenset({
    "access_denied", "account_inactive", "channel_not_found", "invalid_auth",
    "missing_scope", "not_authed", "not_in_channel", "token_revoked",
})


def provider_failure(error: Exception) -> bool:
    """Keep reads closed, but do not turn an unavailable provider into a denial."""
    code = "timeout" if isinstance(error, TimeoutError) else "provider_unavailable"
    status, retry_after = None, None
    if isinstance(error, ConnectorError):
        code = error.code if isinstance(error.code, str) and re.fullmatch(r"[a-z_]{1,64}", error.code) else code
        status = error.status_code if isinstance(error.status_code, int) else None
        if isinstance(error.retry_after, (int, float)) and math.isfinite(error.retry_after) and error.retry_after > 0:
            retry_after = math.ceil(error.retry_after)
        denied = code in DENIED_ERRORS and not error.retryable
    else:
        status = error.status_code if isinstance(error, HTTPException) else None
        denied = status in {401, 403, 404}
    # Only structured identifiers and numbers; never exception strings, request
    # headers, provider payloads, message bodies or credentials.
    logger.warning("Slack audience check %s: code=%s status=%s retry_after=%s",
                   "denied" if denied else "unavailable", code, status, retry_after)
    if denied:
        return False
    if code == "invalid_refresh_token":
        raise HTTPException(
            503, "The Slack bot connection could not be renewed. An administrator must reconnect Slack "
            "in Settings → Connections.",
        ) from None
    raise HTTPException(
        503, "Slack access could not be verified right now. Try again shortly.",
        headers={"Retry-After": str(retry_after)} if retry_after else None,
    ) from None


async def can_read(slack, db, session, actor) -> bool:
    binding = await db.get(ProductSlackBinding, session.id)
    if binding is None:
        return False
    return await can_read_binding(slack, db, binding, actor)


async def can_read_binding(slack, db, binding, actor) -> bool:
    context = await audience_context(slack, db, binding, actor)
    if isinstance(context, bool):
        return context
    # Prefetch must finish before the caller opens an admission transaction.
    # Revalidate identity, installation and linked users above on every use.
    reads = audience_reads(slack)
    info = reads.cached(context.installed, "conversations.info", context.channel)
    channel = info.get("channel", {})
    if (not info.get("ok") or channel.get("id") != context.channel
            or channel.get("is_ext_shared") is not False):
        return False
    if channel.get("is_private") is False and channel.get("is_channel") is True:
        return True
    cursor, seen = "", set()
    for _ in range(50):
        page = reads.cached(context.installed, "conversations.members", context.channel, cursor)
        if not page.get("ok"):
            return False
        if set(context.users) & set(page.get("members", [])):
            return True
        cursor = (page.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return False


@dataclass(frozen=True)
class AudienceContext:
    installed: dict[str, Any]
    channel: str
    users: tuple[str, ...]


def audience_reads(slack) -> SlackAudienceReads:
    reads = getattr(slack, "audience_reads", None)
    if reads is None:
        slack.audience_reads = reads = SlackAudienceReads()
    return reads


async def audience_context(slack, db, binding, actor) -> AudienceContext | bool:
    """Local checks only. This function may run under canonical write ownership."""
    identity = slack.runtime.database._identity()
    principal = await identity.authorize_admission(db, getattr(actor, "product_grant", None))
    installed = await slack.installation_in(db)
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
    return AudienceContext(dict(installed), binding.channel_id, tuple(users))


async def prefetch_context(slack, context: AudienceContext) -> None:
    """Provider/token work owns no caller database transaction or admission lock."""
    reads = audience_reads(slack)
    try:
        async with asyncio.timeout(8):
            info = await reads.read(slack, context.installed, "conversations.info", context.channel)
            channel = info.get("channel", {})
            if (not info.get("ok") or channel.get("id") != context.channel
                    or channel.get("is_ext_shared") is not False):
                return
            allowed = channel.get("is_private") is False and channel.get("is_channel") is True
            cursor, seen = "", set()
            for _ in range(50):
                if allowed:
                    break
                page = await reads.read(slack, context.installed, "conversations.members", context.channel, cursor)
                if not page.get("ok"):
                    return
                allowed = bool(set(context.users) & set(page.get("members", [])))
                cursor = (page.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor or cursor in seen:
                    break
                seen.add(cursor)
    except Exception as error:  # noqa: BLE001 - provider failures never widen the read audience
        if not provider_failure(error):
            reads.denied(context.installed, context.channel)


async def prefetch_sessions(slack, actor, ids: list[str], *, deleted: bool = False) -> None:
    """Resolve a bounded page and inherited audiences before any provider await."""
    from .delegation_models import DelegatedSession
    from .models import Session
    from .swarm_models import SwarmMember

    database = slack.runtime.database
    contexts: dict[tuple[str, str, tuple[str, ...]], AudienceContext] = {}
    async with database.sessions() as db:
        candidates = set(await db.scalars(select(Session.id).outerjoin(
            DelegatedSession, DelegatedSession.session_id == Session.id,
        ).outerjoin(SwarmMember, SwarmMember.session_id == Session.id).where(
            Session.id.in_(ids),
            or_(Session.surface == "slack", DelegatedSession.session_id.is_not(None),
                SwarmMember.session_id.is_not(None)),
        )))
        pending = set()
        for session_id in candidates:
            session = await db.get(Session, session_id)
            if session is None:
                continue
            try:
                if deleted:
                    if not await database.same_person(db, actor, session.owner_id):
                        continue
                else:
                    await database.execution_session(db, session_id, actor)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                continue
            pending.add(session_id)
        seen: set[str] = set()
        for _ in range(8):
            if not pending:
                break
            following: set[str] = set()
            for session_id in pending - seen:
                seen.add(session_id)
                session = await db.get(Session, session_id)
                if session is None:
                    continue
                delegation = await db.get(DelegatedSession, session_id)
                member = await db.get(SwarmMember, session_id)
                if delegation and delegation.parent_session_id != session_id:
                    following.add(delegation.parent_session_id)
                elif session.surface == "slack" and member and member.mission_id != session_id:
                    following.add(member.mission_id)
                elif session.surface == "slack":
                    binding = await db.get(ProductSlackBinding, session_id)
                    if binding is not None:
                        context = await audience_context(slack, db, binding, actor)
                        if isinstance(context, AudienceContext):
                            contexts[(context.installed["authority"], context.channel, context.users)] = context
                if member and member.mission_id != session_id:
                    following.add(member.mission_id)
            pending = following - seen
        if pending:
            raise HTTPException(503, "Conversation audience verification exceeded its bound.")
    # Closing this SQL context is mandatory even when token renewal uses a
    # shared lock: a queued exclusive writer can block a recursive shared lock.
    await prefetch_contexts(slack, list(contexts.values()))


async def prefetch_contexts(slack, contexts: list[AudienceContext]) -> None:
    slots = asyncio.Semaphore(4)

    async def prefetch(context: AudienceContext) -> None:
        async with slots:
            await prefetch_context(slack, context)

    tasks = [asyncio.create_task(prefetch(context)) for context in contexts]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def historical_rows(slack, conn, ids: list[str]):
    history = slack.runtime.product_history
    t = history.table
    return (await conn.execute(text(history.graph_cte() + f"""
        SELECT roots.run_id,s.team_id,s.channel,s.thread_ts,r.owner_id,
          (SELECT e.user_id FROM {t('slack_events')} e WHERE e.run_id=s.run_id
           ORDER BY e.created_at LIMIT 1) AS sender
        FROM history_roots roots LEFT JOIN {t('slack_threads')} s ON s.run_id=roots.root_id
        JOIN {t('runs')} r ON r.id=roots.root_id WHERE roots.run_id IN :ids
          AND (s.run_id IS NOT NULL OR EXISTS(SELECT 1 FROM {t('slack_events')} e WHERE e.run_id=roots.root_id))
    """).bindparams(bindparam("ids", expanding=True)), {"ids": ids})).mappings().all()


async def historical_targets(slack, db, principal, rows, *, source_actor=None):
    from .models import User
    installed = await slack.installation_in(db)
    actor = await db.get(User, principal.id)
    if actor is None:
        return None, []
    cast(Any, actor).product_grant = principal.grant
    # Only the internally verified ingress actor can supply channel evidence.
    if (source_actor is not None and source_actor.id == principal.id
            and getattr(source_actor, "product_grant", None) == principal.grant):
        cast(Any, actor).slack_channel_evidence = getattr(source_actor, "slack_channel_evidence", None)
    bindings = []
    for row in rows:
        if not row["team_id"] or not row["channel"]:
            continue  # Orphaned Slack history has no verifiable audience.
        binding = SimpleNamespace(
            team_id=row["team_id"], channel_id=row["channel"],
            connection_authority=installed["authority"],
            requester_id=f"slack:{row['team_id']}:{row['sender']}" if row["sender"] else row["owner_id"],
        )
        bindings.append((row["run_id"], binding))
    return actor, bindings


async def prefetch_historical(slack, principal, ids: list[str]) -> None:
    """Release both source and native snapshots before provider/token awaits."""
    if not ids:
        return
    async with slack.runtime.product_history.read() as conn:
        rows = await historical_rows(slack, conn, ids)
    if not rows:
        return
    async with slack.runtime.database.sessions() as db:
        actor, bindings = await historical_targets(slack, db, principal, rows)
        contexts = []
        if actor is not None:
            for _, binding in bindings:
                context = await audience_context(slack, db, binding, actor)
                if isinstance(context, AudienceContext):
                    contexts.append(context)
    await prefetch_contexts(slack, contexts)


async def historical_access(slack, conn, principal, ids: list[str], *, source_actor=None) -> set[str]:
    """Fresh local authorization with cache-only provider facts in every transaction."""
    if not ids:
        return set()
    rows = await historical_rows(slack, conn, ids)
    if not rows:
        return set(ids)

    async def check(db):
        actor, bindings = await historical_targets(slack, db, principal, rows, source_actor=source_actor)
        allowed = set(ids) - {row["run_id"] for row in rows}
        if actor is not None:
            for run_id, binding in bindings:
                if await can_read_binding(slack, db, binding, actor):
                    allowed.add(run_id)
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
