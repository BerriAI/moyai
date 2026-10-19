"""Translate a staged workspace tree into the existing checked publication path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed

from .contracts import RunScope
from .github_workflow_types import PublishBranch, WorkspacePublishBranch

if TYPE_CHECKING:
    from .product_github_publication import Publications


async def staged_request(
    publisher: Publications, ctx: RunContext[RunScope], request: WorkspacePublishBranch,
) -> PublishBranch:
    await publisher.check_target(request)
    await publisher.service.check(publisher.scope, publisher.grant)
    if request.expected_head_sha and request.expected_head_sha != request.base_sha:
        raise ToolFailed("Branch updates must use the exact expected head as base_sha.")
    script = Path(__file__).with_name("github_workspace_export.py").read_text()
    result = await ctx.workspace.run(
        ["python3", "-I", "-c", script, request.directory, request.base_sha], timeout=120,
    )
    if result.exit_code:
        raise ToolFailed("Could not export the staged checkout. Check the directory, base SHA, conflicts, symlinks, and size; stage the intended files first.")
    try:
        payload = json.loads(result.stdout)
        resolved = PublishBranch.model_validate({
            **request.model_dump(exclude={"directory"}), "files": payload["files"],
        })
    except (ValueError, KeyError, TypeError, ValidationError):
        raise ToolFailed("The staged checkout contains invalid or disallowed publication paths.") from None
    await publisher.service.check(publisher.scope, publisher.grant)
    return resolved
