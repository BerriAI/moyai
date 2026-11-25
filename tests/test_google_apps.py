"""Exercise Google's real OAuth SDK with substituted HTTP responses only."""
import base64
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app import google_apps
from app.connector_errors import ConnectorError

CLIENT_ID = "google-test-client"
CLIENT_SECRET = "synthetic-google-client-secret"
ACCESS = "synthetic-google-access-token"
REFRESH = "synthetic-google-refresh-token"
VERIFIER = "v" * 64


@pytest.fixture
def connectors():
    def oauth_client(provider, *, personal):
        assert provider in google_apps.GOOGLE_PROVIDERS and personal is True
        return CLIENT_ID, CLIENT_SECRET

    return SimpleNamespace(
        oauth_client=oauth_client,
        redirect_uri=lambda provider: f"https://moyai.test/oauth/{provider}/callback",
    )


@pytest.fixture
def google_http(monkeypatch):
    backend = SimpleNamespace(queue=[], calls=[], closed=[])
    original_close = requests.Session.close

    def send(session, request, **kwargs):
        backend.calls.append((request, kwargs, threading.get_ident()))
        assert request.url in {google_apps.TOKEN_URL, google_apps.USERINFO_URL}
        status, payload = backend.queue.pop(0)
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response

    def close(session):
        backend.closed.append(threading.get_ident())
        original_close(session)

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(requests.Session, "close", close)
    return backend


def token(provider, **extra):
    return {
        "access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer", "expires_in": 3600,
        "scope": " ".join([*google_apps.IDENTITY_SCOPES, *google_apps.SCOPES[provider]]), **extra,
    }


def profile(**extra):
    return {"sub": "google-subject-123", "email": "owner@example.test", "email_verified": True, **extra}


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
def test_authorization_url_has_s256_exact_readonly_scopes_and_offline_access(connectors, provider):
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
async def test_code_exchange_uses_sdk_verifier_and_verified_subject(connectors, google_http, provider):
    google_http.queue.extend([(200, token(provider)), (200, profile())])
    before = time.time()
    result = await google_apps.exchange(connectors, provider, code="synthetic-code", code_verifier=VERIFIER)
    request, options, thread = google_http.calls[0]
    assert request.method == "POST" and request.url == google_apps.TOKEN_URL
    body = parse_qs(request.body)
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["synthetic-code"]
    assert body["code_verifier"] == [VERIFIER]
    assert body["redirect_uri"] == [f"https://moyai.test/oauth/{provider}/callback"]
    assert options["timeout"] == 20 and thread != threading.get_ident()
    info, options, _ = google_http.calls[1]
    assert info.method == "GET" and info.url == google_apps.USERINFO_URL
    assert info.headers["Authorization"] == f"Bearer {ACCESS}" and options["timeout"] == 15
    assert ACCESS not in info.url and REFRESH not in info.url
    assert result["subject"] == "google-subject-123" and result["label"] == "owner@example.test"
    assert result["access_token"] == ACCESS and result["refresh_token"] == REFRESH
    assert before + 3600 <= result["expires_at"] <= time.time() + 3600
    assert len(google_http.closed) == 1 and google_http.closed[0] != threading.get_ident()


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("rotates", [False, True])
async def test_refresh_sends_only_refresh_grant_and_retains_or_rotates_refresh_token(
    connectors, google_http, provider, rotates,
):
    response = token(provider, access_token="new-access")
    if not rotates:
        response.pop("refresh_token")
    google_http.queue.extend([(200, response), (200, profile())])
    result = await google_apps.exchange(connectors, provider, refresh_token="old-refresh")
    request, options, _ = google_http.calls[0]
    body = parse_qs(request.body)
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["old-refresh"]
    assert body["client_id"] == [CLIENT_ID] and body["client_secret"] == [CLIENT_SECRET]
    assert not {"code", "code_verifier", "redirect_uri"} & body.keys()
    assert set(body["scope"][0].split()) == set(google_apps.IDENTITY_SCOPES) | set(google_apps.SCOPES[provider])
    assert options["timeout"] == 20
    assert result["refresh_token"] == (REFRESH if rotates else "old-refresh")
    assert result["access_token"] == "new-access" and result["subject"] == "google-subject-123"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_refresh_accepts_sdk_resolved_unchanged_scopes_when_response_omits_scope(
    connectors, google_http, provider,
):
    # OAuth permits omission when the grant's scope is unchanged. OAuthlib
    # resolves this on OAuth2Token.scopes, without adding a "scope" dict entry.
    response = token(provider)
    response.pop("scope")
    response.pop("refresh_token")
    google_http.queue.extend([(200, response), (200, profile())])
    result = await google_apps.exchange(connectors, provider, refresh_token="old-refresh")
    assert set(result["scope"].split()) == set(google_apps.IDENTITY_SCOPES) | set(google_apps.SCOPES[provider])
    assert result["refresh_token"] == "old-refresh"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("scope", ["", "openid https://www.googleapis.com/auth/userinfo.email"])
async def test_refresh_never_treats_explicit_scope_removal_as_unchanged(
    connectors, google_http, provider, scope,
):
    # An absent field and an explicitly empty/reduced grant are different.
    google_http.queue.append((200, token(provider, scope=scope)))
    with pytest.raises(ConnectorError):
        await google_apps.exchange(connectors, provider, refresh_token="old-refresh")
    assert len(google_http.calls) == 1
    assert len(google_http.closed) == 1


@pytest.mark.parametrize("case", ["missing_refresh", "missing_scope", "unverified_email", "missing_subject", "http_error"])
async def test_failed_consent_is_safe_and_closes_session(connectors, google_http, case):
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
        await google_apps.exchange(connectors, "gmail", code="synthetic-code", code_verifier=VERIFIER)
    assert all(secret not in str(failure.value) for secret in (CLIENT_SECRET, ACCESS, REFRESH))
    assert failure.value.__suppress_context__ is True
    assert len(google_http.closed) == 1
    assert len(google_http.calls) == (2 if case in {"unverified_email", "missing_subject"} else 1)
