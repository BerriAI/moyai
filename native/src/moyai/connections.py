"""Bind selected accounts to Harness's native service capabilities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, assert_never

from pydantic import BaseModel, ValidationError
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
from .connection_policy import ApprovalRequirement, ConnectionPolicy, approval_requirement, read_only_tool
from .contracts import Grant, RunScope
from .github_workflow_types import RepositoryTarget, repository_target
from .security import Security

if TYPE_CHECKING:
    from .product_connections import ProductConnections

SERVICES: dict[
    str, type[GitHub[RunScope] | Linear[RunScope] | Notion[RunScope] | Slack[RunScope]]
] = {
    "github": GitHub, "linear": Linear, "notion": Notion, "slack": Slack,
}


@dataclass(frozen=True, slots=True)
class SelectedConnection:
    grant: Grant
    policy: ConnectionPolicy

    @property
    def prefix(self) -> str:
        return f"{self.grant.kind}_{self.grant.id[:8]}"

    @property
    def shared_github(self) -> bool:
        return self.grant.kind == "github" and self.policy.product is not None


def selected_connections(scope: RunScope) -> tuple[SelectedConnection, ...]:
    selected = tuple(
        SelectedConnection(grant, ConnectionPolicy.model_validate(grant.policy))
        for grant in sorted(scope.grants, key=lambda item: item.id)
    )
    if any(item.grant.kind not in {*SERVICES, "granola"} for item in selected):
        raise ValueError("Unsupported connected service.")
    if len({item.prefix for item in selected}) != len(selected):
        raise ValueError("Selected account tool prefixes collide.")
    return selected


def product_service() -> ProductConnections:
    from .runtime import current

    product = current().product_connections
    if product is None:
        raise PermissionError("Canonical product connections are not configured.")
    return product


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
        requirement: ApprovalRequirement = "connection",
    ) -> GitHubAllowed | GitHubRefused:
        from .product_github_routing import resolve_grant
        product = product_service()
        grants = tuple(item.grant for item in selected_connections(scope) if item.shared_github)
        try:
            grant = await resolve_grant(product, scope, grants, target, operation, writing=True)
        except ToolFailed as error:
            return GitHubRefused(message=str(error))
        policy = ConnectionPolicy.model_validate(grant.policy)
        return GitHubAllowed(requires_approval=policy.needs_approval(requirement))

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
        decision = await self.github_approval(
            ctx.deps, call.tool_name.removeprefix("github_"), target, approval_requirement(tool_def),
        )
        match decision:
            case GitHubRefused(message=message):
                raise ToolFailed(message)
            case GitHubAllowed(requires_approval=required):
                if required and not ctx.tool_call_approved:
                    raise ApprovalRequired
            case _:
                assert_never(decision)
        return args


def connection_tools(security: Security) -> Toolset[RunScope]:
    catalog = ConnectionCatalog()

    def scoped(ctx: RunContext[RunScope], connection: SelectedConnection) -> AbstractToolset[RunScope]:
        grant, policy = connection.grant, connection.policy
        if grant.kind == "granola":
            from .granola import GranolaToolset
            from .runtime import current
            native = GranolaToolset(current().database, ctx.deps, grant)
        elif policy.product is not None:
            native = product_service().toolset(ctx.deps, grant)
        else:
            credential = security.unseal(grant.credential).decode()
            if credential.strip().lower() == "oauth":
                raise ValueError("Server connections require a token; interactive OAuth is not supported here.")
            native = SERVICES[grant.kind](
                id=f"connection_{grant.id}", auth=credential, read_only=grant.read_only,
                include_instructions=False,
            ).get_toolset()

        async def prepare_approval(
            _ctx: RunContext[RunScope], definitions: list[ToolDefinition],
        ) -> list[ToolDefinition]:
            return [
                replace(tool, kind="unapproved") if policy.needs_approval(approval_requirement(tool)) else tool
                for tool in definitions
            ]

        return (
            native.filtered(lambda _ctx, tool: policy.allows(tool.name))
            .prepared(prepare_approval)
            .approval_required(lambda _ctx, tool, _args: policy.needs_approval(approval_requirement(tool)))
            .prefixed(connection.prefix)
            .defer_loading()
        )

    def github(
        ctx: RunContext[RunScope], connections: tuple[SelectedConnection, ...], operation: str | None,
    ) -> AbstractToolset[RunScope]:
        if not connections:
            return CombinedToolset([])
        from .product_github_checkout import checkout_toolset
        from .product_github_routing import RoutedGitHubToolset

        product = product_service()
        grants = tuple(item.grant for item in connections)
        checkout_grants = [item.grant for item in connections if item.policy.allows("checkout")]
        if operation == "checkout":
            if not checkout_grants:
                return CombinedToolset([])
            return checkout_toolset(product, ctx.deps, checkout_grants).defer_loading()
        routed = RoutedGitHubToolset(product, ctx.deps, grants, operation).defer_loading()
        if operation is not None or not checkout_grants:
            return routed
        return CombinedToolset([
            routed, checkout_toolset(product, ctx.deps, checkout_grants).defer_loading(),
        ])

    def build(ctx: RunContext[RunScope], tool_name: str | None = None) -> AbstractToolset[RunScope]:
        connections = selected_connections(ctx.deps)
        repositories = tuple(item for item in connections if item.shared_github)
        if tool_name is None:
            return CombinedToolset([
                *(scoped(ctx, item) for item in connections if not item.shared_github),
                github(ctx, repositories, None),
            ])
        for connection in connections:
            if tool_name.startswith(connection.prefix + "_"):
                return scoped(ctx, connection)
        if tool_name.startswith("github_"):
            return github(ctx, repositories, tool_name.removeprefix("github_"))
        return CombinedToolset([])

    def resolve(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
        if not ctx.deps.grants:
            return None
        if (
            any(grant.kind == "github" for grant in ctx.deps.grants)
            and all(grant.policy.get("product") for grant in ctx.deps.grants)
        ):
            from .product_connections import ProductCatalogToolset

            return ProductCatalogToolset(
                catalog, ctx, lambda name: build(ctx, name), reconstruct=lambda tool: reconstruct(ctx, tool),
            )
        return CatalogToolset(
            catalog, ctx, lambda name: build(ctx, name), reconstruct=lambda tool: reconstruct(ctx, tool),
        )

    def reconstruct(ctx: RunContext[RunScope], tool: ToolDefinition) -> AbstractToolset[RunScope] | None:
        if not (tool.metadata or {}).get("moyai_github_provider"):
            return None
        from .product_github_routing import RoutedGitHubToolset
        grants = tuple(item.grant for item in selected_connections(ctx.deps) if item.shared_github)
        return RoutedGitHubToolset(product_service(), ctx.deps, grants, tool).defer_loading()

    # Per-run factories execute in workflow code in the durable SDK. Resolve
    # selected accounts inside its get-tools/call-tool activities instead:
    # credential decryption and canonical provider imports are not replay-safe.
    # Provider schemas, transport, calls and deferred approvals stay SDK-owned.
    return ConnectionTools(
        DynamicToolset(resolve, id="moyai_connections", per_run_step=True),
        id="moyai_connections",
    )
