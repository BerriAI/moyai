"""Confirmed native PR/MR creation receipts, independent of attachments.

The receipt identity and state/date validation preserve the semantics of
app/pr_analytics.py and app/session_pull_requests.py without importing their
runtime, credentials, or database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import BigInteger, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from .analytics import utc
from .contracts import Grant, RunScope
from .gitlab_reviews import mr_identity, project_reference
from .links import gitlab_link
from .models import Base, Effect, Turn
from .pull_requests import parse_pr_url

SHARED_CREATIONS = frozenset({
    "github_create_pull_request", "github_open_pull_request",
    "gitlab_create_merge_request", "gitlab_open_merge_request",
})
logger = logging.getLogger(__name__)


def scoped_creations(connection_id: str, provider: str = "github") -> frozenset[str]:
    if provider == "gitlab":
        return frozenset({
            f"gitlab_{connection_id[:8]}_create_merge_request",
            f"gitlab_{connection_id[:8]}_open_merge_request",
        })
    return frozenset({
        f"github_{connection_id[:8]}_create_pull_request",
        f"github_{connection_id[:8]}_open_pull_request",
    })


def matches_creation(grant: Grant, tool_name: str, connection_id: str | None) -> bool:
    if grant.kind not in {"github", "gitlab"} or not tool_name.startswith(grant.kind + "_"):
        return False
    if tool_name in scoped_creations(grant.id, grant.kind):
        return True
    if grant.id != connection_id:
        return False
    return tool_name in SHARED_CREATIONS


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
    provider: str = "github"
    origin: str = "https://github.com"
    project_id: int | None = None
    merge_request_id: int | None = None


def positive_id(value: Any) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63 else None


def creation_receipt(
    scope: RunScope, tool_name: str, args: dict[str, Any], result: Any, confirmed_at: str,
    *, connection_id: str | None = None,
) -> CreationReceipt | None:
    """Only a successful exact creation tool and its selected grant can attest creation."""
    selected = [
        grant for grant in scope.grants
        if matches_creation(grant, tool_name, connection_id)
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
    grant = selected[0]
    if grant.kind == "gitlab":
        return gitlab_creation_receipt(grant, args, value, confirmed_at)
    url = value.get("html_url") or value.get("url")
    if not isinstance(url, str):
        return None
    try:
        pr = parse_pr_url(url)
    except HTTPException:
        return None
    if pr.provider != "github":
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


def gitlab_creation_receipt(
    grant: Grant, args: dict[str, Any], value: dict[str, Any], confirmed_at: str,
) -> CreationReceipt | None:
    """A canonical grant and a complete provider acknowledgment attest MR creation."""
    try:
        origin, project_id = project_reference(grant)
        pr = parse_pr_url(value["web_url"])
    except (HTTPException, PermissionError, ValueError, TypeError, KeyError):
        return None
    request = args.get("request", args)
    if not isinstance(request, dict):
        return None
    requested_id = request.get("repository_id")
    requested_name = request.get("repository")
    if (
        not grant.policy.get("product") or pr.provider != "gitlab"
        or not mr_identity(value, project_id, pr.number, origin, pr.repository)
        or value["draft"] or value["state"] != "opened"
        or type(value.get("source_project_id")) is not int or type(value.get("target_project_id")) is not int
        or value.get("source_project_id") != project_id or value.get("target_project_id") != project_id
        or (requested_id is None and not requested_name)
        or (requested_id is not None and (type(requested_id) is not int or requested_id != project_id))
        or (requested_name and requested_name != pr.repository)
    ):
        return None
    created = utc(value.get("created_at"), require_timezone=True)
    confirmed = utc(confirmed_at, require_timezone=True)
    if confirmed is None or value.get("created_at") is not None and created is None:
        return None
    return CreationReceipt(
        grant.id, grant.revision, pr.repository, pr.url, pr.number,
        None, None, confirmed.isoformat(), created.isoformat() if created else None,
        "gitlab", origin, project_id, value["id"],
    )


def publication_identity(receipt: CreationReceipt) -> str:
    """Preserve old GitHub URL hashes; MR hashes bind immutable, origin-scoped IDs."""
    if receipt.provider == "gitlab":
        if receipt.project_id is None:
            raise ValueError("An MR receipt needs its verified project ID.")
        return f"gitlab:{receipt.origin}:{receipt.project_id}:{receipt.number}"
    return receipt.url.casefold()


async def persist_publication(database: Any, scope: RunScope, identity: str, receipt: CreationReceipt) -> None:
    """Project already-confirmed evidence; never authorize or repeat an external write."""
    async with database.sessions.begin() as db:
        effect = await db.get(Effect, identity)
        creation_tools = scoped_creations(receipt.connection_id, receipt.provider) | {
            name for name in SHARED_CREATIONS if name.startswith(receipt.provider + "_")
        }
        if (
            effect is None or effect.state != "confirmed" or effect.session_id != scope.session_id
            or effect.actor_id != scope.actor_id or effect.turn_id != scope.turn_id
            or effect.tool_name not in creation_tools
        ):
            raise ValueError("A matching confirmed native effect is required.")
        if effect.tool_name in SHARED_CREATIONS or receipt.provider == "gitlab":
            from .product_github_models import ProductGitOperation

            kind = effect.tool_name.removeprefix("github_")
            if receipt.provider == "gitlab":
                kind = "gl_create_mr" if effect.tool_name.endswith("_create_merge_request") else "gl_open_mr"
            operation = await db.scalar(select(ProductGitOperation).where(
                ProductGitOperation.original_effect_id == identity,
                ProductGitOperation.actor_id == scope.actor_id,
                ProductGitOperation.session_id == scope.session_id,
                ProductGitOperation.turn_id == scope.turn_id,
                ProductGitOperation.kind == kind,
                ProductGitOperation.connection_id == receipt.connection_id,
                ProductGitOperation.connection_revision == receipt.connection_revision,
                ProductGitOperation.repository_id == (
                    receipt.project_id if receipt.provider == "gitlab" else receipt.repository_id
                ),
            ))
            if operation is None:
                raise ValueError("A creation receipt requires its matching provider operation.")
            if receipt.provider == "gitlab":
                document = json.loads(database.security.unseal(operation.document))
                confirmed = document.get("result")
                if document.get("provider_scope") != {
                    "provider": "gitlab", "origin": receipt.origin, "project_id": receipt.project_id,
                } or not isinstance(confirmed, dict) or (
                    confirmed.get("id") != receipt.merge_request_id
                    or confirmed.get("iid") != receipt.number
                    or confirmed.get("project_id") != receipt.project_id
                    or confirmed.get("web_url") != receipt.url
                    or confirmed.get("draft") is not False
                ):
                    raise ValueError("The MR receipt differs from the confirmed operation.")
        fields = asdict(receipt)
        target = {"url": fields.pop("url"), "repository": fields.pop("repository")}
        provider = {key: fields.pop(key) for key in ("provider", "origin", "project_id", "merge_request_id")}
        if receipt.provider == "gitlab":
            target.update(provider)
        insert = postgres_insert if database.settings.postgres_url else sqlite_insert
        await db.execute(insert(PRPublication).values(
            id=identity, actor_id=scope.actor_id, session_id=scope.session_id, turn_id=scope.turn_id,
            model=scope.model, url_key=database.security.object_id(publication_identity(receipt).encode()),
            target=database.security.seal(target), **fields,
        ).on_conflict_do_nothing(index_elements=["id"]))


async def project_gitlab_creation(database: Any, scope: RunScope, operation_id: str) -> bool:
    """Project finish evidence even if tool delivery was lost or a later turn resumes it.

    Call after the publisher's finish transaction commits. Analytics failure must
    never turn an acknowledged provider write into a failed/retryable operation.
    """
    from .product_github_models import ProductGitOperation

    try:
        async with database.sessions() as db:
            operation = await db.get(ProductGitOperation, operation_id)
            if operation is None or operation.kind not in {"gl_create_mr", "gl_open_mr"}:
                return False
            if operation.actor_id != scope.actor_id or operation.session_id != scope.session_id:
                return False
            effect = await db.get(Effect, operation.original_effect_id)
            turn = await db.get(Turn, operation.turn_id)
            if effect is None or effect.state != "confirmed" or not effect.settled_at or turn is None:
                return False
            value = json.loads(database.security.unseal(operation.document)).get("result")
            original = replace(scope, turn_id=operation.turn_id, model=turn.model)
            receipt = creation_receipt(
                original, effect.tool_name, {"repository_id": operation.repository_id},
                value, effect.settled_at, connection_id=operation.connection_id,
            )
            if receipt is None or receipt.provider != "gitlab":
                return False
        await persist_publication(database, original, operation.original_effect_id, receipt)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Confirmed MR analytics receipt was not saved; coverage is incomplete.")
        return False


def verified_status(
    receipt: PRPublication, value: Any, target: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate GitHub identity and aware dates before attributing any merge."""
    if target and target.get("provider") == "gitlab":
        return verified_gitlab_status(receipt, value, target)
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


def verified_gitlab_status(
    receipt: PRPublication, value: Any, target: dict[str, Any],
) -> dict[str, Any] | None:
    project = target.get("project_id")
    global_id = target.get("merge_request_id")
    try:
        origin, repository, _ = gitlab_link(value["web_url"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if (
        type(project) is not int or project <= 0 or type(global_id) is not int or global_id <= 0
        or origin != target.get("origin")
        or not mr_identity(value, project, receipt.number, origin, repository)
        or value["id"] != global_id
    ):
        return None
    created = utc(value.get("created_at"), require_timezone=True)
    merged = utc(value.get("merged_at"), require_timezone=True)
    if (
        created is None or value.get("merged_at") and merged is None
        or merged and (value["state"] != "merged" or merged < created)
    ):
        return None
    state = "open" if value["state"] in {"opened", "locked"} else value["state"]
    if state == "open" and value["draft"]:
        state = "draft"
    return {"state": state, "created_at": created, "merged_at": merged}
