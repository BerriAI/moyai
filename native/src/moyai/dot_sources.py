"""Host-enforced provider scope for the verified discovery integration."""

import re
from urllib.parse import urlsplit

from app.automation_definitions import AttentionSource

from .connection_policy import ConnectionPolicy
from .contracts import Grant

GITHUB_MCP = "https://api.githubcopilot.com/mcp/readonly"
GITHUB_READS = frozenset({"list_pull_requests", "list_issues", "pull_request_read", "issue_read"})


def github_reader(grant: Grant) -> bool:
    policy = ConnectionPolicy.model_validate(grant.policy)
    return bool(grant.kind == "mcp" and policy.mcp and policy.mcp.url == GITHUB_MCP)


def repositories(grants: list[Grant], sources: list[dict[str, str]]) -> dict[str, list[tuple[str, str]]]:
    if not sources:
        return {}
    selected = {grant.id: grant for grant in grants}
    result: dict[str, list[tuple[str, str]]] = {}
    for value in sources:
        source = AttentionSource.model_validate(value)
        grant = selected.get(source.connection_id)
        if grant is None or not github_reader(grant) or not set(
            ConnectionPolicy.model_validate(grant.policy).allowed_tools or [],
        ).issubset(GITHUB_READS):
            raise ValueError("Restricted discovery requires the official GitHub read-only MCP with repository-bound read tools.")
        owner, repository = urlsplit(source.source).path.strip("/").split("/")
        result.setdefault(grant.id, []).append((owner.casefold(), repository.casefold()))
    if set(result) != set(selected):
        raise ValueError("Choose a repository for every restricted discovery account.")
    return result


def source_allowed(source: str, allowed: list[tuple[str, str]]) -> bool:
    parsed = urlsplit(source)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if parsed.netloc.casefold() == "api.github.com" and parts[:1] == ["repos"]:
        parts = parts[1:]
    elif parsed.netloc.casefold() != "github.com":
        return False
    return len(parts) >= 2 and (parts[0].casefold(), parts[1].casefold()) in allowed


def github_item(source: str) -> bool:
    return bool(re.fullmatch(
        r"https://(?:github\.com/[^/]+/[^/]+/(?:issues|pull)|api\.github\.com/repos/[^/]+/[^/]+/(?:issues|pulls))/[0-9]+",
        source,
    ))


def provider_partial(record: dict) -> bool:
    """Preserve explicit provider truncation signals, even in an empty page."""
    if any(record.get(key) is True for key in ("incomplete_results", "has_more", "hasMore", "truncated", "hasNextPage")):
        return True
    if any(record.get(key) not in (None, "", False) for key in ("next_cursor", "nextCursor", "next_page", "nextPage", "nextPageToken")):
        return True
    page = record.get("pageInfo") or record.get("page_info")
    if isinstance(page, dict) and provider_partial(page):
        return True
    collections = [value for key, value in record.items() if key in (
        "items", "results", "records", "issues", "pull_requests", "nodes", "edges", "data",
    ) and isinstance(value, list)]
    total = next((record[key] for key in ("total_count", "totalCount", "total") if isinstance(record.get(key), int)), None)
    return total is not None and bool(collections) and total > max(map(len, collections))
