"""Fixed first-party OAuth endpoints. Never discover a token recipient from user input."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .settings import Settings

OAuthProvider = Literal["linear", "notion", "github", "slack"]


class OAuthFailure(Exception):
    """Safe to display; never include a provider body, token or authorization code."""


class OAuthClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(min_length=1, max_length=4096)
    client_secret: SecretStr = SecretStr("")
    redirect_uri: str


class OAuthTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: SecretStr
    refresh_token: SecretStr = SecretStr("")
    expires_at: float = 0
    client: OAuthClient

    def stored(self) -> dict:
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value(),
            "expires_at": self.expires_at,
            "client": {
                **self.client.model_dump(exclude={"client_secret"}),
                "client_secret": self.client.client_secret.get_secret_value(),
            },
        }


@dataclass(frozen=True)
class Provider:
    name: str
    authorization_url: str
    token_url: str
    mcp_url: str
    scope: str
    registration_url: str = ""
    issuer: str = ""
    resource: str = ""


# Linear and Notion advertise RFC 7591 registration and S256 PKCE at their
# /.well-known/oauth-authorization-server endpoints. Slack does not offer DCR.
PROVIDERS: dict[OAuthProvider, Provider] = {
    "linear": Provider(
        "Linear", "https://mcp.linear.app/authorize", "https://mcp.linear.app/token",
        "https://mcp.linear.app/mcp", "read write",
        "https://mcp.linear.app/register", "https://mcp.linear.app", "https://mcp.linear.app/mcp",
    ),
    "notion": Provider(
        "Notion", "https://mcp.notion.com/authorize", "https://mcp.notion.com/token",
        "https://mcp.notion.com/mcp", "default",
        "https://mcp.notion.com/register", "https://mcp.notion.com", "https://mcp.notion.com/mcp",
    ),
    "github": Provider(
        "GitHub", "https://github.com/login/oauth/authorize", "https://github.com/login/oauth/access_token",
        "https://api.githubcopilot.com/mcp/", "repo read:user",
    ),
    "slack": Provider(
        "Slack", "https://slack.com/oauth/v2_user/authorize", "https://slack.com/api/oauth.v2.user.access",
        "https://mcp.slack.com/mcp",
        "channels:history,channels:read,groups:history,groups:read,im:history,im:read,"
        "mpim:history,mpim:read,chat:write,search:read.public,search:read.private,"
        "search:read.im,search:read.mpim,search:read.users,users:read",
    ),
}


def callback_uri(settings: Settings, provider: OAuthProvider) -> str:
    root = settings.public_url.rstrip("/")
    parsed = urlsplit(root)
    if (
        not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or "\\" in root or any(ord(char) <= 32 for char in root)
        or (parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ))
    ):
        raise OAuthFailure("The deployment administrator must configure Moyai's public URL.")
    return root + f"/oauth/personal/{provider}/callback"


def configured(settings: Settings, provider: OAuthProvider) -> bool:
    try:
        callback_uri(settings, provider)
    except OAuthFailure:
        return False
    if PROVIDERS[provider].registration_url:
        return True
    return bool(
        getattr(settings, f"personal_{provider}_client_id")
        and getattr(settings, f"personal_{provider}_client_secret").get_secret_value()
    )


async def provider_request(url: str, **kwargs) -> dict:
    """Small JSON responses, no redirects, no raw provider errors in logs or UI."""
    try:
        async with (
            httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client,
            client.stream("POST", url, headers={"Accept": "application/json"}, **kwargs) as response,
        ):
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise OAuthFailure("The provider returned an invalid authorization response.")
            value = json.loads(content)
            if not response.is_success or not isinstance(value, dict) or value.get("error") or value.get("ok") is False:
                raise OAuthFailure("Authorization was not confirmed. Try connecting again.")
            return value
    except (httpx.HTTPError, ValueError):
        raise OAuthFailure("The provider could not be reached. Try connecting again.") from None


async def register(settings: Settings, provider: OAuthProvider) -> OAuthClient:
    definition = PROVIDERS[provider]
    redirect_uri = callback_uri(settings, provider)
    if definition.registration_url:
        value = await provider_request(definition.registration_url, json={
            "client_name": "Moyai", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
        })
        identity = value.get("client_id")
        if not isinstance(identity, str) or not identity or len(identity) > 4096:
            raise OAuthFailure("The provider could not register Moyai. Try connecting again.")
        return OAuthClient(client_id=identity, redirect_uri=redirect_uri)
    if not configured(settings, provider):
        raise OAuthFailure(f"{definition.name} is not enabled on this deployment. Ask your Moyai administrator.")
    return OAuthClient(
        client_id=getattr(settings, f"personal_{provider}_client_id"),
        client_secret=getattr(settings, f"personal_{provider}_client_secret"),
        redirect_uri=redirect_uri,
    )


def authorization_url(provider: OAuthProvider, client: OAuthClient, state: str, verifier: str) -> str:
    definition = PROVIDERS[provider]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {
        "client_id": client.client_id, "redirect_uri": client.redirect_uri, "response_type": "code",
        "state": state, "scope": definition.scope, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if definition.resource:
        params["resource"] = definition.resource
    return definition.authorization_url + "?" + urlencode(params)


async def exchange(
    provider: OAuthProvider, client: OAuthClient, *, code: str = "", verifier: str = "",
    previous: OAuthTokens | None = None,
) -> OAuthTokens:
    definition = PROVIDERS[provider]
    payload = {"client_id": client.client_id}
    if client.client_secret.get_secret_value():
        payload["client_secret"] = client.client_secret.get_secret_value()
    if previous is not None:
        if not previous.refresh_token.get_secret_value():
            raise OAuthFailure("This connection expired. Connect your account again.")
        payload.update(grant_type="refresh_token", refresh_token=previous.refresh_token.get_secret_value())
    else:
        payload.update(
            grant_type="authorization_code", code=code, redirect_uri=client.redirect_uri, code_verifier=verifier,
        )
    if definition.resource:
        payload["resource"] = definition.resource
    value = await provider_request(definition.token_url, data=payload)
    token, refresh, expires = value.get("access_token"), value.get("refresh_token"), value.get("expires_in", 0)
    # Slack's user-only OAuth endpoint labels bearer credentials as "user".
    token_types = {"bearer", "user"} if provider == "slack" else {"bearer"}
    if (
        not isinstance(token, str) or not token or len(token) > 32768
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
        or not isinstance(value.get("token_type", "bearer"), str)
        or value.get("token_type", "bearer").lower() not in token_types
        or not isinstance(expires, (int, float)) or not 0 <= expires <= 10 * 365 * 86400
        or (refresh is not None and (not isinstance(refresh, str) or len(refresh) > 32768))
    ):
        raise OAuthFailure("The provider returned an invalid token. Connect your account again.")
    return OAuthTokens(
        access_token=SecretStr(token),
        refresh_token=SecretStr(refresh or (previous.refresh_token.get_secret_value() if previous else "")),
        expires_at=time.time() + expires if expires else 0, client=client,
    )
