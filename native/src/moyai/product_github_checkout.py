"""Native workspace checkout using credential-free Git bundles."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed

from .contracts import RunScope
from .github_workflow_types import Checkout, PullRequest
from .product_github_bootstrap import INSTALL, fetch_bundle
from .product_github_workflows import Workflows, parse

if TYPE_CHECKING:
    from .product_github_publication import Publications

CHECKOUT_INSTALL = INSTALL.replace(
    'target = pathlib.Path("/workspace/repo")',
    'target = pathlib.Path(data["directory"])\n'
    'if target.parent != pathlib.Path("/workspace") or target.name in {".", "..", ".git"}:\n'
    '    raise ValueError("Invalid checkout directory.")',
)


async def checkout(
    publisher: Publications, ctx: RunContext[RunScope], request: Checkout,
) -> dict[str, JsonValue]:
    await publisher.check_target(request)
    credentials = await publisher.service.credentials(publisher.scope, publisher.grant)
    token = credentials["access_token"]
    workflows = Workflows(publisher)
    repo = await workflows.repository(token)
    if request.number is not None:
        pr = parse(PullRequest, await publisher.request(
            "GET", workflows.prefix + f"/pulls/{request.number}", token,
        ))
        if pr.number != request.number or pr.base.repo.id != repo.id:
            raise ToolFailed("GitHub returned a different PR.")
        commit, branch = pr.head.sha, f"moyai-checkout/pr-{request.number}"
    else:
        commit = await workflows.commit_at(token, request.ref or repo.default_branch)
        branch = "moyai-checkout/" + commit[:16]
    url = "https://github.com/" + repo.full_name + ".git"
    with tempfile.TemporaryDirectory(prefix="moyai-github-checkout-") as temporary:
        bundle = await fetch_bundle(url, branch, commit, token, Path(temporary) / "source.bundle")
    await publisher.service.check(publisher.scope, publisher.grant)
    receipt: dict[str, JsonValue] = {
        "repository_id": repo.id, "repository": repo.full_name, "commit": commit,
        "directory": request.directory, "ref": request.ref, "number": request.number,
    }
    marker = publisher.database.security.object_id(json.dumps({
        "scope": publisher.scope.workspace_key, "connection": publisher.grant.id,
        "revision": publisher.grant.revision, "receipt": receipt,
    }, sort_keys=True).encode())
    remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
    await ctx.workspace.write_bytes(remote, bundle)
    command = ["python3", "-I", "-c", CHECKOUT_INSTALL, json.dumps({
        "directory": request.directory, "bundle": remote, "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "marker": marker, "branch": branch, "commit": commit, "url": url, "receipt": receipt,
    })]
    result = await ctx.workspace.run(command, timeout=240)
    if result.exit_code:
        raise ToolFailed("Checkout did not complete; existing work was not replaced.")
    if json.loads(result.stdout) != receipt:
        raise ToolFailed("The installed checkout receipt differs from the selected repository and commit.")
    await publisher.service.check(publisher.scope, publisher.grant)
    return receipt
