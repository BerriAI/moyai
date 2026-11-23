"""Personal provider tools resolve a live token and authority for every operation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .connection_catalog import ConnectionDefinitionChanged
from .connection_policy import read_only_tool
from .contracts import Grant, RunScope
from .mcp_remote import RemoteMCPConfig, remote_mcp_toolset
from .oauth_providers import PROVIDERS, OAuthProvider
from .personal_oauth import access_token

if TYPE_CHECKING:
    from .database import Database


class PersonalOAuthToolset(AbstractToolset[RunScope]):
    def __init__(self, database: Database, scope: RunScope, grant: Grant):
        self.database, self.scope, self.grant = database, scope, grant

    @property
    def id(self) -> str:
        return f"connection_{self.grant.id}"

    async def authorize(self) -> None:
        await self.database.authorize_scope(self.scope)

    async def native(self):
        token = await access_token(self.database, self.scope, self.grant)
        return remote_mcp_toolset(
            RemoteMCPConfig(url=PROVIDERS[TypeAdapter(OAuthProvider).validate_python(self.grant.kind)].mcp_url), token, [],
            identity=self.id, authorize=self.authorize,
        )

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
            async with await self.native() as native:
                current = (await native.get_tools(ctx)).get(name)
                if current is None or (
                    current.tool_def.parameters_json_schema != tool.tool_def.parameters_json_schema
                    or read_only_tool(current.tool_def) != read_only_tool(tool.tool_def)
                    or current.tool_def.metadata != tool.tool_def.metadata
                ):
                    raise ConnectionDefinitionChanged(
                        "The provider's tools changed. Discover them again before retrying."
                    )
                await self.authorize()
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
