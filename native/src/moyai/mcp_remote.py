"""Remote MCP protocol and egress boundary for user-managed connections."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai.mcp import MCPToolset

from .contracts import RunScope

MAX_MCP_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass
class MCPResponseBudget:
    received: int = 0


class BoundedMCPStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, budget: MCPResponseBudget):
        self.stream = stream
        self.budget = budget

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            self.budget.received += len(chunk)
            if self.budget.received > MAX_MCP_RESPONSE_BYTES:
                raise httpx2.ReadError("The MCP response exceeded the size limit.")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class RemoteMCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    url: str = Field(min_length=1, max_length=2048)
    transport: Literal["streamable_http"] = "streamable_http"

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError("Enter an HTTP or HTTPS MCP endpoint without whitespace.")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value
        ):
            raise ValueError("Use an HTTP or HTTPS endpoint without credentials, query parameters or fragments.")
        try:
            url = httpx2.URL(value)
            if not url.port and parsed.netloc.endswith(":"):
                raise ValueError
        except (httpx2.InvalidURL, ValueError) as error:
            raise ValueError("Enter a valid MCP endpoint.") from error
        return str(url)


def origin(url: httpx2.URL) -> tuple[str, str, int]:
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port


def validate_mcp_endpoint(config: RemoteMCPConfig, allowed_origins: Sequence[str]) -> bool:
    """Return whether this exact origin is an operator-approved private endpoint."""
    url = httpx2.URL(config.url)
    approved = any(origin(httpx2.URL(item)) == origin(url) for item in allowed_origins)
    if url.scheme != "https" and not approved:
        raise ValueError("Use HTTPS, or ask an administrator to allow this MCP origin.")
    try:
        address = ipaddress.ip_address(url.host)
    except ValueError:
        address = None
    if address is not None and not address.is_global and not approved:
        raise ValueError("Private MCP endpoints require an administrator-approved origin.")
    return approved


class MCPHTTPTransport(httpx2.AsyncBaseTransport):
    """Pin validated DNS addresses while retaining the configured HTTP/TLS identity."""

    def __init__(
        self, config: RemoteMCPConfig, allowed_origins: Sequence[str],
        authorize: Callable[[], Awaitable[None]] | None = None,
    ):
        self.endpoint = httpx2.URL(config.url)
        self.private_allowed = validate_mcp_endpoint(config, allowed_origins)
        self.authorize = authorize
        self.budget = MCPResponseBudget()
        self.transport = httpx2.AsyncHTTPTransport(
            trust_env=False, retries=0,
            limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if origin(request.url) != origin(self.endpoint):
            raise httpx2.ConnectError("MCP requests must stay on the configured origin.", request=request)
        try:
            async with asyncio.timeout(10):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    self.endpoint.host, origin(self.endpoint)[2], type=socket.SOCK_STREAM,
                )
        except (OSError, TimeoutError) as error:
            raise httpx2.ConnectError("Could not resolve the MCP endpoint.", request=request) from error
        hosts = list(dict.fromkeys(str(item[4][0]) for item in addresses))
        if not hosts or (not self.private_allowed and any(not ipaddress.ip_address(host).is_global for host in hosts)):
            raise httpx2.ConnectError("The MCP endpoint resolves to a restricted address.", request=request)
        # Passing the checked numeric address to the transport prevents a second
        # DNS lookup from changing the recipient after the authorization check.
        pinned = httpx2.Request(
            request.method, request.url.copy_with(host=hosts[0]),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.endpoint.host},
        )
        if self.authorize is not None:
            await self.authorize()
        response = await self.transport.handle_async_request(pinned)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise httpx2.ReadError("The MCP endpoint must support uncompressed responses.", request=request)
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = BoundedMCPStream(response.stream, self.budget)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


class BoundedMCPToolset(MCPToolset[RunScope]):
    async def list_tools(self) -> list[MCPTool]:
        async with asyncio.timeout(30):
            return await super().list_tools()


def remote_mcp_toolset(
    config: RemoteMCPConfig, credential: str, allowed_origins: Sequence[str], *, identity: str,
    authorize: Callable[[], Awaitable[None]] | None = None,
) -> MCPToolset[RunScope]:
    validate_mcp_endpoint(config, allowed_origins)

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = False,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers={**(headers or {}), "Accept-Encoding": "identity"},
            auth=auth, follow_redirects=False, trust_env=False,
            timeout=httpx2.Timeout(30, connect=10, read=60),
            transport=MCPHTTPTransport(config, allowed_origins, authorize),
        )

    return BoundedMCPToolset(
        StreamableHttpTransport(
            config.url, auth=credential or None, httpx_client_factory=client_factory,
        ),
        id=identity, include_instructions=False, init_timeout=15, read_timeout=60,
        tool_error_behavior="failed",
    )
