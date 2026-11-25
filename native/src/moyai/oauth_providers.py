"""Personal grants use the deployment's canonical OAuth apps and provider helpers."""

from __future__ import annotations

import base64
import hashlib
import math
from typing import TYPE_CHECKING, Literal

from app.connector_errors import ConnectorError
from app.db import database as source_database

if TYPE_CHECKING:
    from .product_connections import ProductConnections

# Slack keeps its existing PersonalSlack service, storage, callback and tools.
OAuthProvider = Literal["linear", "notion", "github", "google_calendar", "gmail"]
PROVIDERS: dict[OAuthProvider, str] = {
    "linear": "Linear", "notion": "Notion", "github": "GitHub",
    "google_calendar": "Google Calendar", "gmail": "Gmail",
}


async def client_identity(service: ProductConnections, provider: OAuthProvider) -> str:
    def read():
        identity, secret = service.connectors.oauth_client(provider, personal=True)
        return identity if identity and secret else ""
    return await source_database(read)


async def authorization_url(
    service: ProductConnections, provider: OAuthProvider, state: str, verifier: str,
) -> str:
    if provider in ("gmail", "google_calendar"):
        from app.google_apps import authorization_url as google_authorization_url
        return await source_database(google_authorization_url, service.connectors, provider, state, verifier)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return await source_database(
        service.connectors.authorization_url, provider, state, code_challenge=challenge, personal=True,
    )


async def exchange(
    service: ProductConnections, provider: OAuthProvider, identity: str, *,
    code: str = "", verifier: str = "", previous: dict | None = None,
) -> dict:
    """Reuse canonical exchange/verification without reading an organization token."""
    if not identity or await client_identity(service, provider) != identity:
        raise ConnectorError("The app configuration changed. Connect your account again.", dispatched=False)
    if previous is not None and not previous.get("refresh_token"):
        raise ConnectorError("This connection expired. Connect your account again.", dispatched=False)

    async def redeem(connectors):
        value = await connectors.exchange(
            provider, code=code or None, code_verifier=verifier or None,
            refresh_token=previous["refresh_token"] if previous else None,
            personal=True,
        )
        if (
            not isinstance(value.get("access_token"), str) or not value["access_token"].strip()
            or ("refresh_token" in value and not isinstance(value["refresh_token"], str))
            or ("expires_at" in value and (
                type(value["expires_at"]) not in (int, float)
                or not math.isfinite(value["expires_at"]) or value["expires_at"] <= 0
            ))
        ):
            raise ConnectorError("The provider returned invalid account credentials.", dispatched=False)
        if previous and not value.get("refresh_token"):
            value["refresh_token"] = previous["refresh_token"]
        if previous and provider in {"gmail", "google_calendar"} and value.get("subject") != previous.get("subject"):
            raise ConnectorError("The Google account changed. Connect it again.", dispatched=False)
        label = await connectors.verify(provider, value)
        return {**value, "client_id": identity, "label": label}

    value = await service.execution.call(redeem)
    if await client_identity(service, provider) != identity:
        raise ConnectorError("The app configuration changed. Connect your account again.", dispatched=False)
    return value
