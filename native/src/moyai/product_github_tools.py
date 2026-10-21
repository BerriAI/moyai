"""Model-visible native workflow tools; schemas stay owned by Pydantic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .contracts import RunScope
from .github_workflow_types import (
    AddToStack,
    Checkout,
    CreateBranch,
    CreateReview,
    CreateStack,
    EditReview,
    OpenPullRequest,
    PendingReview,
    PRWrite,
    PublishBranch,
    Rebase,
    Retarget,
    StackList,
    StackRead,
    SubmitReview,
    WorkspacePublishBranch,
)
from .product_github_workflows import Workflows

if TYPE_CHECKING:
    from .product_github_publication import Publications

READ = {"annotations": {"readOnlyHint": True}}
WRITE = {"annotations": {"readOnlyHint": False}}


def register(tools: FunctionToolset[RunScope], publisher: Publications) -> None:
    workflows = Workflows(publisher)

    @tools.tool(metadata=READ, sequential=True)
    async def checkout(ctx: RunContext[RunScope], request: Checkout) -> dict[str, JsonValue]:
        """Check out a selected branch/SHA or PR into a fresh /workspace directory.

        Existing files are never replaced. GitHub credentials remain on the trusted worker.
        """
        from .product_github_checkout import checkout as install
        return await install(publisher, ctx, request)

    @tools.tool(metadata=READ)
    async def stacks(ctx: RunContext[RunScope], request: StackList) -> dict[str, JsonValue]:
        """List GitHub stacks in the selected repository. Follow next_page."""
        from .product_github_stacks import Stacks
        return await Stacks(publisher).read(request)

    @tools.tool(metadata=READ)
    async def stack(ctx: RunContext[RunScope], request: StackRead) -> dict[str, JsonValue]:
        """Read a stack with full ordered member identities and its revision."""
        from .product_github_stacks import Stacks
        return await Stacks(publisher).read(request)

    if publisher.grant.read_only:
        return

    @tools.tool(metadata=WRITE, sequential=True)
    async def create_branch(ctx: RunContext[RunScope], request: CreateBranch) -> dict[str, JsonValue]:
        """Create a new unrestricted branch at an exact commit, without a PR. Never overwrite an existing branch."""
        return await workflows.create_branch(ctx, request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def publish_branch(ctx: RunContext[RunScope], request: PublishBranch | WorkspacePublishBranch) -> dict[str, JsonValue]:
        """Publish files without opening a PR. Omit expected_head_sha to create a new named branch.

        Updating an existing branch requires acknowledged Moyai ownership and its exact current
        head as both base_sha and expected_head_sha. The update is atomic and never forced.
        For local checkout changes, stage the intended files with git add and pass directory
        instead of files. The worker reads the staged tree; do not copy file contents into the call.
        """
        return await workflows.publish_branch(ctx, request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def open_pull_request(ctx: RunContext[RunScope], request: OpenPullRequest) -> dict[str, JsonValue]:
        """Open a ready-for-review PR from an existing exact head to a chosen base.

        Creating this PR does not grant ownership or write permission over its head branch.
        """
        return await workflows.open_existing(ctx, request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def rebase_pull_request(ctx: RunContext[RunScope], request: Rebase) -> dict[str, JsonValue]:
        """Publish one prepared commit on the PR's current base with atomic expected-head fencing.

        Supply files containing the complete intended changes relative to base_sha.
        Preserves the PR identity; cannot write protected/default branches or replay an uncertain write.
        """
        return await workflows.rebase(ctx, request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def close_pull_request(ctx: RunContext[RunScope], request: PRWrite) -> dict[str, JsonValue]:
        """Close an authorized PR at its exact head when requested. Never merge or delete its branch."""
        return await workflows.lifecycle(ctx, "close_pull_request", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def reopen_pull_request(ctx: RunContext[RunScope], request: PRWrite) -> dict[str, JsonValue]:
        """Reopen an authorized, unmerged PR at its exact head."""
        return await workflows.lifecycle(ctx, "reopen_pull_request", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def retarget_pull_request(ctx: RunContext[RunScope], request: Retarget) -> dict[str, JsonValue]:
        """Change an authorized PR's base branch. Retargeting invalidates prior external-PR consent."""
        return await workflows.lifecycle(ctx, "retarget_pull_request", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def convert_pull_request_to_draft(ctx: RunContext[RunScope], request: PRWrite) -> dict[str, JsonValue]:
        """Convert an authorized PR to draft only when the user explicitly requests that conversion."""
        return await workflows.lifecycle(ctx, "convert_pull_request_to_draft", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def create_review(ctx: RunContext[RunScope], request: CreateReview) -> dict[str, JsonValue]:
        """Create a pending, COMMENT, or REQUEST_CHANGES review at the current head. Cannot approve."""
        return await workflows.review(ctx, "create_review", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def submit_review(ctx: RunContext[RunScope], request: SubmitReview) -> dict[str, JsonValue]:
        """Submit this task's confirmed pending review as COMMENT or REQUEST_CHANGES. Cannot approve."""
        return await workflows.review(ctx, "submit_review", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def edit_pending_review(ctx: RunContext[RunScope], request: EditReview) -> dict[str, JsonValue]:
        """Edit this task's confirmed pending review while its PR head remains unchanged."""
        return await workflows.review(ctx, "edit_pending_review", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def delete_pending_review(ctx: RunContext[RunScope], request: PendingReview) -> dict[str, JsonValue]:
        """Delete only this task's confirmed pending review. Cannot dismiss a submitted review."""
        return await workflows.review(ctx, "delete_pending_review", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def create_stack(ctx: RunContext[RunScope], request: CreateStack) -> dict[str, JsonValue]:
        """Group existing PRs bottom-to-top, requiring independent head-branch authority for every member."""
        from .product_github_stacks import Stacks
        return await Stacks(publisher).write(ctx, "create_stack", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def add_to_stack(ctx: RunContext[RunScope], request: AddToStack) -> dict[str, JsonValue]:
        """Append PRs to an unchanged stack. Supply its full members/revision and each new member's identity."""
        from .product_github_stacks import Stacks
        return await Stacks(publisher).write(ctx, "add_to_stack", request)

