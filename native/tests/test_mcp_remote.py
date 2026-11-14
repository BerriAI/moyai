"""Remote MCP endpoints cannot redirect credentials or rebind into private networks."""

import asyncio
import socket

import httpx2
import pytest
from pydantic import ValidationError

from moyai.mcp_remote import (
    BoundedMCPStream,
    MCPHTTPTransport,
    MCPResponseBudget,
    RemoteMCPConfig,
    remote_mcp_toolset,
    validate_mcp_endpoint,
)


@pytest.mark.parametrize("url", [
    "file:///tmp/mcp", "https://user:password@example.com/mcp",
    "https://example.com/mcp?token=secret", "https://example.com/mcp#fragment",
    "https://example.com/\nmcp", "https://example.com\\@localhost/mcp", "https:///mcp",
])
def test_mcp_endpoint_rejects_ambiguous_or_credential_bearing_urls(url):
    with pytest.raises(ValidationError):
        RemoteMCPConfig(url=url)


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "http://example.com:0/mcp", "https://127.0.0.1/mcp", "https://[::1]/mcp",
])
def test_mcp_private_or_plaintext_endpoint_requires_exact_operator_origin(url):
    config = RemoteMCPConfig(url=url)
    with pytest.raises(ValueError):
        validate_mcp_endpoint(config, [])
    parsed = httpx2.URL(url)
    approved = str(parsed.copy_with(path="", query=None, fragment=None))
    assert validate_mcp_endpoint(config, [approved])
    with pytest.raises(ValueError):
        validate_mcp_endpoint(config, ["https://other.example.com"])
    with pytest.raises(ValueError):
        validate_mcp_endpoint(config, [str(parsed.copy_with(port=12345, path=""))])


async def test_mcp_dns_is_pinned_with_original_host_and_tls_identity(monkeypatch):
    loop = asyncio.get_running_loop()

    async def addresses(host, port, **kwargs):
        assert (host, port) == ("gateway.example.com", 443)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(loop, "getaddrinfo", addresses)
    transport = MCPHTTPTransport(RemoteMCPConfig(url="https://gateway.example.com/mcp"), [])
    await transport.transport.aclose()
    observed = []

    async def upstream(request):
        observed.append(request)
        return httpx2.Response(200, json={"ok": True})

    transport.transport = httpx2.MockTransport(upstream)
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.post("https://gateway.example.com/mcp", headers={"authorization": "Bearer test-token"})
    assert len(observed) == 1
    assert observed[0].url.host == "93.184.216.34"
    assert observed[0].headers["host"] == "gateway.example.com"
    assert observed[0].extensions["sni_hostname"] == "gateway.example.com"
    assert observed[0].headers["authorization"] == "Bearer test-token"


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["169.254.169.254"], ["::1"], ["::ffff:127.0.0.1"],
    ["93.184.216.34", "10.0.0.1"], [],
])
async def test_mcp_dns_denies_private_mixed_and_empty_answers(monkeypatch, addresses):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    transport = MCPHTTPTransport(RemoteMCPConfig(url="https://gateway.example.com/mcp"), [])
    await transport.transport.aclose()
    calls = []
    transport.transport = httpx2.MockTransport(lambda request: calls.append(request) or httpx2.Response(200))
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.ConnectError, match="restricted"):
            await client.get("https://gateway.example.com/mcp")
    assert calls == []


async def test_mcp_rechecks_destination_on_each_request_and_authority_after_dns(monkeypatch):
    lookups, sends = [], []
    allowed = True

    async def resolve(host, port, **kwargs):
        nonlocal allowed
        lookups.append(host)
        if len(lookups) == 2:
            allowed = False
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    async def authorize():
        if not allowed:
            raise PermissionError("Connection revoked during DNS lookup")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    transport = MCPHTTPTransport(RemoteMCPConfig(url="https://gateway.example.com/mcp"), [], authorize)
    await transport.transport.aclose()
    transport.transport = httpx2.MockTransport(lambda request: sends.append(request) or httpx2.Response(200))
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.get("https://gateway.example.com/mcp")
        with pytest.raises(PermissionError, match="revoked"):
            await client.get("https://gateway.example.com/mcp")
        with pytest.raises(httpx2.ConnectError, match="configured origin"):
            await client.get("https://attacker.example.com/mcp")
    assert len(sends) == 1
    assert lookups == ["gateway.example.com", "gateway.example.com"]


@pytest.mark.parametrize("separate_responses", [False, True])
async def test_mcp_response_bound_counts_all_stream_chunks(monkeypatch, separate_responses):
    monkeypatch.setattr("moyai.mcp_remote.MAX_MCP_RESPONSE_BYTES", 8)
    closed = []

    class Chunks(httpx2.AsyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

        async def aclose(self):
            closed.append(True)

    budget = MCPResponseBudget()
    streams = [
        BoundedMCPStream(Chunks(chunks), budget)
        for chunks in ([[b"12345"], [b"67890"]] if separate_responses else [[b"12345", b"67890"]])
    ]
    try:
        with pytest.raises(httpx2.ReadError, match="size limit"):
            for stream in streams:
                _ = [chunk async for chunk in stream]
    finally:
        for stream in streams:
            await stream.aclose()
    assert len(closed) == len(streams)


async def test_mcp_factory_cannot_enable_redirects_or_inherit_proxy_credentials():
    tools = remote_mcp_toolset(
        RemoteMCPConfig(url="https://gateway.example.com/mcp"), "", [], identity="configured",
    )
    factory = tools.client.transport.httpx_client_factory
    async with factory(follow_redirects=True) as client:
        assert not client.follow_redirects
        assert not client.trust_env
        assert client.headers["accept-encoding"] == "identity"


async def test_mcp_rejects_compressed_responses_before_decoding():
    transport = MCPHTTPTransport(
        RemoteMCPConfig(url="http://127.0.0.1:12345/mcp"), ["http://127.0.0.1:12345"],
    )
    await transport.transport.aclose()
    transport.transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx2.ByteStream(b"untrusted-compressed-data"),
    ))
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.ReadError, match="uncompressed"):
            await client.get("http://127.0.0.1:12345/mcp")
