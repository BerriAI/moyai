"""Closed GitLab API vocabulary. No arbitrary REST/GraphQL or quick actions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote

from app.gitlab_graphql import REQUEST_CHANGES, REVIEW_CAPABILITY
from pydantic_ai.exceptions import ToolFailed

from .gitlab_workflow_types import safe_text

__all__ = ["REQUEST_CHANGES", "REVIEW_CAPABILITY", "validate_request"]

READS = (
    r"", r"/repository/tree", r"/repository/files/[^/?#]+",
    r"/repository/branches(?:/[^?#]+)?", r"/protected_branches",
    r"/repository/commits(?:/[^/?#]+(?:/diff)?)?", r"/repository/compare", r"/search",
    r"/issues(?:/[1-9][0-9]*(?:/notes)?)?",
    r"/merge_requests(?:/[1-9][0-9]*(?:/(?:diffs|commits|notes|discussions|blocks|pipelines))?)?",
    r"/merge_requests/[1-9][0-9]*/draft_notes(?:/[1-9][0-9]*)?",
    r"/pipelines(?:/[1-9][0-9]*(?:/jobs)?)?",
    r"/jobs/[1-9][0-9]*(?:/(?:trace|artifacts(?:/[^?#]+)?))?",
)


def validate_request(method: str, path: str, project: int, body: object = None) -> None:
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        raise ToolFailed("Invalid GitLab request path.") from None
    if (
        any(c in path for c in "?#\\") or "\\" in decoded
        or re.search(r"%(?![a-fA-F0-9]{2})", path)
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ToolFailed("Invalid GitLab request path.")
    if path == "/graphql":
        if method == "POST" and isinstance(body, Mapping):
            if body == {"query": REVIEW_CAPABILITY}:
                return
            if set(body) == {"query", "variables"} and body["query"] == REQUEST_CHANGES:
                variables = body["variables"]
                if isinstance(variables, Mapping) and set(variables) == {"projectPath", "iid"} and (
                    isinstance(variables["projectPath"], str)
                    and re.fullmatch(r"[1-9][0-9]*", str(variables["iid"]))
                ):
                    return
        raise ToolFailed("Only the fixed GitLab request-changes mutation is available.")
    prefix = f"/projects/{project}"
    if not path.startswith(prefix):
        raise ToolFailed("GitLab requests must target the selected immutable project ID.")
    suffix = path[len(prefix):]
    if method == "GET" and any(re.fullmatch(pattern, suffix) for pattern in READS):
        return
    if not isinstance(body, Mapping):
        raise ToolFailed("GitLab mutations require a typed object.")
    for field in ("description", "title", "note", "body"):
        if field in body:
            try:
                if not isinstance(body[field], str):
                    raise TypeError()
                safe_text(body[field])
            except (ValueError, TypeError):
                raise ToolFailed("GitLab quick actions are unavailable.") from None
    mr = r"/merge_requests/[1-9][0-9]*"
    allowed = False
    if method == "POST" and suffix == "/merge_requests":
        allowed = set(body) == {"source_branch", "target_branch", "title", "description",
                                "remove_source_branch", "allow_collaboration"} and (
            body["remove_source_branch"] is False and body["allow_collaboration"] is False
            and not re.match(r"(?i)^\s*(?:draft|wip)\b|^\s*[\[(](?:draft|wip)\b", str(body["title"]))
        )
    elif method == "PUT" and re.fullmatch(mr, suffix):
        allowed = (
            (set(body) == {"state_event"} and body["state_event"] in {"close", "reopen"})
            or (set(body) == {"target_branch"} and isinstance(body["target_branch"], str))
            or (set(body) == {"title"} and str(body["title"]).startswith("Draft: "))
        )
    elif method == "POST" and re.fullmatch(mr + "/draft_notes", suffix):
        allowed = "note" in body and not set(body) - {"note", "position", "commit_id"}
    elif method == "PUT" and re.fullmatch(mr + "/draft_notes/[1-9][0-9]*", suffix):
        allowed = set(body) == {"note"}
    elif method == "DELETE" and re.fullmatch(mr + "/draft_notes/[1-9][0-9]*", suffix) or method == "PUT" and re.fullmatch(mr + "/draft_notes/[1-9][0-9]*/publish", suffix):
        allowed = not body
    elif method == "POST" and re.fullmatch(mr + "/blocks", suffix):
        allowed = set(body) == {"blocking_merge_request_id"} and type(body["blocking_merge_request_id"]) is int
    if not allowed:
        raise ToolFailed("GitLab approval, merge, branch deletion, bulk review publication and arbitrary API writes are unavailable.")
