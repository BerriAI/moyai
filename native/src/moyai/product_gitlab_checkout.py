"""GitLab checkout/staged export; sandbox receives objects without credentials."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.github import Change
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.exceptions import ToolFailed

from .gitlab_transport import fetch_bundle, remote_url
from .gitlab_workflow_types import Changes, Checkout
from .product_github_checkout import CHECKOUT_INSTALL

if TYPE_CHECKING:
    from pydantic_ai import RunContext

    from .contracts import RunScope
    from .product_gitlab_publication import Publications


async def trusted_bundle(p: Publications, commit: str, branch: str) -> tuple[bytes, str]:
    credentials = await p.credentials()
    project = await p.project()
    with tempfile.TemporaryDirectory(prefix="moyai-gitlab-checkout-") as temporary:
        async def execute(bound):
            # The invocation-local provider wrapper rechecks current authority.
            value = await bound.gitlab.request(
                "GET", p.prefix + "/repository/commits/" + commit,
                token=credentials["access_token"], base_url=p.origin,
            )
            if not isinstance(value, dict) or value.get("id") != commit:
                raise ToolFailed("GitLab did not confirm the checkout commit.")
            return await fetch_bundle(
                base_url=p.origin, project_path=project.path_with_namespace, commit=commit,
                branch=branch, token=credentials["access_token"], destination=Path(temporary) / "source.bundle", allow_loopback_http=p.allow_loopback_http,
            )
        bundle = await p.service.execution.call(
            execute, authorize=lambda: p.service.check(p.scope, p.grant), recheck=True,
        )
    await p.service.check(p.scope, p.grant)
    return bundle, remote_url(p.origin, project.path_with_namespace)


async def checkout(p: Publications, ctx: RunContext[RunScope], request: Checkout) -> dict:
    from .product_gitlab_workflows import Workflows
    await p.check_target(request)
    w, project = Workflows(p), await p.project()
    if request.number is not None:
        mr = await w.read_mr(request.number)
        commit = mr.sha
    else:
        commit = await w.commit_at(request.ref or project.default_branch)
    branch = "moyai-checkout/" + commit[:16]
    bundle, url = await trusted_bundle(p, commit, branch)
    receipt = {
        **p.identity_scope, "repository_id": p.target, "repository": project.path_with_namespace,
        "commit": commit, "directory": request.directory, "ref": request.ref, "number": request.number,
    }
    marker = p.database.security.object_id(json.dumps({
        "scope": p.scope.workspace_key, "connection": p.grant.id, "revision": p.grant.revision,
        "receipt": receipt,
    }, sort_keys=True).encode())
    remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
    await ctx.workspace.write_bytes(remote, bundle)
    result = await ctx.workspace.run(["python3", "-I", "-c", CHECKOUT_INSTALL, json.dumps({
        "directory": request.directory, "bundle": remote, "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "marker": marker, "branch": branch, "commit": commit, "url": url, "receipt": receipt,
    })], timeout=240)
    if result.exit_code or json.loads(result.stdout) != receipt:
        raise ToolFailed("GitLab checkout did not complete; existing workspace files were not replaced.")
    await p.service.check(p.scope, p.grant)
    return receipt


async def staged_files(p: Publications, ctx: RunContext[RunScope], request: Changes) -> list[Change]:
    directory = request.directory
    if directory is None:
        raise ToolFailed("A staged publication requires a workspace directory.")
    script = Path(__file__).with_name("github_workspace_export.py").read_text()
    async def export():
        await p.service.check(p.scope, p.grant)
        return await ctx.workspace.run(
            ["python3", "-I", "-c", script, directory, request.base_sha], timeout=120,
        )
    result = await export()
    if result.exit_code:
        try:
            missing = json.loads(result.stdout).get("code") == "missing_base"
        except (ValueError, AttributeError):
            missing = False
        if missing:
            bundle, _ = await trusted_bundle(p, request.base_sha, "moyai-base/" + request.base_sha)
            remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
            await ctx.workspace.write_bytes(remote, bundle)
            imported = await ctx.workspace.run([
                "python3", "-I", "-c", Path(__file__).with_name("github_workspace_import.py").read_text(),
                directory, remote, hashlib.sha256(bundle).hexdigest(), request.base_sha,
            ], timeout=180)
            if imported.exit_code:
                raise ToolFailed("Could not import the exact publication base without changing the checkout.")
            result = await export()
    if result.exit_code:
        raise ToolFailed("Could not export staged files. Check base SHA, conflicts, symlinks, size and staging.")
    try:
        files = TypeAdapter(list[Change]).validate_python(json.loads(result.stdout)["files"])
        # Revalidate aggregate path/size constraints on untrusted sandbox output.
        Changes.model_validate({
            **request.model_dump(exclude={"directory", "files", "branch", "expected_head_sha", "number",
                                          "base_branch", "head_branch", "body"}),
            "files": [item.model_dump() for item in files],
        })
    except (ValidationError, KeyError, TypeError, ValueError):
        raise ToolFailed("Staged files contain invalid, excessive or disallowed publication paths.") from None
    await p.service.check(p.scope, p.grant)
    return files
