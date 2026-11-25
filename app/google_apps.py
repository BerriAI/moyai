"""Google's OAuth library for personal, read-only Workspace connections."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal, Protocol, TypedDict, cast

from google_auth_oauthlib.flow import Flow
from oauthlib.oauth2.rfc6749.errors import (
    AccessDeniedError,
    InvalidClientError,
    InvalidGrantError,
    InvalidScopeError,
    InvalidTokenError,
    ServerError,
    TemporarilyUnavailableError,
    UnauthorizedClientError,
)
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from requests import ConnectionError, HTTPError, Response, Timeout
from requests.adapters import BaseAdapter

from .connector_errors import ConnectorError

GoogleProvider = Literal["gmail", "google_calendar"]
GOOGLE_PROVIDERS: frozenset[GoogleProvider] = frozenset({"gmail", "google_calendar"})
SCOPES: dict[GoogleProvider, tuple[str, ...]] = {
    "gmail": ("https://www.googleapis.com/auth/gmail.readonly",),
    "google_calendar": (
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ),
}
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleConnectors(Protocol):
    def oauth_client(self, provider: GoogleProvider, *, personal: bool = False) -> tuple[str, str]: ...

    def redirect_uri(self, provider: GoogleProvider) -> str: ...


class GoogleCredentials(TypedDict):
    kind: Literal["oauth"]
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    subject: str
    label: str


class _OAuthSession(Protocol):
    def mount(self, prefix: str, adapter: BaseAdapter) -> None: ...

    def register_compliance_hook(self, hook_type: str, hook: Callable[[Response], Response]) -> None: ...

    def refresh_token(
        self, token_url: str, *, refresh_token: str, client_id: str, client_secret: str, timeout: int,
    ) -> object: ...

    def get(self, url: str, *, timeout: int) -> Response: ...

    def close(self) -> None: ...


class OAuthFlow(Protocol):
    """The untyped Google SDK surface; provider payloads remain untrusted."""

    @property
    def oauth2session(self) -> _OAuthSession: ...

    def authorization_url(
        self, *, state: str, access_type: str, prompt: str, include_granted_scopes: str,
    ) -> tuple[str, str]: ...

    def fetch_token(self, *, code: str | None, timeout: int) -> object: ...


class _Token(BaseModel):
    model_config = ConfigDict(strict=True)

    access_token: str = Field(min_length=1)
    refresh_token: str | None = Field(default=None, min_length=1)
    expires_in: float = Field(gt=0, allow_inf_nan=False)
    scope: str | list[str] | None = None

    @field_validator("access_token", "refresh_token")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Empty token.")
        return value


class _Profile(BaseModel):
    model_config = ConfigDict(strict=True)

    sub: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=254)
    email_verified: bool

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("Invalid account email.")
        return value

    @field_validator("email_verified")
    @classmethod
    def verified(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Unverified account email.")
        return value


_SCOPES = TypeAdapter(list[str])


def _token_response(response: Response) -> Response:
    # OAuthlib parses token bodies without checking HTTP status. Preserve
    # HTTP failures before a proxy error page becomes "missing token".
    if response.status_code in {401, 403, 408, 429} or response.status_code >= 500:
        response.raise_for_status()
    return response


def flow(
    connectors: GoogleConnectors, provider: GoogleProvider, verifier: str = "",
    *, http_adapter: BaseAdapter | None = None,
) -> OAuthFlow:
    client_id, client_secret = connectors.oauth_client(provider, personal=True)
    if not client_id or not client_secret or provider not in GOOGLE_PROVIDERS:
        raise ConnectorError("Google app connections are not configured.", dispatched=False)
    oauth = cast(OAuthFlow, Flow.from_client_config(
        {"web": {
            "client_id": client_id, "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth", "token_uri": TOKEN_URL,
        }},
        scopes=[*IDENTITY_SCOPES, *SCOPES[provider]],
        redirect_uri=connectors.redirect_uri(provider),
        code_verifier=verifier or None,
    ))
    if http_adapter is not None:
        oauth.oauth2session.mount("https://", http_adapter)
    oauth.oauth2session.register_compliance_hook("access_token_response", _token_response)
    oauth.oauth2session.register_compliance_hook("refresh_token_response", _token_response)
    return oauth


def authorization_url(
    connectors: GoogleConnectors, provider: GoogleProvider, state: str, verifier: str,
) -> str:
    oauth = flow(connectors, provider, verifier)
    try:
        url, _ = oauth.authorization_url(
            state=state, access_type="offline", prompt="consent select_account",
            include_granted_scopes="false",
        )
        return TypeAdapter(str).validate_python(url, strict=True)
    finally:
        oauth.oauth2session.close()


def _failure(error: Exception) -> ConnectorError:
    if isinstance(error, ConnectorError):
        return error
    response = error.response if isinstance(error, HTTPError) else None
    status = response.status_code if response is not None else None
    if (
        isinstance(error, (Timeout, ConnectionError, ServerError, TemporarilyUnavailableError))
        or status in {408, 429}
        or (status is not None and status >= 500)
    ):
        retry = response.headers.get("Retry-After", "") if response is not None else ""
        return ConnectorError(
            "Google authorization is temporarily unavailable. Try again later.",
            code="rate_limited" if status == 429 else "provider_unavailable",
            retryable=True, dispatched=False, status_code=status,
            retry_after=min(int(retry), 86400) if retry.isascii() and retry.isdigit() and len(retry) <= 5 else None,
        )
    if isinstance(error, (
        AccessDeniedError, InvalidClientError, InvalidGrantError, InvalidScopeError,
        InvalidTokenError, UnauthorizedClientError,
    )) or status in {401, 403}:
        return ConnectorError(
            "Google authorization is no longer valid. Reconnect and approve the requested permissions.",
            code="authentication_required", dispatched=False, status_code=status,
        )
    return ConnectorError(
        "Google could not confirm read-only offline access. Try connecting again.",
        code="invalid_response", dispatched=False, status_code=status,
    )


async def exchange(
    connectors: GoogleConnectors, provider: GoogleProvider, *, code: str | None = None,
    refresh_token: str | None = None, code_verifier: str | None = None,
    http_adapter: BaseAdapter | None = None,
) -> GoogleCredentials:
    """The SDK owns code exchange/refresh; native OAuth owns state and storage."""
    def redeem() -> GoogleCredentials:
        oauth = flow(connectors, provider, code_verifier or "", http_adapter=http_adapter)
        try:
            if refresh_token:
                client_id, client_secret = connectors.oauth_client(provider, personal=True)
                token = oauth.oauth2session.refresh_token(
                    TOKEN_URL, refresh_token=refresh_token, client_id=client_id,
                    client_secret=client_secret, timeout=20,
                )
            else:
                token = oauth.fetch_token(code=code, timeout=20)
            validated = _Token.model_validate(token)
            # OAuthlib resolves an omitted, unchanged scope from the requested
            # grant. An explicitly empty/reduced scope must still fail closed.
            scopes = (
                validated.scope if "scope" in validated.model_fields_set
                else _SCOPES.validate_python(getattr(token, "scopes", []), strict=True)
            )
            if scopes is None:
                raise InvalidScopeError()
            granted = set(scopes.split() if isinstance(scopes, str) else scopes)
            if not set(SCOPES[provider]).issubset(granted):
                raise InvalidScopeError()
            refresh = validated.refresh_token or refresh_token
            if not refresh:
                raise InvalidGrantError()
            profile_response = oauth.oauth2session.get(USERINFO_URL, timeout=15)
            profile_response.raise_for_status()
            profile = _Profile.model_validate(profile_response.json())
            return {
                "kind": "oauth", "access_token": validated.access_token, "refresh_token": refresh,
                "expires_at": time.time() + validated.expires_in, "scope": " ".join(sorted(granted)),
                "subject": profile.sub, "label": profile.email,
            }
        finally:
            oauth.oauth2session.close()

    try:
        return await asyncio.to_thread(redeem)
    except Exception as error:  # noqa: BLE001 - SDK failures may contain tokens, codes or provider bodies.
        raise _failure(error) from None
