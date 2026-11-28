"""Real PostgreSQL publication and owner controls with synthetic provider evidence."""

from __future__ import annotations

import pytest
import test_connection_attention as connection_fixtures
import test_google_attention as enrollment
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from sqlalchemy import func, select

from moyai.attention_policy import attention_reads
from moyai.dot_contracts import DotJudgment, DotResearch
from moyai.dot_models import DotCanonicalItem, DotItem
from moyai.models import Connection

pytestmark = connection_fixtures.pytestmark
admitted_product = connection_fixtures.admitted_product
automations = connection_fixtures.automations
canonical_connections = connection_fixtures.canonical_connections
google = connection_fixtures.google
connected = connection_fixtures.connected
remote_reads = connection_fixtures.remote_reads


async def publish_item(ctx, prepared, *, version="one"):
    grant = prepared.scope.grants[0]
    title, source = "Synthetic attention review item", "https://example.test/review-item"
    report = DotResearch.model_validate({
        "coverage": "complete", "observations": [{
            "title": title, "summary": title,
            "evidence": [{
                "connection_id": grant.id, "source": source, "source_version": version, "quote": title,
            }],
        }],
    })
    operation = min(attention_reads(grant))
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[ToolReturnPart(
            tool_name=f"{grant.kind}_{grant.id[:8]}_{operation}", tool_call_id="review-read",
            content={
                "dot_scan": True, "scanned": True, "limited": False, "error": False,
                "records": [{"url": source, "version": version, "title": title, "state": "open"}],
            },
        )]),
    ])).decode()
    await ctx.product.database.dots.discovery.judgment_input(
        prepared.scope, report.model_dump_json(), conversation,
    )
    judgment = DotJudgment.model_validate({
        "items": [{"observation": 0, "state": "open"}], "notify_observation": None, "message": "",
    })
    assert await ctx.product.database.save_result(
        prepared.scope, output=judgment.model_dump_json(), conversation=conversation, workspace=None, usage={},
    )
    await ctx.product.database.settle(prepared.scope.turn_id)


@pytest.mark.parametrize("provider", [
    "github", "linear", "notion", "gmail", "google_calendar", "slack", "mcp", "litellm",
])
async def test_every_supported_provider_can_publish_list_and_edit_followups(connected, remote_reads, provider):
    ctx = connected
    if provider == "slack":
        grant = await connection_fixtures.personal_slack(ctx)
        enabled = await ctx.client.patch(f"/api/connections/{grant.id}/attention", json={
            "revision": grant.revision, "consent_revision": 0, "use_proactively": True,
        })
        assert enabled.status_code == 200, enabled.text
    elif provider in {"mcp", "litellm"}:
        await connection_fixtures.native_connect(ctx, provider)
    else:
        await enrollment.connect(ctx, provider)
    _, prepared = await enrollment.admit(ctx)
    grant = prepared.scope.grants[0]
    await publish_item(ctx, prepared)
    listed = await ctx.client.get("/api/dot/items")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 1
    item = listed.json()["items"][0]
    assert item["connection_id"] == grant.id
    changed = await ctx.client.patch(f"/api/dot/items/{item['id']}", json={
        "revision": item["revision"], "state": "dismissed",
    })
    assert changed.status_code == 200 and changed.json()["state"] == "dismissed"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(DotItem)) == int(provider != "slack")
        assert await db.scalar(select(func.count()).select_from(DotCanonicalItem)) == int(provider == "slack")
        if provider == "slack":
            assert await db.get(Connection, grant.id) is None
        # Storing/viewing attention never replaces the real chat grant.
        assert await ctx.product.database.resolve_grants(db, ctx.product.user_id, [grant.id]) == [grant]


async def test_canonical_slack_owner_controls_deduplication_and_revocation(connected):
    from moyai import connection_attention

    ctx = connected
    grant = await connection_fixtures.personal_slack(ctx)
    enabled = await ctx.client.patch(f"/api/connections/{grant.id}/attention", json={
        "revision": grant.revision, "consent_revision": 0, "use_proactively": True,
    })
    assert enabled.status_code == 200
    _, prepared = await enrollment.admit(ctx)
    await publish_item(ctx, prepared)
    item = (await ctx.client.get("/api/dot/items")).json()["items"][0]
    edited = await ctx.client.patch(f"/api/dot/items/{item['id']}", json={
        "revision": item["revision"], "state": "dismissed",
    })
    assert edited.status_code == 200
    _, later = await enrollment.admit(ctx)
    await publish_item(ctx, later, version="two")
    listed = (await ctx.client.get("/api/dot/items")).json()["items"]
    assert len(listed) == 1 and listed[0]["state"] == "dismissed"
    await connection_fixtures.personal_slack(ctx, epoch="replaced")
    await connection_attention.provider_changed(ctx.runtime, "slack", owner=ctx.product.user_id)
    assert (await ctx.client.get("/api/dot/items")).json()["items"] == []
    revoked = await ctx.client.patch(f"/api/dot/items/{item['id']}", json={
        "revision": edited.json()["revision"], "state": "open",
    })
    assert revoked.status_code in {403, 404, 409}
