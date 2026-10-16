"""Confirmed native GitHub creation receipts, independent of PR attachments.

The receipt identity and state/date validation preserve the semantics of
app/pr_analytics.py and app/session_pull_requests.py without importing their
runtime, credentials, or database.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from .analytics import utc
from .contracts import RunScope
from .models import Base, Effect
from .pull_requests import parse_pr_url


class PRPublication(Base):
    __tablename__ = "moyai_pr_publications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # native effect identity
    actor_id: Mapped[str] = mapped_column(String(512), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    turn_id: Mapped[str] = mapped_column(String(36), index=True)
    model: Mapped[str] = mapped_column(String(200))
    connection_id: Mapped[str] = mapped_column(String(36))
    connection_revision: Mapped[int] = mapped_column(Integer)
    url_key: Mapped[str] = mapped_column(String(64), index=True)  # private canonical-URL digest
    target: Mapped[str] = mapped_column(Text)  # encrypted URL/repository; never in analytics responses
    number: Mapped[int] = mapped_column(BigInteger)
    github_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    repository_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)  # confirmed tool receipt time
    github_created_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


@dataclass(frozen=True)
class CreationReceipt:
    connection_id: str
    connection_revision: int
    repository: str
    url: str
    number: int
    github_id: int | None
    repository_id: int | None
    created_at: str
    github_created_at: str | None


def positive_id(value: Any) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63 else None


def creation_receipt(
    scope: RunScope, tool_name: str, args: dict[str, Any], result: Any, confirmed_at: str,
) -> CreationReceipt | None:
    """Only a successful exact creation tool and its selected grant can attest creation."""
    selected = [
        grant for grant in scope.grants
        if grant.kind == "github" and tool_name == f"github_{grant.id[:8]}_create_pull_request"
    ]
    if len(selected) != 1 or selected[0].read_only:
        return None
    value = result
    # Pinned Pydantic MCP normally returns structured dicts or JSON text. Do
    # not search arbitrary prose, tool arguments, attachments, or nested bodies.
    for _ in range(3):
        if isinstance(value, str) and len(value) <= 65_536:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        elif isinstance(value, list) and len(value) == 1:
            value = value[0]
        elif isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
        else:
            break
    if not isinstance(value, dict) or value.get("isError") or value.get("is_error") or value.get("error"):
        return None
    url = value.get("html_url") or value.get("url")
    if not isinstance(url, str):
        return None
    try:
        pr = parse_pr_url(url)
    except HTTPException:
        return None
    owner, repo = args.get("owner"), args.get("repo")
    if not isinstance(owner, str) or not isinstance(repo, str) or pr.repository.casefold() != f"{owner}/{repo}".casefold():
        return None
    if value.get("number") is not None and positive_id(value["number"]) != pr.number:
        return None
    confirmed = utc(confirmed_at, require_timezone=True)
    if confirmed is None:
        return None
    base = value.get("base")
    repository = base.get("repo") if isinstance(base, dict) else None
    repository = repository if isinstance(repository, dict) else {}
    if repository.get("full_name") and str(repository["full_name"]).casefold() != pr.repository.casefold():
        return None
    created = utc(value.get("created_at"), require_timezone=True)
    grant = selected[0]
    return CreationReceipt(
        grant.id, grant.revision, pr.repository, pr.url, pr.number,
        positive_id(value.get("id")), positive_id(repository.get("id")), confirmed.isoformat(),
        created.isoformat() if created else None,
    )


async def persist_publication(database: Any, scope: RunScope, identity: str, receipt: CreationReceipt) -> None:
    """Project already-confirmed evidence; never authorize or repeat an external write."""
    async with database.sessions.begin() as db:
        effect = await db.get(Effect, identity)
        if (
            effect is None or effect.state != "confirmed" or effect.session_id != scope.session_id
            or effect.actor_id != scope.actor_id or effect.turn_id != scope.turn_id
            or effect.tool_name != f"github_{receipt.connection_id[:8]}_create_pull_request"
        ):
            raise ValueError("A matching confirmed native effect is required.")
        fields = asdict(receipt)
        target = {"url": fields.pop("url"), "repository": fields.pop("repository")}
        insert = postgres_insert if database.settings.postgres_url else sqlite_insert
        await db.execute(insert(PRPublication).values(
            id=identity, actor_id=scope.actor_id, session_id=scope.session_id, turn_id=scope.turn_id,
            model=scope.model, url_key=database.security.object_id(target["url"].casefold().encode()),
            target=database.security.seal(target), **fields,
        ).on_conflict_do_nothing(index_elements=["id"]))


def verified_status(receipt: PRPublication, value: Any) -> dict[str, Any] | None:
    """Validate GitHub identity and aware dates before attributing any merge."""
    if not receipt.github_id and not receipt.repository_id:
        # A mutable repository-name alias alone cannot prove this is still the
        # same historical PR. Creation remains known; current status does not.
        return None
    if not isinstance(value, dict) or positive_id(value.get("number")) != receipt.number:
        return None
    if receipt.github_id and positive_id(value.get("id")) != receipt.github_id:
        return None
    base = value.get("base")
    repository = base.get("repo") if isinstance(base, dict) else None
    if receipt.repository_id and (
        not isinstance(repository, dict) or positive_id(repository.get("id")) != receipt.repository_id
    ):
        return None
    if value.get("state") not in {"open", "closed"} or not isinstance(value.get("draft"), bool):
        return None
    created = utc(value.get("created_at"), require_timezone=True)
    merged = utc(value.get("merged_at"), require_timezone=True)
    if value.get("created_at") and created is None or value.get("merged_at") and merged is None:
        return None
    if "merged" in value and not isinstance(value["merged"], bool):
        return None
    if value.get("merged") is False and merged is not None:
        return None
    is_merged = value.get("merged") is True or merged is not None
    if is_merged and value.get("state") != "closed" or merged and created and merged < created:
        return None
    state = "merged" if is_merged else "draft" if value["state"] == "open" and value["draft"] else value["state"]
    return {"state": state, "created_at": created, "merged_at": merged}
