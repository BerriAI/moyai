"""Repository routing for shared canonical GitHub operations."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from app.connector_errors import ConnectorError
from app.db import database as source_database
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from .connections import PROHIBITED_TOOLS, ConnectionPolicy, read_only_tool
from .contracts import Grant, RunScope
from .github_workflow_types import RepositorySelector, RepositoryTarget, repository_target

if TYPE_CHECKING:
    from .product_connections import ProductConnections

ROUTING = "moyai_github_routing"
PROVIDER = "moyai_github_provider"
SEARCHES = frozenset({"search_code", "search_issues", "search_pull_requests"})
ARGUMENTS = TypeAdapter(dict[str, JsonValue])


class RepositoryGrant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repository_id: int = Field(gt=0, strict=True)


def permits(grant: Grant, operation: str, *, writing: bool) -> bool:
    policy = ConnectionPolicy.model_validate(grant.policy)
    return (
        operation not in PROHIBITED_TOOLS
        and (policy.allowed_tools is None or operation in policy.allowed_tools)
        and not (writing and grant.read_only)
    )


async def resolve_grant(
    service: ProductConnections, scope: RunScope, grants: Sequence[Grant],
    target: RepositoryTarget, operation: str, *, writing: bool,
) -> Grant:
    if not grants:
        raise ToolFailed("No GitHub repository is selected for this task.")
    await service.check(scope, grants[0])
    try:
        github = service.connectors.github
        repository_id = await source_database(github.target, target.repository_id or target.repository)
        if target.repository_id is not None and target.repository:
            named = await source_database(github.target, target.repository)
            if named != repository_id:
                raise ToolFailed("The repository name and ID identify different repositories.")
    except ConnectorError as error:
        raise ToolFailed(str(error)) from error
    selected = [
        grant for grant in grants
        if RepositoryGrant.model_validate(grant.policy.get("product")).repository_id == repository_id
        and permits(grant, operation, writing=writing)
    ]
    if len(selected) != 1:
        raise ToolFailed(f"This task has no unique selected GitHub connection permitting {operation} of that repository.")
    await service.check(scope, selected[0])
    return selected[0]


class RoutedGitHubToolset(AbstractToolset[RunScope]):
    def __init__(
        self, service: ProductConnections, scope: RunScope, grants: Sequence[Grant],
        operation: str | None = None, *, recorded: ToolDefinition | None = None,
    ) -> None:
        self.service, self.scope, self.grants = service, scope, tuple(grants)
        self.operation = operation
        self.recorded = recorded
        self.originals: dict[str, ToolsetTool[RunScope]] = {}
        self.providers: set[str] = set()

    @property
    def id(self) -> str:
        return "product_github_routing"

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        from .product_connections import ProductGitHubToolset

        if self.recorded is not None:
            if not read_only_tool(self.recorded):
                raise ToolFailed("Only GitHub provider reads can reconstruct a recorded schema.")
            operation = self.recorded.name.removeprefix("github_")
            schema = copy.deepcopy(self.recorded.parameters_json_schema)
            if operation in SEARCHES:
                schema["properties"] = {
                    key: value for key, value in schema["properties"].items()
                    if key not in {"repository", "repository_id"}
                }
            original = ToolsetTool(
                toolset=self, tool_def=replace(
                    self.recorded, name=operation, parameters_json_schema=schema,
                    metadata={key: value for key, value in (self.recorded.metadata or {}).items()
                              if key not in {ROUTING, PROVIDER}},
                ), max_retries=1, args_validator=SchemaValidator(ARGUMENTS.core_schema),
            )
            self.originals[operation] = original
            self.providers.add(operation)
            return {self.recorded.name: replace(
                self.shared(operation, original), tool_def=self.recorded,
            )}
        adapters = [(grant, ProductGitHubToolset(self.service, self.scope, grant)) for grant in self.grants]
        local_tools = [(grant, adapter, await adapter.publications.get_tools(ctx)) for grant, adapter in adapters]
        local_only = self.operation is not None and any(self.operation in local for _, _, local in local_tools)
        discovered = (
            [local for _, _, local in local_tools] if local_only
            else await asyncio.gather(*(adapter.get_tools(ctx) for _, adapter, _ in local_tools))
        )
        for (grant, _adapter, local), definitions in zip(local_tools, discovered, strict=True):
            for name, tool in definitions.items():
                if (
                    name == "checkout" or self.operation not in {None, name}
                    or not permits(grant, name, writing=not read_only_tool(tool.tool_def))
                ):
                    continue
                previous = self.originals.get(name)
                if previous is not None and (
                    previous.tool_def.parameters_json_schema != tool.tool_def.parameters_json_schema
                    or previous.tool_def.metadata != tool.tool_def.metadata
                    or previous.tool_def.kind != tool.tool_def.kind
                ):
                    raise ToolFailed("Selected GitHub repositories expose incompatible tool definitions.")
                self.originals[name] = tool
                if name not in local:
                    self.providers.add(name)
            if self.operation in self.originals:
                break
        return {f"github_{name}": self.shared(name, tool) for name, tool in self.originals.items()}

    def shared(self, name: str, tool: ToolsetTool[RunScope]) -> ToolsetTool[RunScope]:
        schema = copy.deepcopy(tool.tool_def.parameters_json_schema)
        selector: RepositorySelector = "owner_repo" if {"owner", "repo"} <= set(schema.get("properties", {})) else "repository"
        validator = tool.args_validator
        if name in SEARCHES:
            if {"repository", "repository_id"} & set(schema.get("properties", {})):
                raise ToolFailed("The GitHub search schema conflicts with repository routing.")
            schema["properties"] = {
                **schema.get("properties", {}), **RepositoryTarget.model_json_schema()["properties"],
            }
            def search_arguments(value: object) -> dict[str, object]:
                arguments = ARGUMENTS.validate_python(value)
                target = RepositoryTarget.model_validate(arguments)
                provider = ARGUMENTS.validate_python(tool.args_validator.validate_python({
                    key: item for key, item in arguments.items() if key not in {"repository", "repository_id"}
                }))
                return {**provider, **target.model_dump()}

            validator = SchemaValidator(core_schema.no_info_plain_validator_function(search_arguments))
        description = tool.tool_def.description or ""
        if description.startswith("Connected GitHub repository: "):
            description = description.split(". ", 1)[1]
        return replace(tool, toolset=self, args_validator=validator, tool_def=replace(
            tool.tool_def, name=f"github_{name}", parameters_json_schema=schema,
            description=description + " The server routes to the explicitly requested repository selected for this task.",
            metadata={**(tool.tool_def.metadata or {}), ROUTING: selector,
                      **({PROVIDER: True} if name in self.providers else {})},
        ))

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        from .product_connections import ProductGitHubToolset

        operation = name.removeprefix("github_")
        original = self.originals[operation]
        selector = (tool.tool_def.metadata or {})[ROUTING]
        target = repository_target(tool_args, selector)
        writing = not read_only_tool(original.tool_def)
        grant = await resolve_grant(self.service, self.scope, self.grants, target, operation, writing=writing)
        policy = ConnectionPolicy.model_validate(grant.policy)
        if not ctx.tool_call_approved and (
            original.tool_def.kind == "unapproved" or writing and policy.require_approval
        ):
            raise ApprovalRequired
        arguments = (
            {key: value for key, value in tool_args.items() if key not in {"repository", "repository_id"}}
            if operation in SEARCHES else tool_args
        )
        adapter = ProductGitHubToolset(self.service, self.scope, grant)
        return await adapter.call_tool(operation, arguments, replace(ctx, tool_name=name), original)
