"""Real native response shapes must retain same-record attention evidence."""

from types import SimpleNamespace

from pydantic_ai.messages import ToolCallPart

from moyai.contracts import Grant, RunScope
from moyai.dot_contracts import DotEvidence
from moyai.dot_discovery_agent import DiscoveryBounds
from moyai.dot_evidence import match


async def scan(provider, tool, payload, args=None):
    grant = Grant(
        id="account-1", kind=provider, revision=1, credential="sealed",
        policy={"allowed_tools": [tool]},
    )
    scope = RunScope(session_id="session", turn_id="turn", actor_id="owner", model="test", grants=[grant])
    return await DiscoveryBounds().after_tool_execute(
        SimpleNamespace(deps=scope),
        call=ToolCallPart(f"{provider}_account-_{tool}", {}, "read"),
        tool_def=None, args=args or {}, result=payload,
    )


async def test_notion_page_properties_keep_identity_version_and_quotable_content():
    result = await scan("notion", "discover", {
        "object": "list", "has_more": False, "next_cursor": None,
        "results": [{
            "object": "page", "id": "page-one", "url": "https://www.notion.so/page-one",
            "last_edited_time": "2026-11-26T09:00:00.000Z",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Review the launch plan"}]},
                "Status": {"type": "status", "status": {"name": "Needs review"}},
                "Due": {"type": "date", "date": {"start": "2026-11-27", "end": None}},
            },
        }],
    })
    record = next(row for row in result["records"] if row.get("id") == "page-one")
    assert record["revision"] == "2026-11-26T09:00:00.000Z"
    assert record["property:Name"] == "Review the launch plan"
    assert record["property:Status"] == "Needs review"
    assert record["property:Due:start"] == "2026-11-27"
    assert not result["limited"] and result["scanned"]
    assert match(DotEvidence(
        connection_id="account-1", source=record["url"], source_version=record["revision"],
        quote="Review the launch plan",
    ), "account-1", record)


async def test_notion_block_content_never_borrows_text_from_child_records():
    result = await scan("notion", "page", {"results": [{
        "object": "block", "id": "block-one", "type": "to_do",
        "last_edited_time": "2026-11-26T09:00:00.000Z",
        "to_do": {"rich_text": [{"plain_text": "Prepare meeting notes"}], "checked": False,
                  "children": [{"paragraph": {"rich_text": [{"plain_text": "Unrelated private child"}]}}]},
    }], "has_more": False})
    record = next(row for row in result["records"] if row.get("id") == "block-one")
    assert record["text"] == "Prepare meeting notes"
    assert record["checked"] is False
    assert "Unrelated private child" not in str(result)
    assert not result["limited"]


async def test_slack_search_and_thread_use_same_identity_and_observed_revision():
    message = {"type": "message", "ts": "1795683600.000001", "text": "Could you review this today?"}
    search = await scan("slack", "search", {
        "ok": True, "messages": {"matches": [{
            **message, "channel": {"id": "C123"}, "permalink": "https://example.slack.com/archives/C123/p1795683600000001",
        }], "paging": {"page": 1, "pages": 1}},
        "coverage": {"has_more": False},
    })
    thread = await scan("slack", "thread", {
        "ok": True, "messages": [message], "has_more": False,
    }, {"channel": "C123", "thread_ts": message["ts"]})
    first = next(row for row in search["records"] if row.get("id") == "slack:C123:1795683600.000001")
    second = next(row for row in thread["records"] if row.get("id") == first["id"])
    assert first["revision"] == second["revision"]
    assert not search["limited"] and not thread["limited"]
    assert match(DotEvidence(
        connection_id="account-1", source=first["id"], source_version=first["revision"],
        quote="review this today",
    ), "account-1", first) == ("account-1", first["id"], first["revision"])


async def test_slack_revision_changes_for_edits_and_new_replies_not_pagination():
    message = {"type": "message", "ts": "1795683600.000001", "text": "Review today"}
    async def revision(row):
        result = await scan("slack", "channel", {"messages": [row], "has_more": False}, {"channel": "C123"})
        return next(item["revision"] for item in result["records"] if item.get("id", "").startswith("slack:"))
    original = await revision(message)
    assert await revision({**message, "edited": {"ts": "1795683700.000001"}, "text": "Review tomorrow"}) != original
    assert await revision({**message, "reply_count": 1, "latest_reply": "1795683800.000001"}) != original


async def test_slack_messages_without_verified_channel_are_not_promoted_to_evidence():
    result = await scan("slack", "search", {
        "messages": {"matches": [{"ts": "1795683600.000001", "text": "No channel"}]},
    })
    assert result["limited"]
    assert not any(row.get("revision") for row in result["records"])


async def test_notion_oversized_text_remains_partial_and_does_not_mix_records():
    result = await scan("notion", "object", {
        "object": "page", "id": "page-one", "last_edited_time": "2026-11-26T09:00:00.000Z",
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "a" * 3000}]}},
    })
    record = next(row for row in result["records"] if row.get("id") == "page-one")
    assert record["property:Name"] == "a" * 2000
    assert result["limited"]
