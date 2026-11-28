"""Canonical OAuth HTTP/state tests; only Slack provider I/O is substituted."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import test_product_connections as connection_fixtures
from app.personal_slack import PersonalSlackAttentionChoice
from fastapi import FastAPI
from test_product_identity import google_cookie

bridge = connection_fixtures.bridge


def client_for(bridge, app):
    cookie, csrf = google_cookie(
        SimpleNamespace(legacy_security=bridge.security), sub="owner", email="owner@example.test",
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=bridge.settings.public_url,
        cookies={"workspace_session": cookie},
        headers={"X-CSRF-Token": csrf, "Origin": bridge.settings.public_url},
    )


def provider(bridge, monkeypatch):
    personal = bridge.connectors.personal_slack
    bridge.settings.slack_client_id = "synthetic-client"
    bridge.settings.slack_client_secret = "synthetic-client-secret"

    async def exchange(**_kwargs):
        return {"access_token": "synthetic-token", "user_id": "USYNTHETIC", "team_id": "TSYNTHETIC"}

    async def verify(_credentials):
        return "Synthetic personal Slack"

    monkeypatch.setattr(personal, "exchange", exchange)
    monkeypatch.setattr(personal, "verify", verify)
    return personal


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("selected", [False, True, None])
async def test_oauth_binds_choice_and_reports_status_after_committed_save(bridge, monkeypatch, previous, selected):
    personal = provider(bridge, monkeypatch)
    saved = {"consent": previous, "calls": []}

    async def choice(request, owner, revision, value, consent_revision):
        assert owner == "google:owner" and personal.row(owner)["revision"] == revision
        assert consent_revision == (None if value is None else 3)
        return PersonalSlackAttentionChoice(saved["consent"] if value is None else value, 3)

    async def changed(owner, *, request=None, revision=None, use_proactively=None, consent_revision=None):
        if request is not None:
            assert consent_revision == 3
            assert personal.row(owner)["revision"] == revision
            assert personal.row(owner)["encrypted"]
            saved["consent"] = use_proactively
        saved["calls"].append(use_proactively)

    async def status(owner, result):
        assert owner == "google:owner"
        result.use_proactively = saved["consent"]
        return result

    app = FastAPI()
    callback = personal.routes(app, after_change=changed, attention_choice=choice, attention_status=status)
    app.add_api_route("/oauth/slack/callback", callback, methods=["GET"], include_in_schema=False)
    async with client_for(bridge, app) as client:
        response = await client.post(
            "/api/connections/slack/personal/oauth",
            json={} if selected is None else {"use_proactively": selected, "consent_revision": 3},
        )
        assert response.status_code == 200, response.text
        state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
        expected = previous if selected is None else selected
        assert saved["consent"] is previous and saved["calls"] == []
        completed = await client.get("/oauth/slack/callback", params={"state": state, "code": "synthetic-code"})
        assert completed.status_code == 303, completed.text
        assert saved["calls"] == [expected]
        inventory = await client.get("/api/connections/slack/personal")
        assert inventory.status_code == 200 and inventory.json()["connected"]
        assert inventory.json()["use_proactively"] is expected
        replay = await client.get("/oauth/slack/callback", params={"state": state, "code": "synthetic-code"})
        assert replay.status_code == 400 and saved["calls"] == [expected]
        disconnected = await client.delete("/api/connections/slack/personal")
        assert disconnected.status_code == 200 and not disconnected.json()["connected"]
        assert saved["calls"] == [expected, None]


@pytest.mark.parametrize("boundary", ["tampered_choice", "cancelled", "revoked_grant"])
async def test_oauth_invalidated_or_cancelled_choice_never_enrolls(bridge, monkeypatch, boundary):
    personal = provider(bridge, monkeypatch)
    calls = []

    async def choice(_request, _owner, _revision, selected, _consent_revision):
        return PersonalSlackAttentionChoice(selected, 3)

    async def changed(*args, **kwargs):
        calls.append((args, kwargs))

    app = FastAPI()
    callback = personal.routes(app, after_change=changed, attention_choice=choice)
    app.add_api_route("/oauth/slack/callback", callback, methods=["GET"], include_in_schema=False)
    async with client_for(bridge, app) as client:
        started = await client.post("/api/connections/slack/personal/oauth", json={"use_proactively": True})
        assert started.status_code == 200, started.text
        state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
        params = {"state": state, "code": "synthetic-code"}
        if boundary == "tampered_choice":
            params["state"] = state.replace("attention-on:", "attention-off:")
        elif boundary == "cancelled":
            params["error"] = "access_denied"
        else:
            personal.disconnect("google:owner")
        response = await client.get("/oauth/slack/callback", params=params)
        assert response.status_code == {"tampered_choice": 400, "cancelled": 303, "revoked_grant": 409}[boundary]
        assert calls == [] and not personal.configured("google:owner")


async def test_legacy_without_attention_rejects_proactive_choice(bridge, monkeypatch):
    personal = provider(bridge, monkeypatch)
    app = FastAPI()
    personal.routes(app)
    async with client_for(bridge, app) as client:
        rejected = await client.post("/api/connections/slack/personal/oauth", json={"use_proactively": True})
        assert rejected.status_code == 409
        assert not bridge.store.rows("SELECT * FROM personal_slack_states")
        compatible = await client.post("/api/connections/slack/personal/oauth")
        assert compatible.status_code == 200
