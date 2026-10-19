"""Bind selected accounts to Harness's native service capabilities."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Toolset, durable_operation
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset
from pydantic_ai_harness.github import GitHub
from pydantic_ai_harness.linear import Linear
from pydantic_ai_harness.notion import Notion
from pydantic_ai_harness.slack import Slack

from .connection_catalog import CatalogToolset, ConnectionCatalog
from .contracts import RunScope
from .github_workflow_types import RepositoryTarget, repository_target
from .security import Security

SERVICES: dict[
    str, type[GitHub[RunScope] | Linear[RunScope] | Notion[RunScope] | Slack[RunScope]]
] = {
    "github": GitHub, "linear": Linear, "notion": Notion, "slack": Slack,
}
PROHIBITED_TOOLS = {
    "merge_pull_request", "delete_repository", "delete_file",
    "pull_request_review_write", "create_and_submit_pull_request_review",
}


class ConnectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] | None = Field(default=None, max_length=200)
    require_approval: bool = True
    product: dict[str, object] | None = None


def read_only_tool(tool: ToolDefinition) -> bool:
    return ((tool.metadata or {}).get("annotations") or {}).get("readOnlyHint") is True


class GitHubAllowed(BaseModel):
    kind: Literal["allowed"] = "allowed"
    requires_approval: bool


class GitHubRefused(BaseModel):
    kind: Literal["refused"] = "refused"
    message: str


class ConnectionTools(Toolset[RunScope]):
    @durable_operation("github_approval")
    async def github_approval(
        self, scope: RunScope, operation: str, target: RepositoryTarget,
    ) -> GitHubAllowed | GitHubRefused:
        from .product_github_routing import resolve_grant
        from .runtime import current

        product = current().product_connections
        if product is None:
            raise PermissionError("Canonical product connections are not configured.")
        grants = [grant for grant in scope.grants if grant.kind == "github" and grant.policy.get("product")]
        try:
            grant = await resolve_grant(product, scope, grants, target, operation, writing=True)
        except ToolFailed as error:
            return GitHubRefused(message=str(error))
        return GitHubAllowed(requires_approval=ConnectionPolicy.model_validate(grant.policy).require_approval)

    async def after_tool_validate(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        selector = (tool_def.metadata or {}).get("moyai_github_routing")
        if selector is None or read_only_tool(tool_def):
            return args
        if selector not in {"repository", "owner_repo"}:
            raise ToolFailed("Unknown GitHub repository selector.")
        try:
            target = repository_target(args, selector)
        except ValidationError as error:
            raise ToolFailed("Supply a valid explicit repository name or ID.") from error
        decision = await self.github_approval(ctx.deps, call.tool_name.removeprefix("github_"), target)
        match decision:
            case GitHubRefused(message=message):
                raise ToolFailed(message)
            case GitHubAllowed(requires_approval=True) if not ctx.tool_call_approved:
                raise ApprovalRequired
        return args


def connection_tools(security: Security) -> Toolset[RunScope]:
    catalog = ConnectionCatalog()

    def build(ctx: RunContext[RunScope], tool_name: str | None = None) -> AbstractToolset[RunScope]:
        selected = []
        checkout_grants = []
        github_grants = []
        prefixes: set[str] = set()
        for grant in sorted(ctx.deps.grants, key=lambda item: item.id):
            service = SERVICES.get(grant.kind)
            if service is None:
                raise ValueError("Unsupported connected service.")
            prefix = f"{grant.kind}_{grant.id[:8]}"
            if prefix in prefixes:
                raise ValueError("Selected account tool prefixes collide.")
            prefixes.add(prefix)
            policy = ConnectionPolicy.model_validate(grant.policy)
            canonical_github = grant.kind == "github" and policy.product is not None
            if canonical_github:
                github_grants.append(grant)
            if canonical_github and (policy.allowed_tools is None or "checkout" in policy.allowed_tools):
                checkout_grants.append(grant)
            if canonical_github and tool_name is None:
                continue
            if tool_name is not None and not tool_name.startswith(prefix + "_"):
                continue
            if policy.product is not None:
                from .runtime import current
                product = getattr(current(), "product_connections", None)
                if product is None:
                    raise PermissionError("Canonical product connections are not configured.")
                native = product.toolset(ctx.deps, grant)
            else:
                credential = security.unseal(grant.credential).decode()
                if credential.strip().lower() == "oauth":
                    raise ValueError("Server connections require a token; interactive OAuth is not supported here.")
                native = service(
                    id=f"connection_{grant.id}",
                    auth=credential,
                    read_only=grant.read_only,
                    include_instructions=False,
                ).get_toolset()

            def allowed(
                _ctx: RunContext[RunScope], tool: ToolDefinition,
                allowed_names: list[str] | None = policy.allowed_tools,
            ) -> bool:
                return (
                    tool.name not in PROHIBITED_TOOLS
                    and (allowed_names is None or tool.name in allowed_names)
                )

            tools = native.filtered(allowed)
            if policy.require_approval:
                async def prepare_approval(
                    _ctx: RunContext[RunScope], definitions: list[ToolDefinition]
                ) -> list[ToolDefinition]:
                    return [
                        replace(tool, kind="unapproved") if not read_only_tool(tool) else tool
                        for tool in definitions
                    ]
                tools = tools.prepared(prepare_approval)
                tools = tools.approval_required(
                    lambda _ctx, tool, _args: not read_only_tool(tool)
                )
            selected.append(tools.prefixed(prefix).defer_loading())
        if github_grants and (
            tool_name is None or tool_name.startswith("github_")
            and tool_name != "github_checkout"
            and not any(tool_name.startswith(f"github_{grant.id[:8]}_") for grant in github_grants)
        ):
            from .product_github_routing import RoutedGitHubToolset
            from .runtime import current

            product = current().product_connections
            if product is None:
                raise PermissionError("Canonical product connections are not configured.")
            selected.append(RoutedGitHubToolset(
                product, ctx.deps, github_grants,
                tool_name.removeprefix("github_") if tool_name else None,
            ).defer_loading())
        if checkout_grants and tool_name in {None, "github_checkout"}:
            from .product_github_checkout import checkout_toolset
            from .runtime import current

            product = current().product_connections
            if product is None:
                raise PermissionError("Canonical product connections are not configured.")
            selected.append(checkout_toolset(product, ctx.deps, checkout_grants).defer_loading())
        return CombinedToolset(selected)

    def resolve(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
        if not ctx.deps.grants:
            return None
        if (
            any(grant.kind == "github" for grant in ctx.deps.grants)
            and all(grant.policy.get("product") for grant in ctx.deps.grants)
        ):
            from .product_connections import ProductCatalogToolset

            return ProductCatalogToolset(catalog, ctx, lambda name: build(ctx, name), reconstruct=lambda tool: reconstruct(ctx, tool))
        return CatalogToolset(catalog, ctx, lambda name: build(ctx, name), reconstruct=lambda tool: reconstruct(ctx, tool))

    def reconstruct(ctx: RunContext[RunScope], tool: ToolDefinition) -> AbstractToolset[RunScope] | None:
        if not (tool.metadata or {}).get("moyai_github_provider"):
            return None
        from .product_github_routing import RoutedGitHubToolset
        from .runtime import current

        service = current().product_connections
        if service is None:
            raise PermissionError("Canonical product connections are not configured.")
        grants = [grant for grant in ctx.deps.grants if grant.kind == "github" and grant.policy.get("product")]
        return RoutedGitHubToolset(service, ctx.deps, grants, recorded=tool).defer_loading()

    # Per-run factories execute in workflow code in the durable SDK. Resolve
    # selected accounts inside its get-tools/call-tool activities instead:
    # credential decryption and canonical provider imports are not replay-safe.
    # Provider schemas, transport, calls and deferred approvals stay SDK-owned.
    return ConnectionTools(
        DynamicToolset(resolve, id="moyai_connections", per_run_step=True),
        id="moyai_connections",
    )
