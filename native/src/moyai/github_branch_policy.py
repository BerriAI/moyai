"""Conservative preflight of default, classic-protected and ruleset branches."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.exceptions import ToolFailed

from .github_branch_patterns import classic_pattern_matches
from .github_request_policy import PROTECTION
from .github_workflow_types import RepositoryIdentity

if TYPE_CHECKING:
    from .product_github_publication import Publications


class Page(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    hasNextPage: bool
    endCursor: str | None


class Rule(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    pattern: str = Field(min_length=1, max_length=1024)


class Rules(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    nodes: list[Rule] = Field(max_length=100)
    pageInfo: Page


class ProtectionRepository(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    databaseId: int
    branchProtectionRules: Rules


class ProtectionData(BaseModel):
    repository: ProtectionRepository


class Protection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    data: ProtectionData


async def require_writable(
    publisher: Publications, repo: RepositoryIdentity, token: str, branch: str, *, existing: bool,
) -> None:
    from .product_github_workflows import parse
    if branch == repo.default_branch:
        raise ToolFailed("The default branch is not a publication target.")
    prefix = f"/repositories/{repo.id}"
    value = await publisher.request("GET", prefix + "/branches/" + quote(branch, safe=""), token, missing=True)
    if value is not None and (value.get("name") != branch or value.get("protected") is not False):
        raise ToolFailed("The selected branch is protected or its protection status is unavailable.")
    if existing and value is None:
        raise ToolFailed("The selected branch no longer exists.")
    rules = await publisher.request("GET", prefix + "/rules/branches/" + quote(branch, safe=""), token)
    if rules != []:
        raise ToolFailed("Repository rules restrict this branch. Choose an unrestricted feature branch.")
    if value is not None:
        return
    owner, name = repo.full_name.split("/")
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        page = parse(Protection, await publisher.request("POST", "/graphql", token, json={
            "query": PROTECTION, "variables": {"owner": owner, "name": name, "cursor": cursor},
        })).data.repository
        if page.databaseId != repo.id:
            raise ToolFailed("Branch protection belongs to a different repository.")
        for rule in page.branchProtectionRules.nodes:
            try:
                matches = classic_pattern_matches(rule.pattern, branch)
            except ValueError:
                raise ToolFailed("A classic protection pattern could not be evaluated safely.") from None
            if matches:
                raise ToolFailed("A classic protection rule may cover this branch. Choose an unrestricted name.")
        info = page.branchProtectionRules.pageInfo
        if not info.hasNextPage:
            return
        if not info.endCursor or info.endCursor in seen:
            break
        seen.add(info.endCursor)
        cursor = info.endCursor
    raise ToolFailed("Complete branch protection could not be verified.")
