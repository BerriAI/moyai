"""Repository routing for shared canonical GitHub operations."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from app.connector_errors import ConnectorError
from app.db import database as source_database
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from .connection_policy import ConnectionPolicy, approval_requirement, read_only_tool
from .contracts import Grant, RunScope
from .github_workflow_types import RepositorySelector, RepositoryTarget, repository_target

if TYPE_CHECKING:
    from .product_connections import ProductConnections, ProductGitHubToolset

ROUTING = "moyai_github_routing"
PROVIDER = "moyai_github_provider"
SEARCHES = frozenset({"search_code", "search_issues", "search_pull_requests"})
ARGUMENTS = TypeAdapter(dict[str, JsonValue])


class RepositoryGrant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repository_id: int = Field(gt=0, strict=True)


def permits(grant: Grant, operation: str, *, writing: bool) -> bool:
    if writing and grant.read_only:
        return False
    policy = ConnectionPolicy.model_validate(grant.policy)
    return policy.allows(operation)


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


@dataclass(frozen=True, slots=True)
class GitHubToolBinding:
    tool: ToolsetTool[RunScope]
    source: Literal["native", "provider"]

    def compatible_with(self, other: GitHubToolBinding) -> bool:
        previous, current = self.tool.tool_def, other.tool.tool_def
        return (
            self.source == other.source
            and previous.parameters_json_schema == current.parameters_json_schema
            and previous.metadata == current.metadata
            and previous.kind == current.kind
            and self.tool.max_retries == other.tool.max_retries
        )


def search_validator(tool: ToolsetTool[RunScope]) -> SchemaValidator:
    def validate(value: object) -> dict[str, object]:
        arguments = ARGUMENTS.validate_python(value)
        target = RepositoryTarget.model_validate(arguments)
        provider = ARGUMENTS.validate_python(tool.args_validator.validate_python({
            key: item for key, item in arguments.items() if key not in {"repository", "repository_id"}
        }))
        return {**provider, **target.model_dump()}

    return SchemaValidator(core_schema.no_info_plain_validator_function(validate))


class RoutedGitHubToolset(AbstractToolset[RunScope]):
    def __init__(
        self, service: ProductConnections, scope: RunScope, grants: Sequence[Grant],
        selection: str | ToolDefinition | None = None,
    ) -> None:
        self.service, self.scope, self.grants = service, scope, tuple(grants)
        self.selection = selection
        self._bindings: Mapping[str, GitHubToolBinding] = MappingProxyType({})

    @property
    def id(self) -> str:
        return "product_github_routing"

    def adapter(self, grant: Grant) -> ProductGitHubToolset:
        from .product_connections import ProductGitHubToolset

        adapter = self.service.toolset(self.scope, grant)
        if not isinstance(adapter, ProductGitHubToolset):
            raise TypeError("Canonical GitHub grants require the repository-scoped adapter.")
        return adapter

    def restore(self, recorded: ToolDefinition) -> ToolsetTool[RunScope]:
        if not read_only_tool(recorded):
            raise ToolFailed("Only GitHub provider reads can reconstruct a recorded schema.")
        operation = recorded.name.removeprefix("github_")
        schema = copy.deepcopy(recorded.parameters_json_schema)
        if operation in SEARCHES:
            schema["properties"] = {
                key: value for key, value in schema["properties"].items()
                if key not in {"repository", "repository_id"}
            }
        original = ToolsetTool(
            toolset=self, tool_def=replace(
                recorded, name=operation, parameters_json_schema=schema,
                metadata={key: value for key, value in (recorded.metadata or {}).items()
                          if key not in {ROUTING, PROVIDER}},
            ), max_retries=1, args_validator=SchemaValidator(ARGUMENTS.core_schema),
        )
        self._bindings = MappingProxyType({operation: GitHubToolBinding(original, "provider")})
        validator = search_validator(original) if operation in SEARCHES else original.args_validator
        return replace(original, tool_def=recorded, args_validator=validator)

    async def discover(
        self, ctx: RunContext[RunScope], operation: str | None,
    ) -> dict[str, GitHubToolBinding]:
        adapters = [(grant, self.adapter(grant)) for grant in self.grants]
        local_tools = [(grant, adapter, await adapter.publications.get_tools(ctx)) for grant, adapter in adapters]
        local_only = operation is not None and any(operation in local for _, _, local in local_tools)
        discovered = (
            [local for _, _, local in local_tools] if local_only
            else await asyncio.gather(*(adapter.get_tools(ctx) for _, adapter, _ in local_tools))
        )
        bindings: dict[str, GitHubToolBinding] = {}
        for (grant, _adapter, local), definitions in zip(local_tools, discovered, strict=True):
            for name, tool in definitions.items():
                if (
                    name == "checkout" or operation not in {None, name}
                    or not permits(grant, name, writing=not read_only_tool(tool.tool_def))
                ):
                    continue
                binding = GitHubToolBinding(tool, "native" if name in local else "provider")
                previous = bindings.get(name)
                if previous is not None and not previous.compatible_with(binding):
                    raise ToolFailed("Selected GitHub repositories expose incompatible tool definitions.")
                bindings[name] = binding
            if operation in bindings:
                break
        return bindings

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        if isinstance(self.selection, ToolDefinition):
            return {self.selection.name: self.restore(self.selection)}
        bindings = await self.discover(ctx, self.selection)
        self._bindings = MappingProxyType(bindings)
        return {f"github_{name}": self.shared(name, binding) for name, binding in bindings.items()}

    def shared(self, name: str, binding: GitHubToolBinding) -> ToolsetTool[RunScope]:
        tool = binding.tool
        schema = copy.deepcopy(tool.tool_def.parameters_json_schema)
        properties = schema.get("properties", {})
        selector: RepositorySelector = "owner_repo" if {"owner", "repo"} <= set(properties) else "repository"
        validator = tool.args_validator
        if name in SEARCHES:
            if {"repository", "repository_id"} & set(properties):
                raise ToolFailed("The GitHub search schema conflicts with repository routing.")
            schema["properties"] = {**properties, **RepositoryTarget.model_json_schema()["properties"]}
            validator = search_validator(tool)
        description = tool.tool_def.description or ""
        if description.startswith("Connected GitHub repository: "):
            description = description.split(". ", 1)[1]
        return replace(tool, toolset=self, args_validator=validator, tool_def=replace(
            tool.tool_def, name=f"github_{name}", parameters_json_schema=schema,
            description=description + " The server routes to the explicitly requested repository selected for this task.",
            metadata={**(tool.tool_def.metadata or {}), ROUTING: selector,
                      **({PROVIDER: True} if binding.source == "provider" else {})},
        ))

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        operation = name.removeprefix("github_")
        original = self._bindings[operation].tool
        selector = (tool.tool_def.metadata or {})[ROUTING]
        try:
            target = repository_target(tool_args, selector)
        except ValidationError as error:
            raise ToolFailed("Supply a valid explicit repository name or ID.") from error
        writing = not read_only_tool(original.tool_def)
        grant = await resolve_grant(self.service, self.scope, self.grants, target, operation, writing=writing)
        policy = ConnectionPolicy.model_validate(grant.policy)
        if policy.needs_approval(approval_requirement(original.tool_def)) and not ctx.tool_call_approved:
            raise ApprovalRequired
        arguments = (
            {key: value for key, value in tool_args.items() if key not in {"repository", "repository_id"}}
            if operation in SEARCHES else tool_args
        )
        adapter = self.adapter(grant)
        return await adapter.call_tool(operation, arguments, replace(ctx, tool_name=name), original)
