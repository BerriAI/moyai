"""Host-enforced provider scope for the verified discovery integration."""

import re
from urllib.parse import urlsplit, urlunsplit

from app.automation_definitions import AttentionSource

from .connection_policy import ConnectionPolicy
from .contracts import Grant

GITHUB_MCP = "https://api.githubcopilot.com/mcp/readonly"
GITHUB_READS = frozenset({"list_pull_requests", "list_issues", "pull_request_read", "issue_read"})
# issue_read.get also enriches hierarchy data from potentially foreign repos.
# Keep restricted reads to reviewed variants that stay within their repository.
GITHUB_METHODS = {
    "pull_request_read": frozenset({"get", "get_comments"}),
    "issue_read": frozenset({"get_comments"}),
}
MORE_FIELDS = (
    "incomplete_results", "has_more", "hasMore", "truncated", "hasNextPage", "has_next_page",
    "partial", "page_truncated", "records_truncated", "headers_incomplete", "snippet_truncated",
)


def google_reader(grant: Grant) -> bool:
    from app.gmail_tools import READS as GMAIL_READS
    from app.google_calendar_tools import READS as CALENDAR_READS

    reads = {"gmail": GMAIL_READS, "google_calendar": CALENDAR_READS}.get(grant.kind)
    policy = ConnectionPolicy.model_validate(grant.policy)
    return bool(
        reads and grant.read_only and policy.oauth and policy.product is None
        and policy.allowed_tools and set(policy.allowed_tools).issubset(
            {name.removeprefix(grant.kind + "_") for name in reads}
        )
    )


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
    if any(
        identity not in result and not google_reader(grant)
        for identity, grant in selected.items()
    ):
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


def github_record(record: dict) -> dict:
    """Normalize provider URLs without manufacturing repository identity."""
    result = dict(record)
    # GitHub can follow repository renames. Request arguments do not establish
    # the returned repository, so number-only issue rows remain unverified.
    # Repository names are case-insensitive. Use one identity across provider
    # URLs even when subsequent calls spell them differently.
    for key in ("html_url", "web_url", "permalink", "url"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        parts = parsed.path.split("/")
        if parsed.scheme == "https" and parsed.netloc.casefold() == "github.com" and len(parts) >= 3:
            parts[1:3] = [part.casefold() for part in parts[1:3]]
            result[key] = urlunsplit(parsed._replace(netloc="github.com", path="/".join(parts)))
    return result


def provider_partial(record: dict) -> bool:
    """Preserve explicit provider truncation signals, even in an empty page."""
    if any(record.get(key) is True for key in MORE_FIELDS):
        return True
    if any(record.get(key) is False for key in ("complete", "content_complete", "page_complete")):
        return True
    if any(record.get(key) not in (None, "", False) for key in ("next_cursor", "nextCursor", "next_page", "nextPage", "nextPageToken")):
        return True
    if any(
        isinstance(record.get(key), dict) and provider_partial(record[key])
        for key in ("pageInfo", "page_info", "coverage")
    ):
        return True
    collections = [value for key, value in record.items() if key in (
        "items", "results", "records", "issues", "pull_requests", "nodes", "edges", "data",
    ) and isinstance(value, list)]
    total = next((record[key] for key in ("total_count", "totalCount", "total") if isinstance(record.get(key), int)), None)
    return total is not None and bool(collections) and total > max(map(len, collections))


def provider_complete(record: dict) -> bool:
    """An explicit terminal page, never inferred from a short or empty array."""
    if provider_partial(record):
        return False
    if record.get("complete") is True or record.get("content_complete") is True:
        return True
    if any(record.get(key) is False for key in ("has_more", "hasMore", "hasNextPage", "has_next_page")):
        return True
    return any(
        isinstance(record.get(key), dict) and provider_complete(record[key])
        for key in ("pageInfo", "page_info", "coverage")
    )
