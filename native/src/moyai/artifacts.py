"""Save user-facing outputs from the native workspace into private object storage."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import PurePosixPath
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .contracts import RunScope
from .database import Database
from .links import LinkInput, save_link
from .models import Artifact, new_id
from .storage import ObjectStore

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


def artifact_capability(database: Database, objects: ObjectStore) -> Capability[RunScope]:
    async def save_artifact(ctx: RunContext[RunScope], path: str) -> dict[str, str | int]:
        """Save a workspace file for the user to download after this computer expires.

        Call this for finished reports, images, archives and other requested deliverables.
        The returned URL requires the user's authenticated Moyai session.
        """
        await database.authorize_scope(ctx.deps)
        root = PurePosixPath(await ctx.workspace.realpath(await ctx.workspace.working_dir()))
        resolved = PurePosixPath(await ctx.workspace.realpath(path))
        if not resolved.is_relative_to(root) or resolved == root:
            raise ToolFailed("Only files inside this conversation's workspace can be delivered.")
        entry = await ctx.workspace.stat(str(resolved))
        if entry.size is not None and entry.size > MAX_ARTIFACT_BYTES:
            raise ToolFailed("The file exceeds the 20 MB delivery limit.")
        content = await ctx.workspace.read_bytes(str(resolved))
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ToolFailed("The file exceeds the 20 MB delivery limit.")
        digest = await objects.put(content)
        name = str(resolved.relative_to(root))
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        content_hash = hashlib.sha256(content).hexdigest()
        try:
            async with database.sessions.begin() as db:
                artifact = await db.scalar(select(Artifact).where(
                    Artifact.turn_id == ctx.deps.turn_id, Artifact.name == name, Artifact.digest == digest
                ))
                if artifact is None:
                    artifact = Artifact(
                        id=new_id(), session_id=ctx.deps.session_id, turn_id=ctx.deps.turn_id,
                        name=name, media_type=media_type, digest=digest,
                        content_hash=content_hash, size=len(content),
                    )
                    db.add(artifact)
        except IntegrityError:
            async with database.sessions() as db:
                artifact = await db.scalar(select(Artifact).where(
                    Artifact.turn_id == ctx.deps.turn_id, Artifact.name == name, Artifact.digest == digest
                ))
                if artifact is None:
                    raise
        return {
            "id": artifact.id, "name": name, "bytes": len(content),
            "sha256": content_hash, "download_url": f"/api/files/{artifact.id}",
        }

    async def attach_link(
        ctx: RunContext[RunScope], url: str,
        kind: Literal["pull_request", "output", "source"], title: str = "",
    ) -> dict[str, str]:
        """Attach a PR, published output/preview, or source to this session's Workspace panel.

        Use this for a PR you created or are working on, and for actual delivered output URLs.
        It records a link only; it does not fetch, create, merge, or publish anything externally.
        """
        await database.authorize_scope(ctx.deps)
        actor = await database.user(ctx.deps.actor_id)
        try:
            body = LinkInput(kind=kind, url=url, title=title)
        except ValueError as error:
            raise ToolFailed("Use a valid HTTP(S) link without credentials; PR links must be GitHub pull request URLs.") from error
        row = await save_link(database, actor, ctx.deps.session_id, body)
        return {"id": row.id, "url": row.url, "title": row.title}

    return Capability(id="moyai_artifacts", tools=[save_artifact, attach_link])
