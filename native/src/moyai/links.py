"""Session attachments around native agent execution; no external URL fetching."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update

from .database import Database
from .models import Session, SessionLink, User, new_id

PR_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?$")
PR_MENTION = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*(?![A-Za-z0-9])")


class LinkInput(BaseModel):
    kind: Literal["pull_request", "output", "source"]
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_link(self) -> LinkInput:
        value = self.url.strip()
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("Use a shorter web address.")
        if any(ord(character) < 32 for character in value):
            raise ValueError("Use a valid web address.")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Use an HTTP(S) address without embedded credentials.")
        # Reading port validates malformed or out-of-range values too.
        port = parsed.port
        self.title = self.title.strip()
        if self.kind == "pull_request":
            match = PR_PATH.fullmatch(parsed.path)
            if parsed.scheme != "https" or parsed.hostname != "github.com" or port not in {None, 443} or not match:
                raise ValueError("Use a GitHub pull request URL.")
            owner, repo, number = match.groups()
            self.url = f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}"
            self.title = self.title or f"{owner}/{repo} #{number}"
        else:
            self.url = urlunsplit(parsed)
            self.title = self.title or (parsed.hostname + parsed.path)[:200]
        return self


async def save_link(database: Database, actor: User, session_id: str, body: LinkInput) -> SessionLink:
    async with database.sessions.begin() as db:
        await database.allowed_session(db, session_id, actor)
        # Serialize duplicate attachments and deletion against the session row.
        locked = await db.scalar(update(Session).where(
            Session.id == session_id, Session.deleted.is_(False),
        ).values(revision=Session.revision + 1).returning(Session.id))
        if locked is None:
            raise HTTPException(404, "Conversation not found.")
        row = await db.scalar(select(SessionLink).where(
            SessionLink.session_id == session_id, SessionLink.kind == body.kind, SessionLink.url == body.url,
        ))
        if row is None:
            row = SessionLink(id=new_id(), session_id=session_id, actor_id=actor.id, **body.model_dump())
            db.add(row)
        return row
