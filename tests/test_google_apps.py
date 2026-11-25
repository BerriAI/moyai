"""Exercise Google's real OAuth SDK with substituted HTTP responses only."""
import base64
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from pydantic import JsonValue, TypeAdapter
from requests.adapters import BaseAdapter
from urllib3.response import HTTPResponse

from app import google_apps
from app.connector_errors import ConnectorError

CLIENT_ID = "google-test-client"
CLIENT_SECRET = "synthetic-google-client-secret"
ACCESS = "synthetic-google-access-token"
REFRESH = "synthetic-google-refresh-token"
VERIFIER = "v" * 64
GoogleProvider = google_apps.GoogleProvider
type Timeout = float | tuple[float | None, float | None] | None


class Connectors:
    def oauth_client(self, provider: GoogleProvider, *, personal: bool = False) -> tuple[str, str]:
        assert provider in google_apps.GOOGLE_PROVIDERS and personal is True
        return CLIENT_ID, CLIENT_SECRET

    def redirect_uri(self, provider: GoogleProvider) -> str:
        return f"https://moyai.test/oauth/{provider}/callback"


@pytest.fixture
def connectors() -> Connectors:
    return Connectors()


class GoogleHTTP(BaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.queue: list[tuple[int, JsonValue] | Exception] = []
        self.calls: list[tuple[requests.PreparedRequest, dict[str, Timeout], int]] = []
        self.closed: list[int] = []
        self.headers: dict[str, str] = {}

    def send(
        self, request: requests.PreparedRequest, stream: bool = False, timeout: Timeout = None,
        verify: bool | str = True, cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        self.calls.append((request, {"timeout": timeout}, threading.get_ident()))
        assert request.url in {google_apps.TOKEN_URL, google_apps.USERINFO_URL}
        queued = self.queue.pop(0)
        if isinstance(queued, Exception):
            raise queued
        status, payload = queued
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response.headers.update(self.headers)
        response.raw = HTTPResponse(body=BytesIO(json.dumps(payload).encode()), preload_content=False)
        return response

    def close(self) -> None:
        self.closed.append(threading.get_ident())


@pytest.fixture
def google_http() -> GoogleHTTP:
    return GoogleHTTP()


def token(provider: GoogleProvider, **extra: JsonValue) -> dict[str, JsonValue]:
    return {
        "access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer", "expires_in": 3600,
        "scope": " ".join([*google_apps.IDENTITY_SCOPES, *google_apps.SCOPES[provider]]), **extra,
    }


def profile(**extra: JsonValue) -> dict[str, JsonValue]:
    return {"sub": "google-subject-123", "email": "owner@example.test", "email_verified": True, **extra}


def request_body(request: requests.PreparedRequest) -> dict[str, list[str]]:
    assert isinstance(request.body, str)
    return parse_qs(request.body)


def assert_safe(error: ConnectorError) -> dict[str, JsonValue]:
    receipt = TypeAdapter(dict[str, JsonValue]).validate_python(error.receipt(), strict=True)
    assert all(secret not in repr(receipt) for secret in (CLIENT_SECRET, ACCESS, REFRESH, VERIFIER))
    assert error.__suppress_context__ is True
    return receipt


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
def test_authorization_url_has_s256_exact_readonly_scopes_and_offline_access(
    connectors: Connectors, provider: GoogleProvider,
) -> None:
    url = google_apps.authorization_url(connectors, provider, "one-time-state", VERIFIER)
    parsed = urlsplit(url)
    assert parsed.scheme == "https" and parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"] and query["state"] == ["one-time-state"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"https://moyai.test/oauth/{provider}/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("="),
    ]
    assert set(query["scope"][0].split()) == set(google_apps.IDENTITY_SCOPES) | set(google_apps.SCOPES[provider])
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent select_account"]
    assert query["include_granted_scopes"] == ["false"]
    assert CLIENT_SECRET not in url and VERIFIER not in url


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_code_exchange_uses_sdk_verifier_and_verified_subject(
    connectors: Connectors, google_http: GoogleHTTP, provider: GoogleProvider,
) -> None:
    google_http.queue.extend([(200, token(provider)), (200, profile())])
    before = time.time()
    result = await google_apps.exchange(
        connectors, provider, code="synthetic-code", code_verifier=VERIFIER, http_adapter=google_http,
    )
    request, options, thread = google_http.calls[0]
    assert request.method == "POST" and request.url == google_apps.TOKEN_URL
    body = request_body(request)
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["synthetic-code"]
    assert body["code_verifier"] == [VERIFIER]
    assert body["redirect_uri"] == [f"https://moyai.test/oauth/{provider}/callback"]
    assert options["timeout"] == 20 and thread != threading.get_ident()
    info, options, _ = google_http.calls[1]
    assert info.method == "GET" and info.url == google_apps.USERINFO_URL
    assert info.headers["Authorization"] == f"Bearer {ACCESS}" and options["timeout"] == 15
    assert info.url is not None and ACCESS not in info.url and REFRESH not in info.url
    assert result["subject"] == "google-subject-123" and result["label"] == "owner@example.test"
    assert result["access_token"] == ACCESS and result["refresh_token"] == REFRESH
    assert before + 3600 <= result["expires_at"] <= time.time() + 3600
    assert isinstance(result, dict) and set(result) == {
        "kind", "access_token", "refresh_token", "expires_at", "scope", "subject", "label",
    }
    assert len(google_http.closed) == 1 and google_http.closed[0] != threading.get_ident()


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("rotates", [False, True])
async def test_refresh_sends_only_refresh_grant_and_retains_or_rotates_refresh_token(
    connectors: Connectors, google_http: GoogleHTTP, provider: GoogleProvider, rotates: bool,
) -> None:
    response = token(provider, access_token="new-access")
    if not rotates:
        response.pop("refresh_token")
    google_http.queue.extend([(200, response), (200, profile())])
    result = await google_apps.exchange(connectors, provider, refresh_token="old-refresh", http_adapter=google_http)
    request, options, _ = google_http.calls[0]
    body = request_body(request)
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["old-refresh"]
    assert body["client_id"] == [CLIENT_ID] and body["client_secret"] == [CLIENT_SECRET]
    assert not {"code", "code_verifier", "redirect_uri"} & body.keys()
    assert set(body["scope"][0].split()) == set(google_apps.IDENTITY_SCOPES) | set(google_apps.SCOPES[provider])
    assert options["timeout"] == 20
    assert result["refresh_token"] == (REFRESH if rotates else "old-refresh")
    assert result["access_token"] == "new-access" and result["subject"] == "google-subject-123"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_refresh_accepts_sdk_resolved_unchanged_scopes_when_response_omits_scope(
    connectors: Connectors, google_http: GoogleHTTP, provider: GoogleProvider,
) -> None:
    # OAuth permits omission when the grant's scope is unchanged. OAuthlib
    # resolves this on OAuth2Token.scopes, without adding a "scope" dict entry.
    response = token(provider)
    response.pop("scope")
    response.pop("refresh_token")
    google_http.queue.extend([(200, response), (200, profile())])
    result = await google_apps.exchange(connectors, provider, refresh_token="old-refresh", http_adapter=google_http)
    assert set(result["scope"].split()) == set(google_apps.IDENTITY_SCOPES) | set(google_apps.SCOPES[provider])
    assert result["refresh_token"] == "old-refresh"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("scope", ["", "openid https://www.googleapis.com/auth/userinfo.email"])
async def test_refresh_never_treats_explicit_scope_removal_as_unchanged(
    connectors: Connectors, google_http: GoogleHTTP, provider: GoogleProvider, scope: str,
) -> None:
    # An absent field and an explicitly empty/reduced grant are different.
    google_http.queue.append((200, token(provider, scope=scope)))
    with pytest.raises(ConnectorError):
        await google_apps.exchange(connectors, provider, refresh_token="old-refresh", http_adapter=google_http)
    assert len(google_http.calls) == 1
    assert len(google_http.closed) == 1


@pytest.mark.parametrize("case", ["missing_refresh", "missing_scope", "unverified_email", "missing_subject", "http_error"])
async def test_failed_consent_is_safe_and_closes_session(
    connectors: Connectors, google_http: GoogleHTTP, case: str,
) -> None:
    response, user = token("gmail"), profile()
    if case == "missing_refresh":
        response.pop("refresh_token")
    if case == "missing_scope":
        response["scope"] = " ".join(google_apps.IDENTITY_SCOPES)
    if case == "unverified_email":
        user["email_verified"] = False
    if case == "missing_subject":
        user.pop("sub")
    google_http.queue.extend([
        (400, {"error": "invalid_grant", "error_description": CLIENT_SECRET + ACCESS + REFRESH})
        if case == "http_error" else (200, response),
        (200, user),
    ])
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(
            connectors, "gmail", code="synthetic-code", code_verifier=VERIFIER, http_adapter=google_http,
        )
    assert_safe(failure.value)
    assert len(google_http.closed) == 1
    assert len(google_http.calls) == (2 if case in {"unverified_email", "missing_subject"} else 1)


@pytest.mark.parametrize("refreshing", [False, True])
@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 502, 503])
async def test_token_http_failure_classification_survives_non_oauth_error_bodies(
    connectors: Connectors, google_http: GoogleHTTP, refreshing: bool, status: int,
) -> None:
    google_http.headers["Retry-After"] = "30"
    google_http.queue.append((status, CLIENT_SECRET + ACCESS + REFRESH))
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(
            connectors, "gmail", code=None if refreshing else "synthetic-code",
            refresh_token=REFRESH if refreshing else None, code_verifier=VERIFIER, http_adapter=google_http,
        )
    receipt = assert_safe(failure.value)
    transient = status not in {401, 403}
    assert failure.value.retryable is transient and receipt["dispatched"] is False
    assert failure.value.code == (
        "authentication_required" if not transient else "rate_limited" if status == 429
        else "provider_unavailable"
    )
    assert receipt["status_code"] == status
    assert receipt["retry_after_seconds"] == (30 if transient else None)
    assert len(google_http.calls) == len(google_http.closed) == 1


@pytest.mark.parametrize("refreshing", [False, True])
@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "server_error", "temporarily_unavailable"])
async def test_sdk_oauth_error_distinguishes_authentication_from_temporary_failure(
    connectors: Connectors, google_http: GoogleHTTP, refreshing: bool, error: str,
) -> None:
    google_http.queue.append((400, {"error": error, "error_description": CLIENT_SECRET + ACCESS + REFRESH}))
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(
            connectors, "gmail", code=None if refreshing else "synthetic-code",
            refresh_token=REFRESH if refreshing else None, http_adapter=google_http,
        )
    transient = error in {"server_error", "temporarily_unavailable"}
    assert failure.value.retryable is transient
    assert failure.value.code == ("provider_unavailable" if transient else "authentication_required")
    assert_safe(failure.value)
    assert len(google_http.calls) == len(google_http.closed) == 1


@pytest.mark.parametrize("during_profile", [False, True])
@pytest.mark.parametrize("exception", [requests.Timeout, requests.ConnectionError])
async def test_network_failures_remain_retryable_and_close_the_session(
    connectors: Connectors, google_http: GoogleHTTP, during_profile: bool, exception: type[Exception],
) -> None:
    if during_profile:
        google_http.queue.append((200, token("gmail")))
    google_http.queue.append(exception(CLIENT_SECRET + ACCESS + REFRESH))
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(connectors, "gmail", refresh_token=REFRESH, http_adapter=google_http)
    assert_safe(failure.value)
    assert failure.value.retryable is True and failure.value.code == "provider_unavailable"
    assert len(google_http.calls) == (2 if during_profile else 1)
    assert len(google_http.closed) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_profile_http_failure_does_not_confuse_outages_with_revocation(
    connectors: Connectors, google_http: GoogleHTTP, status: int,
) -> None:
    google_http.queue.extend([(200, token("gmail")), (status, {"error": ACCESS + REFRESH})])
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(connectors, "gmail", refresh_token=REFRESH, http_adapter=google_http)
    receipt = assert_safe(failure.value)
    assert failure.value.retryable is (status in {429, 503})
    assert receipt["status_code"] == status
    assert failure.value.code == (
        "authentication_required" if status in {401, 403} else "rate_limited" if status == 429
        else "provider_unavailable"
    )
    assert len(google_http.calls) == 2 and len(google_http.closed) == 1


@pytest.mark.parametrize(("field", "value"), [
    ("access_token", 123), ("access_token", " "), ("refresh_token", 123),
    ("expires_in", True), ("expires_in", 0), ("expires_in", float("nan")), ("expires_in", float("inf")),
])
async def test_malformed_token_fields_never_become_credentials(
    connectors: Connectors, google_http: GoogleHTTP, field: str, value: JsonValue,
) -> None:
    google_http.queue.append((200, token("gmail", **{field: value})))
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(connectors, "gmail", refresh_token=REFRESH, http_adapter=google_http)
    assert_safe(failure.value)
    assert not failure.value.retryable
    assert len(google_http.calls) == len(google_http.closed) == 1


@pytest.mark.parametrize("payload", [
    [], None, {"sub": 123, "email": "owner@example.test", "email_verified": True},
    {"sub": "subject", "email": "owner@example.test", "email_verified": 1},
    {"sub": "subject", "email": "owner@example.test", "email_verified": "true"},
    {"sub": "subject", "email": "not-an-email", "email_verified": True},
])
async def test_profile_boundary_rejects_coercion_and_nonobjects(
    connectors: Connectors, google_http: GoogleHTTP, payload: JsonValue,
) -> None:
    google_http.queue.extend([(200, token("gmail")), (200, payload)])
    with pytest.raises(ConnectorError) as failure:
        await google_apps.exchange(connectors, "gmail", refresh_token=REFRESH, http_adapter=google_http)
    assert_safe(failure.value)
    assert failure.value.code == "invalid_response" and not failure.value.retryable
    assert len(google_http.calls) == 2 and len(google_http.closed) == 1
