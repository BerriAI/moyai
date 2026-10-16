"""Read-only presentation of native run lineage and session resources."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import deque
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai_harness.step_persistence import RunRecord
from sqlalchemy import delete, select, update

from .links import PR_MENTION, LinkInput, save_link
from .models import Artifact, Message, Session, SessionLink, SubagentLabel, Turn, Upload, User

if TYPE_CHECKING:
    from .runtime import Runtime


AGENT_NAMES = (
    "Cicero", "Popper", "Copernicus", "Curie", "Kepler", "Noether", "Lovelace", "Euclid",
    "Turing", "Hopper", "Gauss", "Newton", "Galileo", "Sagan", "Faraday", "Hypatia",
    "Tesla", "Pascal", "Descartes", "Leibniz", "Leavitt", "Franklin", "Archimedes", "Spinoza",
    "Plato", "Euler", "Maxwell", "Bohr", "Feynman", "Hamilton", "Fourier", "Darwin",
    "Apollo", "Hermes", "Athena", "Atlas", "Iris", "Lyra", "Orion", "Vega",
    "Artemis", "Thales", "Solon", "Zeno", "Mendel", "Raman", "Planck", "Elara",
)


async def agent_labels(runtime: Runtime, session_id: str, records: list[RunRecord]) -> dict[str, SubagentLabel]:
    if not records:
        return {}
    async with runtime.database.sessions() as db:
        cached = {row.run_id: row for row in await db.scalars(select(SubagentLabel).where(
            SubagentLabel.session_id == session_id,
        ))}
    if all(record.run_id in cached for record in records):
        return cached
    async with runtime.database.sessions.begin() as db:
        # Serialize allocation across workers. This materialized display cache
        # never modifies native runs, snapshots, parentage, or task admission.
        locked = await db.scalar(update(Session).where(
            Session.id == session_id, Session.deleted.is_(False),
        ).values(revision=Session.revision).returning(Session.id))
        if locked is None:
            raise HTTPException(404, "Conversation not found.")
        existing = list(await db.scalars(select(SubagentLabel).where(SubagentLabel.session_id == session_id)))
        labels = {row.run_id: row for row in existing}
        names = {row.name for row in existing}
        for record in records:
            if record.run_id in labels:
                continue
            seed = int.from_bytes(hashlib.sha256(record.run_id.encode()).digest()[:4], "big")
            name = AGENT_NAMES[seed % len(AGENT_NAMES)]
            for offset in range(len(AGENT_NAMES)):
                candidate = AGENT_NAMES[(seed + offset) % len(AGENT_NAMES)]
                if candidate not in names:
                    name = candidate
                    break
            else:
                suffix = 2
                while f"{name} {suffix}" in names:
                    suffix += 1
                name = f"{name} {suffix}"
            row = SubagentLabel(run_id=record.run_id, session_id=session_id, name=name, avatar=seed % 4)
            db.add(row)
            labels[record.run_id] = row
            names.add(name)
        return labels


def link_view(row: SessionLink, user: User, owner_id: str) -> dict:
    return {
        "id": row.id, "kind": row.kind, "url": row.url, "title": row.title,
        "origin": "attached", "can_remove": user.id in {row.actor_id, owner_id},
    }


async def native_subagents(runtime: Runtime, session_id: str, turns: list[Turn], user: User) -> tuple[list[dict], bool]:
    # Always enter the native lineage through authorized product turns. Never
    # enumerate the global run store, which contains other people's contexts.
    queue = deque((turn.id, turn, 0) for turn in turns)
    seen: set[str] = set()
    found: list[tuple[RunRecord, Turn, int]] = []
    while queue and len(found) < 50:
        parent_id, turn, depth = queue.popleft()
        if parent_id in seen or depth > 12:
            continue
        seen.add(parent_id)
        children = await runtime.stores.steps.list_runs(parent_run_id=parent_id)
        for child in children:
            if child.run_id in seen:
                continue
            found.append((child, turn, depth))
            queue.append((child.run_id, turn, depth + 1))
            if len(found) == 50:
                break

    semaphore = asyncio.Semaphore(6)
    labels = await agent_labels(runtime, session_id, [record for record, _, _ in found])

    async def summary(record: RunRecord, turn: Turn, depth: int) -> dict:
        async with semaphore:
            events = await runtime.stores.steps.list_events(run_id=record.run_id)
        terminal = next((event for event in reversed(events) if event.kind in {"run_completed", "run_failed"}), None)
        status = (
            "completed" if terminal and terminal.kind == "run_completed" else
            "failed" if terminal else
            "running" if turn.status in {"starting", "running"} else
            "waiting" if turn.status == "waiting" else "interrupted"
        )
        own = turn.actor_id == user.id
        return {
            "id": record.run_id, "parent_id": record.parent_run_id, "turn_id": turn.id,
            "name": labels[record.run_id].name, "avatar": labels[record.run_id].avatar,
            "status": status, "started_at": record.started_at.isoformat(),
            "can_inspect": own, "depth": depth,
        }

    return list(await asyncio.gather(*(summary(*item) for item in found))), bool(queue)


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()

    async def recheck(user: User, session_id: str) -> None:
        current = await runtime.database.user(user.id)
        if current.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, session_id)

    @router.get("/api/sessions/{session_id}/workspace")
    async def overview(session_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            session = await runtime.database.allowed_session(db, session_id, user)
            turns = list(await db.scalars(select(Turn).where(
                Turn.session_id == session_id, Turn.preparation.is_not(None),
            ).order_by(Turn.created_at.desc()).limit(21)))
            recent_limited = len(turns) > 20
            turns = turns[:20]
            if session.active_turn and not any(turn.id == session.active_turn for turn in turns):
                active = await db.get(Turn, session.active_turn)
                if active:
                    turns.insert(0, active)
            attached = [
                link_view(row, user, session.owner_id)
                for row in await db.scalars(select(SessionLink).where(
                    SessionLink.session_id == session_id,
                ).order_by(SessionLink.created_at.desc()).limit(100))
            ]
            files = [{
                "id": row.id, "name": row.name, "size": row.size, "url": f"/api/files/{row.id}",
            } for row in await db.scalars(select(Artifact).where(
                Artifact.session_id == session_id,
            ).order_by(Artifact.created_at.desc()).limit(100))]
            sources = [{
                "id": row.id, "name": row.name, "media_type": row.media_type, "url": f"/api/uploads/{row.id}",
            } for row in await db.scalars(select(Upload).where(
                Upload.session_id == session_id,
            ).order_by(Upload.created_at.desc()).limit(100))]
            public_messages = list(await db.scalars(select(Message.content).where(
                Message.session_id == session_id,
            ).order_by(Message.id.desc()).limit(100)))
        # Only public transcript URLs become "mentioned" PRs, never URLs found in
        # private tool output, reasoning, memory, or a delegate's working context.
        known = {row["url"] for row in attached if row["kind"] == "pull_request"}
        mentioned = []
        for content in public_messages:
            for url in PR_MENTION.findall(runtime.security.unseal(content).decode()):
                try:
                    item = LinkInput(kind="pull_request", url=url)
                except ValidationError:
                    # Conversation text is untrusted; an invalid mentioned URL
                    # must not prevent valid session resources from loading.
                    continue
                if item.url not in known and len(mentioned) < 30:
                    known.add(item.url)
                    mentioned.append({**item.model_dump(), "origin": "mentioned", "can_remove": False})
        subagents, truncated = await native_subagents(runtime, session_id, turns, user)
        await recheck(user, session_id)
        return {
            "links": attached + mentioned, "files": files, "sources": sources,
            "subagents": subagents, "subagents_limited": truncated or recent_limited,
        }

    @router.get("/api/sessions/{session_id}/subagents/{run_id}")
    async def subagent(session_id: str, run_id: str, user: User = Depends(actor)):
        await runtime.database.session(user, session_id)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,250}", run_id) or ".." in run_id:
            raise HTTPException(404, "Subagent not found.")
        record = await runtime.stores.steps.get_run(run_id=run_id)
        cursor = record
        visited: set[str] = set()
        authorized = False
        async with runtime.database.sessions() as db:
            while cursor and cursor.parent_run_id and len(visited) < 32:
                parent_id = cursor.parent_run_id
                if parent_id in visited:
                    break
                visited.add(parent_id)
                root = await db.get(Turn, parent_id)
                if root:
                    authorized = root.session_id == session_id and root.actor_id == user.id
                    break
                cursor = await runtime.stores.steps.get_run(run_id=parent_id)
        if not authorized or record is None:
            raise HTTPException(404, "Subagent not found.")
        events = await runtime.stores.steps.list_events(run_id=run_id)
        snapshot = await runtime.stores.steps.latest_snapshot(run_id=run_id, include_interrupted=True)
        replies = [
            part.content for message in (snapshot.messages if snapshot else [])
            if isinstance(message, ModelResponse) and message.run_id == run_id
            for part in message.parts if isinstance(part, TextPart)
        ]
        labels = await agent_labels(runtime, session_id, [record])
        await recheck(user, session_id)
        return {
            "id": run_id, "name": labels[run_id].name,
            "output": "\n\n".join(replies[-3:])[-30_000:],
            "steps": [{
                "kind": event.kind, "tool": event.tool_name, "timestamp": event.timestamp.isoformat(),
            } for event in events[-60:]],
        }

    @router.post("/api/sessions/{session_id}/links", status_code=201)
    async def attach(session_id: str, body: LinkInput, user: User = Depends(actor)):
        row = await save_link(runtime.database, user, session_id, body)
        session = await runtime.database.session(user, session_id)
        return link_view(row, user, session.owner_id)

    @router.delete("/api/sessions/{session_id}/links/{link_id}")
    async def remove(session_id: str, link_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            session = await runtime.database.allowed_session(db, session_id, user)
            row = await db.get(SessionLink, link_id)
            if row is None or row.session_id != session_id:
                raise HTTPException(404, "Link not found.")
            if user.id not in {row.actor_id, session.owner_id}:
                raise HTTPException(403, "Only the person who added this link or the session owner can remove it.")
            await db.execute(delete(SessionLink).where(SessionLink.id == link_id))
        return {"removed": True}

    return router
