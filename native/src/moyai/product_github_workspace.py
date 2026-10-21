"""Translate a staged workspace tree into the existing checked publication path."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed

from .contracts import RunScope
from .github_workflow_types import PublishBranch, WorkspacePublishBranch
from .product_github_bootstrap import fetch_bundle

if TYPE_CHECKING:
    from .product_github_publication import Publications


async def import_base(
    publisher: Publications, ctx: RunContext[RunScope], request: WorkspacePublishBranch,
) -> None:
    from .product_github_workflows import Workflows

    credentials = await publisher.service.credentials(publisher.scope, publisher.grant)
    workflows = Workflows(publisher)
    repo = await workflows.repository(credentials["access_token"])
    if await workflows.commit_at(credentials["access_token"], request.base_sha) != request.base_sha:
        raise ToolFailed("GitHub did not confirm the exact publication base.")
    try:
        with tempfile.TemporaryDirectory(prefix="moyai-github-base-") as temporary:
            bundle = await fetch_bundle(
                "https://github.com/" + repo.full_name + ".git", "moyai-base/" + request.base_sha,
                request.base_sha, credentials["access_token"], Path(temporary) / "base.bundle",
            )
    except (ValueError, OSError, TimeoutError):
        raise ToolFailed("Could not retrieve the publication base commit. Check repository access and retry.") from None
    await publisher.service.check(publisher.scope, publisher.grant)
    remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
    await ctx.workspace.write_bytes(remote, bundle)
    script = Path(__file__).with_name("github_workspace_import.py").read_text()
    result = await ctx.workspace.run([
        "python3", "-I", "-c", script, request.directory, remote,
        hashlib.sha256(bundle).hexdigest(), request.base_sha,
    ], timeout=180)
    if result.exit_code:
        raise ToolFailed("Could not import the publication base. Check the checkout directory and available disk space.")
    await publisher.service.check(publisher.scope, publisher.grant)


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
        try:
            missing_base = json.loads(result.stdout).get("code") == "missing_base"
        except (ValueError, AttributeError):
            missing_base = False
        if missing_base:
            await import_base(publisher, ctx, request)
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
