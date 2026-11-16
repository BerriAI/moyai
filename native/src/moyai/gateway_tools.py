"""Skills Hub operations share MCP's deferred search, grants and write receipts."""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING, Annotated

from fastapi import HTTPException
from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from .connection_policy import ConnectionPolicy
from .contracts import Grant, RunScope
from .gateway import gateway_client, unpack_skill
from .mcp_remote import remote_mcp_toolset

if TYPE_CHECKING:
    from .database import Database


def gateway_toolset(database: Database, scope: RunScope, grant: Grant) -> AbstractToolset[RunScope]:
    policy = ConnectionPolicy.model_validate(grant.policy)
    config, discovery = policy.gateway, policy.gateway_discovery
    if config is None:
        raise PermissionError("This connection has no gateway configuration.")

    async def authorize() -> None:
        await database.authorize_scope(scope)

    client = gateway_client(database, grant, authorize)
    parts: list[AbstractToolset[RunScope]] = []
    if config.mcp and discovery and discovery.mcp.status == "ready":
        parts.append(remote_mcp_toolset(
            config.endpoint_config("/mcp"), database.security.unseal(grant.credential).decode(),
            database.settings.mcp_allowed_origins, identity=f"gateway_mcp_{grant.id}", authorize=authorize,
        ))
    if config.skills:
        skills: FunctionToolset[RunScope] = FunctionToolset(id=f"gateway_skills_{grant.id}", max_retries=0)

        @skills.tool(metadata={"annotations": {"readOnlyHint": True}})
        async def skills_search(
            ctx: RunContext[RunScope], query: Annotated[str, Field(max_length=500)] = "",
            offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        ) -> dict:
            """Find Skills Hub metadata. Users invoke skills explicitly through the / picker."""
            try:
                return (await client.skills(query=query, offset=offset)).model_dump()
            except Exception:  # noqa: BLE001 - provider errors may echo credentials
                raise ToolFailed("Could not search the connected Skills Hub.") from None

        @skills.tool(metadata={"annotations": {"readOnlyHint": True}})
        async def skills_pull(ctx: RunContext[RunScope], name: str) -> dict:
            """Read a skill package when the user asks to inspect or copy it.

            This does not activate the skill. To apply its instructions to a task,
            ask the user to select the skill from the / picker.
            """
            try:
                metadata, files = await client.skill(name)
                return {"skill": metadata.model_dump(), "files": files, "invocation": f"/gateway-{grant.id}:{metadata.name}"}
            except HTTPException as error:
                raise ToolFailed(str(error.detail)) from None
            except Exception:  # noqa: BLE001 - provider errors may echo credentials
                raise ToolFailed("Could not download the selected hub skill.") from None

        @skills.tool(metadata={"annotations": {"readOnlyHint": False}}, sequential=True)
        async def skills_upload(
            ctx: RunContext[RunScope],
            name: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)],
            description: Annotated[str, Field(min_length=1, max_length=1024)],
            instructions: Annotated[str, Field(min_length=1, max_length=450_000)],
            supporting_files: Annotated[dict[str, str], Field(max_length=99)] | None = None,
            version: Annotated[str, Field(min_length=1, max_length=64)] | None = None,
        ) -> dict:
            """Create and upload a skill only when the user asks to publish it to this hub.

            An admin key may publish publicly; member keys may submit for review.
            Report the returned status. Do not copy private chat content without
            the user's request. Existing skill names are never overwritten.
            """
            if ctx.deps.delegation_generated or ctx.deps.authority.surface != "web":
                raise ToolFailed("Upload skills from a direct web conversation.")
            async with database.sessions() as db:
                if await database.human_origin(db, ctx.deps) is None:
                    raise ToolFailed("Upload skills from a direct web conversation.")
            from .gateway_config import GatewaySkill
            try:
                GatewaySkill(name=name, description=description)
                manifest = f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n\n{instructions}"
                files = dict(supporting_files or {})
                if "SKILL.md" in files:
                    raise ValueError()
                files["SKILL.md"] = manifest
                if sum(len(value.encode("utf-8")) for value in files.values()) > 450_000:
                    raise ValueError()
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path, text in files.items():
                        archive.writestr(path, text)
                content = buffer.getvalue()
                unpack_skill(content)
                response = await client.request(
                    "POST", "/skill-hub/skills/upload",
                    files={"file": (name + ".zip", content, "application/zip")},
                    data={"version": version} if version else {},
                )
                return response.json()
            except Exception:  # noqa: BLE001 - provider errors may echo credentials
                raise ToolFailed("The hub upload was not confirmed. Check the hub before retrying.") from None

        parts.append(skills)
    return CombinedToolset(parts)
