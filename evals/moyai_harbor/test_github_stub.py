import httpx
import pytest
from evals.moyai_harbor.github_stub import GitHubStub, block_github_dns


def test_github_stub_logs_token_and_mcp_requests_and_rejects_other_paths() -> None:
    stub = GitHubStub()
    stub.start()
    try:
        with httpx.Client() as client:
            token = client.post(f"{stub.api_url}/app/installations/201/access_tokens")
            catalog = client.post(
                stub.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            unexpected = client.get(f"{stub.api_url}/repositories/101")

        assert token.status_code == 201
        assert token.json()["expires_at"] == "2099-01-01T00:00:00Z"
        assert catalog.json()["result"]["tools"] == []
        assert unexpected.status_code == 404
        assert [request["path"] for request in stub.requests] == [
            "/app/installations/201/access_tokens",
            "/mcp/",
            "/repositories/101",
        ]
    finally:
        stub.close()


def test_github_dns_guard_blocks_real_hosts() -> None:
    import socket

    with block_github_dns(), pytest.raises(OSError, match="must use the local evaluation stub"):
        socket.getaddrinfo("api.github.com", 443)
