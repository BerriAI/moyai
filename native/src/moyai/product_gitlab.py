"""Canonical selected-project GitLab toolset: finite typed native workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, JsonValue
from pydantic_ai import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool

from .contracts import Grant, RunScope
from .gitlab_workflow_types import (
    AddToStack,
    Artifact,
    Checkout,
    Compare,
    CreateBranch,
    CreateMR,
    CreateReview,
    CreateStack,
    EditReview,
    File,
    Item,
    Job,
    Log,
    MRRead,
    MRWrite,
    OpenMR,
    Page,
    PendingReview,
    PipelineJobs,
    Pipelines,
    PublishBranch,
    RebaseMR,
    RefRead,
    Retarget,
    Search,
    StackRead,
    Status,
    SubmitReview,
    Target,
    Tree,
    UpdateMR,
)
from .product_gitlab_publication import Publications
from .product_gitlab_reads import Reads
from .product_gitlab_reviews import Reviews, review_capability
from .product_gitlab_stacks import Stacks
from .product_gitlab_workflows import Workflows

if TYPE_CHECKING:
    from .product_connections import ProductConnections

APPROVALS = {"request_merge_request_write_access", "request_mr_lifecycle_access"}


def toolset(service: ProductConnections, scope: RunScope, grant: Grant):
    p = Publications(service, scope, grant)
    workflows, reads, reviews, stacks = Workflows(p), Reads(p), Reviews(p), Stacks(p)
    tools = FunctionToolset[RunScope](id=f"product_gitlab_{grant.id}")

    def register(
        name: str, schema: type[BaseModel], description: str,
        handler: Callable[[RunContext[RunScope], Any], Awaitable[dict]], *, write: bool = False,
    ) -> None:
        if write and grant.read_only:
            return
        async def action(ctx: RunContext[RunScope], request: BaseModel) -> dict:
            return await handler(ctx, request)
        action.__name__, action.__doc__ = name, description
        action.__annotations__ = {"ctx": RunContext[RunScope], "request": schema, "return": dict[str, JsonValue]}
        tools.tool(name=name, sequential=write or name == "checkout",
                   metadata={"annotations": {"readOnlyHint": not write}})(action)

    for name, schema, description in [
        ("repository", Target, "Read the selected immutable GitLab project and verified origin."),
        ("tree", Tree, "Read one bounded GitLab repository-tree page at a ref. Follow next_page."),
        ("file", File, "Read an exact GitLab repository file as base64 at a ref."),
        ("branches", Page, "List a bounded page of branches and effective protected flags."),
        ("commits", RefRead, "List commits at a branch or exact SHA; follow next_page."),
        ("commit", RefRead, "Read a GitLab commit by exact SHA or branch."),
        ("compare_commits", Compare, "Compare two exact commits; provider diff limits are reported explicitly."),
        ("search", Search, "Search code, commits, issues, MRs or notes only inside this selected project."),
        ("issues", Page, "List issues in this selected GitLab project."),
        ("issue", Item, "Read a project-local issue iid."),
        ("issue_comments", Item, "Read a bounded page of issue comments."),
        ("merge_requests", Page, "List MRs for this selected GitLab project."),
        ("merge_request", MRRead, "Read an MR by project-local iid, including exact source/target and diff refs."),
        ("merge_request_diffs", MRRead, "Read MR diff pages fenced to one head/base; inspect coverage and follow next_page."),
        ("merge_request_comments", MRRead, "Read MR note pages at a verified head."),
        ("merge_request_discussions", MRRead, "Read MR discussions at a verified head; follow next_page."),
        ("merge_request_commits", MRRead, "Read the MR's commits at a verified head."),
        ("merge_request_pipelines", MRRead, "Read pipelines associated with this MR at a verified head."),
        ("pipelines", Pipelines, "List pipelines for an exact project commit."),
        ("pipeline_jobs", PipelineJobs, "List jobs only after verifying pipeline project and exact commit."),
        ("job_logs", Log, "Read bounded job log lines; follow next_line with the immutable source revision."),
        ("job_artifacts", Job, "Read artifact metadata for an exact job and commit."),
        ("download_artifact", Artifact, "Read an exact artifact path as base64, at most 2 MiB, with SHA256."),
    ]:
        async def read(_ctx, request, selected=name):
            return await reads.call(selected, request)
        register(name, schema, description, read)

    async def capabilities(_ctx, request):
        await p.check_target(request)
        project = await p.project()
        return {
            **p.identity_scope, "repository": project.path_with_namespace, "read_only": grant.read_only,
            "branch_updates": "trusted_git_exact_ref_lease", "prepared_rebase": True,
            "stacks": "moyai_managed_mr_chain", "reviews": "task_owned_individual_draft_notes",
            "review_decisions": await review_capability(p),
            "unavailable": ["merge", "approve", "delete_branch", "arbitrary_api", "bulk_publish_drafts"],
        }
    register("capabilities", Target, "Inspect GitLab workflow semantics and live review-decision schema availability.", capabilities)

    async def checkout(ctx, request):
        from .product_gitlab_checkout import checkout as install
        return await install(p, ctx, request)
    register("checkout", Checkout, "Check out a branch, exact SHA or same-project MR into a fresh /workspace directory. Credentials stay on the trusted worker.", checkout)
    register("stacks", Page, "List Moyai-managed ordered MR chains in this GitLab project.", lambda _, request: stacks.read(request))
    register("stack", StackRead, "Read a managed MR chain with current heads/bases and full snapshot revision.", lambda _, request: stacks.read(request))
    register("publication_status", Status, "Inspect original request_key/operation receipts without replaying uncertain writes. Partial review publication lists individual confirmed steps.", lambda _, request: p.status(request))

    register("create_branch", CreateBranch, "Create an unrestricted branch at an exact commit using an empty ref lease. Never overwrite an existing ref.", workflows.create_branch, write=True)
    for name, schema, description in [
        ("publish_branch", PublishBranch, "Publish explicit files or a local staged directory to a new branch or acknowledged owned exact head. Supports binary files, executable modes and deletions."),
        ("create_merge_request", CreateMR, "Publish files or a staged directory as a ready-for-review GitLab MR. Exact current base, dedicated branch, frozen intent and no ambiguous replay."),
        ("update_merge_request", UpdateMR, "Publish follow-up changes to the MR's exact current head, using acknowledged ownership or explicit external content consent."),
        ("rebase_merge_request", RebaseMR, "Publish one prepared commit on the MR's exact current base; atomically replace only its expected feature head using an exact lease."),
    ]:
        async def publish(ctx, request, selected=name):
            return await workflows.publish(ctx, selected, request)
        register(name, schema, description, publish, write=True)
    register("open_merge_request", OpenMR, "Open a regular MR from an existing exact head. Does not establish branch ownership.", workflows.open_existing, write=True)
    for name, schema, description in [
        ("close_merge_request", MRWrite, "Close an authorized MR at its exact head/base. Never merge or delete its branch."),
        ("reopen_merge_request", MRWrite, "Reopen an authorized unmerged MR at its exact head/base."),
        ("retarget_merge_request", Retarget, "Retarget an authorized MR; revoke external consent and update owned metadata authority."),
        ("convert_merge_request_to_draft", MRWrite, "Convert an authorized MR to draft only when the requester explicitly asks."),
    ]:
        async def lifecycle(ctx, request, selected=name):
            return await workflows.lifecycle(ctx, selected, request)
        register(name, schema, description, lifecycle, write=True)
    for name, lifecycle_access in [("request_merge_request_write_access", False), ("request_mr_lifecycle_access", True)]:
        async def approve(ctx, request, lifecycle=lifecycle_access):
            return await p.approve(ctx, request, lifecycle=lifecycle)
        register(name, MRWrite, "Ask the direct requester to approve external MR content (exact head) or lifecycle access for this task. Delegated and automated calls cannot approve.", approve, write=True)
    for name, schema, description in [
        ("create_review", CreateReview, "Create task-owned pending draft notes or submit COMMENT/REQUEST_CHANGES at the exact MR head. No approvals or quick actions."),
        ("submit_review", SubmitReview, "Publish only this task's draft notes individually and optionally issue a real request-changes decision. Partial results remain journaled."),
        ("edit_pending_review", EditReview, "Edit one acknowledged draft note owned by this task at the unchanged review head."),
        ("delete_pending_review", PendingReview, "Delete only this task's acknowledged pending review notes. Never delete submitted comments."),
    ]:
        async def review(ctx, request, selected=name):
            return await reviews.run(ctx, selected, request)
        register(name, schema, description, review, write=True)
    for name, schema in [("create_stack", CreateStack), ("add_to_stack", AddToStack)]:
        async def stack_write(ctx, request, selected=name):
            return await stacks.write(ctx, selected, request)
        register(name, schema, "Create or append a Moyai-managed ordered MR chain. Requires full exact-head/base membership and independent authority for every member; updates are atomic.", stack_write, write=True)

    async def prepare(_ctx, definitions):
        return [replace(tool, kind="unapproved") if tool.name in APPROVALS else tool for tool in definitions]
    return tools.prepared(prepare).approval_required(lambda _ctx, tool, _args: tool.name in APPROVALS)


class ProductGitLabToolset(AbstractToolset[RunScope]):
    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant
        self.native = toolset(service, scope, grant)

    @property
    def id(self) -> str:
        return f"connection_{self.grant.id}"

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        await self.service.check(self.scope, self.grant)
        tools = await self.native.get_tools(ctx)
        return {name: replace(tool, toolset=self, tool_def=replace(
            tool.tool_def, description=f"Selected GitLab project {self.grant.policy['product']['repository_id']}. "
            + (tool.tool_def.description or ""),
        )) for name, tool in tools.items()}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        await self.service.check(self.scope, self.grant)
        current = (await self.native.get_tools(ctx))[name]
        return await self.native.call_tool(name, tool_args, ctx, current)
