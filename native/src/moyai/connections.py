"""Bind selected accounts to Harness's native service capabilities."""

from __future__ import annotations

from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset
from pydantic_ai_harness.github import GitHub
from pydantic_ai_harness.linear import Linear
from pydantic_ai_harness.notion import Notion
from pydantic_ai_harness.slack import Slack

from .contracts import RunScope
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


def connection_tools(security: Security) -> Toolset[RunScope]:
    def resolve(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
        selected = []
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
            selected.append(tools.prefixed(prefix))
        return CombinedToolset(selected) if selected else None

    # Per-run factories execute in workflow code in the durable SDK. Resolve
    # selected accounts inside its get-tools/call-tool activities instead:
    # credential decryption and canonical provider imports are not replay-safe.
    # Provider schemas, transport, calls and deferred approvals stay SDK-owned.
    return Toolset(
        DynamicToolset(resolve, id="moyai_connections", per_run_step=True),
        id="moyai_connections",
    )
