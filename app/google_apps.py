"""Google's OAuth library for personal, read-only Workspace connections."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from google_auth_oauthlib.flow import Flow

from .connector_errors import ConnectorError

GOOGLE_PROVIDERS = frozenset({"gmail", "google_calendar"})
SCOPES = {
    "gmail": ("https://www.googleapis.com/auth/gmail.readonly",),
    "google_calendar": (
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ),
}
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def flow(connectors: Any, provider: str, verifier: str = "") -> Flow:
    client_id, client_secret = connectors.oauth_client(provider, personal=True)
    if not client_id or not client_secret or provider not in GOOGLE_PROVIDERS:
        raise ConnectorError("Google app connections are not configured.", dispatched=False)
    return Flow.from_client_config(
        {"web": {
            "client_id": client_id, "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth", "token_uri": TOKEN_URL,
        }},
        scopes=[*IDENTITY_SCOPES, *SCOPES[provider]],
        redirect_uri=connectors.redirect_uri(provider),
        code_verifier=verifier or None,
    )


def authorization_url(connectors: Any, provider: str, state: str, verifier: str) -> str:
    url, _ = flow(connectors, provider, verifier).authorization_url(
        state=state, access_type="offline", prompt="consent select_account",
        include_granted_scopes="false",
    )
    return url


async def exchange(
    connectors: Any, provider: str, *, code: str | None = None,
    refresh_token: str | None = None, code_verifier: str | None = None,
) -> dict[str, Any]:
    """The SDK owns code exchange/refresh; native OAuth owns state and storage."""
    def redeem() -> dict[str, Any]:
        oauth = flow(connectors, provider, code_verifier or "")
        try:
            if refresh_token:
                client_id, client_secret = connectors.oauth_client(provider, personal=True)
                token = oauth.oauth2session.refresh_token(
                    TOKEN_URL, refresh_token=refresh_token, client_id=client_id,
                    client_secret=client_secret, timeout=20,
                )
            else:
                token = oauth.fetch_token(code=code, timeout=20)
            access = token.get("access_token")
            lifetime = token.get("expires_in")
            # OAuthlib resolves an omitted, unchanged scope from the requested
            # grant. An explicitly empty/reduced scope must still fail closed.
            scopes = token.get("scope", getattr(token, "scopes", []))
            granted = set(scopes.split() if isinstance(scopes, str) else scopes)
            if (
                not isinstance(access, str) or not access
                or not set(SCOPES[provider]).issubset(granted)
                or type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0
            ):
                raise ValueError("Incomplete Google authorization.")
            refresh = token.get("refresh_token") or refresh_token
            if not isinstance(refresh, str) or not refresh:
                raise ValueError("Offline access was not granted.")
            profile_response = oauth.oauth2session.get(USERINFO_URL, timeout=15)
            profile_response.raise_for_status()
            profile = profile_response.json()
            subject, email = profile.get("sub"), profile.get("email")
            if (
                not isinstance(subject, str) or not subject or len(subject) > 255
                or profile.get("email_verified") is not True
                or not isinstance(email, str) or "@" not in email or len(email) > 254
            ):
                raise ValueError("Google did not verify this account.")
            return {
                "kind": "oauth", "access_token": access, "refresh_token": refresh,
                "expires_at": time.time() + lifetime, "scope": " ".join(sorted(granted)),
                "subject": subject, "label": email,
            }
        finally:
            oauth.oauth2session.close()

    try:
        return await asyncio.to_thread(redeem)
    except Exception:  # noqa: BLE001 - provider errors may contain tokens or authorization codes.
        raise ConnectorError(
            "Google could not confirm read-only offline access. Reconnect and approve the requested permissions.",
            dispatched=False,
        ) from None
