"""Bounded gateway discovery and explicitly invoked, portable Skills Hub packages."""

from __future__ import annotations

import asyncio
import io
import stat
import zipfile
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx2
from fastapi import HTTPException
from sqlalchemy import select

from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .gateway_config import (
    GatewayCapability,
    GatewayConfig,
    GatewayDiscovery,
    GatewaySkill,
    GatewaySkillPage,
)
from .mcp_remote import MCPHTTPTransport, remote_mcp_toolset
from .models import Connection

if TYPE_CHECKING:
    from .database import Database

MAX_ARCHIVE = 10 * 1024 * 1024
MAX_CONTENT = 450_000


class GatewayClient:
    def __init__(
        self, config: GatewayConfig, credential: str, allowed_origins: list[str],
        authorize: Callable[[], Awaitable[None]] | None = None,
    ):
        self.config, self.credential = config, credential
        self.allowed_origins, self.authorize = allowed_origins, authorize

    async def request(self, method: str, path: str, **kwargs) -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=MCPHTTPTransport(self.config.endpoint_config(), self.allowed_origins, self.authorize),
            headers={"Authorization": "Bearer " + self.credential, "Accept-Encoding": "identity"},
            timeout=30, follow_redirects=False, trust_env=False,
        ) as client:
            response = await client.request(method, self.config.url + path, **kwargs)
        if self.authorize:
            await self.authorize()
        response.raise_for_status()
        return response

    async def skills(self, *, query: str = "", offset: int = 0) -> GatewaySkillPage:
        payload = (await self.request(
            "GET", "/skill-hub/skills", params={"query": query, "offset": offset, "limit": 100},
        )).json()
        return GatewaySkillPage.model_validate({
            "skills": [{**item, "description": item.get("description") or ""} for item in payload["skills"]],
            "next_offset": payload.get("next_offset"),
        })

    async def discover(self) -> GatewayDiscovery:
        async def probe(name: str):
            if not getattr(self.config, name):
                return GatewayCapability(status="disabled"), []
            try:
                if name == "skills":
                    skills: list[GatewaySkill] = []
                    offset = 0
                    for _ in range(10):
                        page = await self.skills(offset=offset)
                        skills.extend(page.skills)
                        next_offset = page.next_offset
                        if next_offset is None:
                            break
                        if next_offset <= offset:
                            raise ValueError("The skill catalog exceeds the supported limit.")
                        offset = next_offset
                    else:
                        raise ValueError("The skill catalog exceeds the supported limit.")
                    if len({skill.name for skill in skills}) != len(skills):
                        raise ValueError("The skill catalog contains duplicate names.")
                    return GatewayCapability(status="ready", count=len(skills)), skills
                async with remote_mcp_toolset(
                    self.config.endpoint_config("/mcp"), self.credential, self.allowed_origins,
                    identity="gateway_discovery", authorize=self.authorize,
                ) as tools:
                    catalog = await tools.list_tools()
                return GatewayCapability(status="ready", count=len(catalog)), []
            except httpx2.HTTPStatusError as error:
                code = error.response.status_code
                status = "unauthorized" if code in {401, 403} else "unavailable" if code in {404, 405, 503} else "error"
                return GatewayCapability(status=status, message=f"Gateway returned HTTP {code}."), []
            except (HTTPException, PermissionError):
                raise
            except Exception:  # noqa: BLE001 - provider errors may echo credentials
                # Provider exceptions can echo headers, bodies or credentials.
                return GatewayCapability(status="error", message="Could not discover this capability. Check access and retry."), []

        skills, mcp = await asyncio.gather(*(probe(name) for name in ("skills", "mcp")))
        return GatewayDiscovery.model_validate({
            "skills": skills[0], "mcp": mcp[0],
            "skill_catalog": skills[1],
        })

    async def skill(self, name: str) -> tuple[GatewaySkill, dict[str, str]]:
        GatewaySkill(name=name)
        route = "/skill-hub/skills/" + quote(name, safe="")
        before = (await self.request("GET", route)).json()
        content = (await self.request("GET", route + "/archive")).content
        files = unpack_skill(content)
        after = (await self.request("GET", route)).json()
        if any(before.get(key) != after.get(key) for key in ("id", "version", "updated_at", "source")):
            raise HTTPException(409, "The hub skill changed while downloading. Select it again.")
        if before.get("name") != name or after.get("name") != name:
            raise HTTPException(422, "The hub returned a different skill.")
        return GatewaySkill.model_validate({**after, "description": after.get("description") or ""}), files


def unpack_skill(content: bytes) -> dict[str, str]:
    """Never extract archive paths onto the host or execute downloaded scripts."""
    if len(content) > MAX_ARCHIVE:
        raise HTTPException(422, "The skill archive exceeds 10 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 100 or sum(item.file_size for item in entries) > MAX_CONTENT:
                raise ValueError()
            files: dict[str, str] = {}
            for item in entries:
                if item.is_dir():
                    continue
                path = item.filename
                if (
                    not path or "\\" in path or path.startswith("/")
                    or any(part in {"", ".", ".."} for part in path.split("/"))
                    or stat.S_ISLNK(item.external_attr >> 16) or path in files
                ):
                    raise ValueError()
                files[path] = archive.read(item).decode("utf-8")
            if not files.get("SKILL.md"):
                raise ValueError()
            return files
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, RuntimeError):
        raise HTTPException(422, "Use a skill with SKILL.md and at most 450 KB of UTF-8 supporting files.") from None


async def gateway_rows(database: Database, actor_id: str, *, shared: bool = False) -> list[Connection]:
    if shared:
        return []
    async with database.sessions() as db:
        return list(await db.scalars(select(Connection).where(
            Connection.owner_id == actor_id, Connection.shared.is_(False),
            Connection.kind == "litellm", Connection.enabled.is_(True),
        ).order_by(Connection.id)))


def gateway_client(database: Database, grant: Grant, authorize=None) -> GatewayClient:
    policy = ConnectionPolicy.model_validate(grant.policy)
    if policy.gateway is None:
        raise PermissionError("This connection has no gateway.")
    return GatewayClient(
        policy.gateway, database.security.unseal(grant.credential).decode(),
        database.settings.mcp_allowed_origins, authorize,
    )
