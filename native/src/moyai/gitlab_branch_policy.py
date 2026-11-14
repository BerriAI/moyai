"""Fail-closed effective GitLab branch protection, including prospective names."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic_ai.exceptions import ToolFailed

from .gitlab_workflow_types import BranchInfo, Project

if TYPE_CHECKING:
    from .product_gitlab_publication import Publications


def pattern_matches(pattern: str, branch: str) -> bool:
    # GitLab documents '*' wildcard matching, including path separators.
    # Unknown metacharacters cannot be silently interpreted as non-matches.
    if not pattern or len(pattern) > 1024 or any(c in "?[]\\{}" or ord(c) < 32 for c in pattern):
        raise ToolFailed("A GitLab protection pattern could not be safely evaluated.")
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), branch) is not None


async def require_writable(p: Publications, repo: Project, branch: str, *, existing: bool) -> BranchInfo | None:
    from .product_gitlab_publication import parse

    if branch == repo.default_branch:
        raise ToolFailed("The GitLab default branch cannot be a publication target.")
    value = await p.request("GET", p.prefix + "/repository/branches/" + quote(branch, safe=""), missing=True)
    current = parse(BranchInfo, value) if value is not None else None
    if current is not None and (current.name != branch or current.protected):
        raise ToolFailed("The GitLab branch is protected or its identity changed.")
    if existing and current is None:
        raise ToolFailed("The selected GitLab branch no longer exists.")
    for page in range(1, 101):
        rules = await p.request("GET", p.prefix + "/protected_branches", params={"page": page, "per_page": 100})
        if not isinstance(rules, list) or len(rules) > 100:
            raise ToolFailed("Complete GitLab branch protection is unavailable.")
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("name"), str):
                raise ToolFailed("GitLab returned incomplete branch protection.")
            # Includes inherited group rules: https://docs.gitlab.com/api/protected_branches/#list-protected-branches
            if pattern_matches(rule["name"], branch):
                raise ToolFailed("A GitLab protected, wildcard or inherited rule covers this branch.")
        if len(rules) < 100:
            return current
    raise ToolFailed("Complete GitLab branch protection exceeds the inspection limit.")
