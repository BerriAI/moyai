"""Personal GitHub reuses the existing capability with live user credentials."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai_harness.github import GitHub

from .connection_catalog import ConnectionDefinitionChanged
from .connection_policy import read_only_tool
from .contracts import Grant, RunScope

if TYPE_CHECKING:
    from .product_connections import ProductConnections


class PersonalOAuthToolset(AbstractToolset[RunScope]):
    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant

    @property
    def id(self) -> str:
        return f"connection_{self.grant.id}"

    async def authorize(self, *, write: bool = False) -> None:
        await self.service.check(self.scope, self.grant, write=write)

    async def native(self, *, write: bool = False):
        value = await self.service.credentials(self.scope, self.grant, write=write)
        return GitHub[RunScope](
            id=self.id, auth=value["access_token"], read_only=self.grant.read_only,
            include_instructions=False,
        ).get_toolset()

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        try:
            async with await self.native() as native:
                tools = await native.get_tools(ctx)
            await self.authorize()
            return {name: replace(tool, toolset=self) for name, tool in tools.items()}
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001 - provider exceptions may contain credentials
            raise ToolFailed("Could not load this personal connection. Reconnect it in Settings if access expired.") from None

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        try:
            async with await self.native(write=not read_only_tool(tool.tool_def)) as native:
                current = (await native.get_tools(ctx)).get(name)
                if current is None or (
                    current.tool_def.parameters_json_schema != tool.tool_def.parameters_json_schema
                    or read_only_tool(current.tool_def) != read_only_tool(tool.tool_def)
                    or current.tool_def.metadata != tool.tool_def.metadata
                ):
                    raise ConnectionDefinitionChanged(
                        "The provider's tools changed. Discover them again before retrying."
                    )
                await self.authorize(write=not read_only_tool(tool.tool_def))
                result = await native.call_tool(
                    name, current.args_validator.validate_python(tool_args), ctx, current,
                )
                if read_only_tool(tool.tool_def):
                    await self.authorize()
                return result
        except (PermissionError, ConnectionDefinitionChanged):
            raise
        except Exception:  # noqa: BLE001 - provider exceptions may contain credentials
            raise ToolFailed(
                "The personal provider operation was not confirmed. Check the result before retrying a change."
            ) from None
