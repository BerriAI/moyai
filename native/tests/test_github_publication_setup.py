"""Exercise GitHub setup routes with real storage and token logic, synthetic GitHub I/O."""

import copy
import html
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import test_product_connections as connections
from app.connector_errors import ConnectorError
from app.github import MANIFEST_PERMISSIONS, PERMISSIONS, PROTECTION_PERMISSIONS, supports_permissions
from app.github_setup import routes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

bridge = connections.bridge


def setup_app(bridge, monkeypatch, permissions):
    github = bridge.connectors.github
    minted = []
    app_info = {"id": 456, "slug": "moyai-test", "pem": "synthetic-key", "permissions": MANIFEST_PERMISSIONS,
                "owner": {"id": 42, "login": "example", "type": "Organization"}}
    repository = {"id": 123, "full_name": "example/repo", "owner": {"id": 42}}

    async def request(method, path, **kwargs):
        if path == "/orgs/example":
            return {"id": 42, "login": "example", "type": "Organization"}
        if path == "/app" or path.startswith("/app-manifests/"):
            return copy.deepcopy(app_info)
        if path == "/app/installations/789":
            return {"id": 789, "account": app_info["owner"], "permissions": permissions, "suspended_at": None}
        if path == "/app/installations/789/access_tokens":
            scopes = kwargs["json"]["permissions"]
            assert supports_permissions(permissions, scopes)
            minted.append(copy.deepcopy(kwargs["json"]))
            return {"token": "synthetic-token", "expires_at": "2099-01-01T00:00:00Z"}
        if path == "/installation/repositories":
            return {"repositories": [repository]}
        raise AssertionError((method, path))

    monkeypatch.setattr(github, "request", request)
    monkeypatch.setattr(github, "app_jwt", lambda *args: "synthetic-jwt")
    security = SimpleNamespace(require=lambda *args, **kwargs: "synthetic-admin-session")
    app = FastAPI()
    app.include_router(routes(bridge.connectors, security, bridge.store, bridge.settings))

    @app.exception_handler(ConnectorError)
    async def connector_error(request: Request, error: ConnectorError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    return app, app_info, minted


@pytest.mark.parametrize("granted", [False, True])
async def test_registration_manifest_and_installation_agree_on_publication(bridge, monkeypatch, granted):
    permissions = {**PERMISSIONS, **(PROTECTION_PERMISSIONS if granted else {})}
    app, _, minted = setup_app(bridge, monkeypatch, permissions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://moyai.test") as client:
        started = await client.post("/api/connections/github/oauth", json={"organization": "example"})
        assert started.status_code == 200
        registration_url = started.json()["url"]
        page = await client.get(registration_url)
        assert page.status_code == 200
        match = re.search(r'name="manifest" value="([^"]+)"', page.text)
        assert match is not None
        manifest = json.loads(html.unescape(match[1]))
        assert supports_permissions(manifest["default_permissions"], {**PERMISSIONS, **PROTECTION_PERMISSIONS})
        state = parse_qs(urlsplit(registration_url).query)["state"][0]
        registered = await client.get("/oauth/github/app-callback", params={"state": state, "code": "synthetic-code"})
        assert registered.status_code == 303
        state = parse_qs(urlsplit(registered.headers["location"]).query)["state"][0]
        installed = await client.get("/oauth/github/callback", params={
            "state": state, "installation_id": "789", "setup_action": "install",
        })
        if not granted:
            assert installed.status_code == 400
            assert "Administration: read" in installed.json()["detail"]
            assert "approve the updated installation permissions" in installed.json()["detail"]
            assert not bridge.connectors.github.saved_credentials()
            return
        assert installed.status_code == 303
        credentials = bridge.connectors.github.saved_credentials()
        assert credentials["repository_ids"] == [123]
        # The real token method can now issue precisely the preflight scope.
        await bridge.connectors.github.installation_token(credentials, repository=123, protection=True)
        assert minted[-1] == {"repository_ids": [123], "permissions": {"administration": "read"}}
        refreshed = await client.post("/api/connections/github/refresh")
        assert refreshed.status_code == 200 and refreshed.json()["connected"] is True
        # Removing permission is detected on refresh without deleting the connection.
        permissions.pop("administration")
        refreshed = await client.post("/api/connections/github/refresh")
        assert refreshed.status_code == 400 and "Administration: read" in refreshed.json()["detail"]
        assert bridge.connectors.github.saved_credentials() == credentials


@pytest.mark.parametrize("administration", [None, "read", "write"])
async def test_existing_app_is_validated_before_saving(bridge, monkeypatch, administration):
    app, app_info, _ = setup_app(bridge, monkeypatch, dict(PERMISSIONS))
    app_info["permissions"] = {**PERMISSIONS, **({"administration": administration} if administration else {})}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://moyai.test") as client:
        response = await client.post("/api/connections/github/app", json={"app_id": 456, "private_key": "x" * 120})
    if administration:
        assert response.status_code == 200
        assert response.json()["url"].startswith("https://github.com/apps/moyai-test/installations/new?")
    else:
        assert response.status_code == 400 and "Administration: read" in response.json()["detail"]
        assert not bridge.connectors.github.app_config()
