"""Conservative preflight of default, classic-protected and ruleset branches."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from app.connector_errors import ConnectorError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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


def protection_response(value: object) -> Protection:
    """Distinguish provider refusal from an unverifiable successful response."""
    if isinstance(value, dict) and value.get("errors"):
        errors = value["errors"]
        items = [error for error in errors if isinstance(error, dict)] if isinstance(errors, list) else []
        for flag, explanation in (
            ("saml_failure", "GitHub reported a SAML restriction. Ask an organization owner to check the App's access under the organization's SSO policy."),
            ("ip_allow_list_failure", "The organization's IP allow list blocks the worker. Ask an organization owner to allow the worker's egress IP."),
        ):
            if any(isinstance(error.get("extensions"), dict) and error["extensions"].get(flag) is True for error in items):
                raise ToolFailed("GitHub denied branch-protection inspection. " + explanation)
        if any(error.get("type") == "RATE_LIMITED" for error in items):
            raise ToolFailed("GitHub rate-limited the branch-protection check. Retry after the rate limit resets; no permission change is indicated.")
        if any(error.get("type") == "FORBIDDEN" for error in items):
            raise ToolFailed(
                "GitHub denied branch-protection inspection (FORBIDDEN). Check the GitHub App's "
                "Administration: read permission and its approval on the installation. If already approved, "
                "ask an organization owner to check repository access and organization restrictions."
            )
        raise ToolFailed("GitHub returned an error while checking branch protection. Protection was not verified. Retry later; if it persists, ask the Moyai operator to investigate the GitHub integration.")
    try:
        return Protection.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        raise ToolFailed("GitHub returned incomplete or invalid branch-protection data. Protection was not verified. Retry later; if it persists, ask the Moyai operator to investigate the GitHub integration.") from None


def protection_failure(error: ConnectorError) -> ToolFailed:
    status = error.status_code
    if status == 401:
        action = "The App credential was rejected (HTTP 401). Ask a Moyai administrator to verify the GitHub App connection."
    elif status in {403, 404}:
        action = f"GitHub denied access (HTTP {status}). Ask an organization owner to check repository access, Administration: read approval, and organization restrictions."
    elif status == 429:
        action = "GitHub rate-limited the request (HTTP 429). Retry after the rate limit resets."
    elif status is not None and status >= 500:
        action = f"GitHub is unavailable (HTTP {status}). Retry later; no permission change is indicated."
    else:
        action = "No usable response was received. Retry later; if it persists, ask the Moyai operator to check GitHub connectivity."
    return ToolFailed("The branch-protection check failed. " + action)


async def require_writable(
    publisher: Publications, repo: RepositoryIdentity, token: str, branch: str, *, existing: bool,
) -> None:
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
    protection_token = await publisher.protection_token()
    owner, name = repo.full_name.split("/")
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        try:
            response = await publisher.request("POST", "/graphql", protection_token, json={
                "query": PROTECTION, "variables": {"owner": owner, "name": name, "cursor": cursor},
            })
        except ConnectorError as error:
            raise protection_failure(error) from None
        page = protection_response(response).data.repository
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
