"""Thread payloads through the real durable adapters; transports are test doubles."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from app.slack_format import slack_text
from pydantic import SecretStr
from sqlalchemy import func, select
from test_product_slack import bind
from test_product_slack import bridge as canonical_bridge
from test_product_slack import slack as canonical_slack
from test_slack_progress import queued, transport

from moyai.api import create_app
from moyai.models import Delivery, Turn
from moyai.slack import deliver_one, stage_deliveries
from moyai.slack_messages import navigation_action, pr_cards, session_url, thread_message
from moyai.slack_progress import stage_progress

PR = "https://github.com/BerriAI/moyai/pull/495"
URL = "https://moyai.example/#session"
bridge = canonical_bridge
slack = canonical_slack


def buttons(message):
    blocks = message.get("blocks", []) + [
        block for attachment in message.get("attachments", []) for block in attachment["blocks"]
    ]
    return [element for block in blocks if block["type"] == "actions" for element in block["elements"]]


def test_pr_cards_use_answer_labels_without_claiming_publication():
    cards = pr_cards(f"[Keep native commentary]({PR})\nAgain: {PR}", URL)
    assert len(cards) == 1 and cards[0]["color"] == "#5B3FD1"
    assert cards[0]["blocks"][1]["text"] == {
        "type": "plain_text", "text": "PR #495 · Keep native commentary",
    }
    actions = buttons({"attachments": cards})
    assert [item["text"]["text"] for item in actions] == ["View PR", "Open Moyai session"]
    assert actions[0]["style"] == "primary" and actions[0]["url"] == PR.lower()
    assert "style" not in actions[1] and actions[1]["url"] == URL


@pytest.mark.parametrize("answer", [
    f"`{PR}`", f"```example\n{PR}\n```", f"```{PR}", PR + "oops",
    PR.replace("github.com", "github.com.evil"), "prefix" + PR,
    "https://example.com/" + PR, "https://github.com/../moyai/pull/495",
])
def test_examples_and_invalid_pr_urls_do_not_create_cards(answer):
    assert pr_cards(answer, URL) == []


def test_card_limit_and_plain_links():
    cards = pr_cards("\n".join(f"https://github.com/owner/repo/pull/{n}" for n in range(1, 9)), "")
    assert len(cards) == 5
    assert cards[0]["blocks"][1]["text"]["text"] == "PR #1"
    assert [item["action_id"] for item in buttons({"attachments": cards})] == ["moyai_view_pr"] * 5


def test_session_route_and_long_fenced_answer():
    assert session_url("https://moyai.example/", "native", "a" * 32).endswith("/#run=" + "a" * 32)
    assert session_url("https://moyai.example", "native", "invalid").endswith("/#native")
    assert session_url("", "native") == ""
    value = thread_message("```python\n" + "x" * 3400 + "\n```", URL)
    assert all(len(block["text"]["text"]) <= 3000 for block in value["blocks"] if block["type"] == "section")
    assert buttons(value)[0]["text"]["text"] == "Open session"
    assert value["text"].endswith(URL)
    assert value["attachments"] == []
    assert "style" not in buttons(value)[0]


@pytest.mark.parametrize("reference", [
    "Reference PR (not created or reviewed here): [{title}]({url}).",
    "Reference PR—not created or reviewed: [{title}]({url}).",
    "Reference PR: [{title}]({url})",
    "Reference: [{title}]({url})",
    "**PR:** [{title}]({url})",
    "- [{title}]({url})",
    "{url}",
])
def test_card_replaces_standalone_reference_line(reference):
    answer = "Both checks passed.\n\n" + reference.format(title="Keep native commentary", url=PR)
    message = thread_message(slack_text(answer), URL, cards=pr_cards(answer, URL))
    assert message["blocks"] == [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "Both checks passed.", "verbatim": True},
    }]
    assert PR.casefold() in message["text"].casefold()
    assert buttons(message)[0]["text"]["text"] == "View PR"


def test_reference_only_answer_is_a_card_without_empty_body():
    answer = f"[Changes]({PR})"
    message = thread_message(slack_text(answer), URL, cards=pr_cards(answer, URL))
    assert message["blocks"] == [] and len(message["attachments"]) == 1
    assert PR in message["text"]


def test_reference_cleanup_preserves_explanation_code_and_unrepresented_links():
    other = "https://github.com/BerriAI/moyai/pull/496"
    answer = (
        f"See [these changes]({PR}) for why cancellation is safe.\n\n"
        f"PR: [{PR}]({PR}) — do not merge until review.\n\n"
        f"```\nReference PR: {PR}\n```\n\n"
        f"Reference PR: {other}"
    )
    message = thread_message(slack_text(answer), URL, cards=pr_cards(PR, URL))
    assert message["blocks"][0]["text"]["text"] == slack_text(answer)


@pytest.mark.parametrize("product,status", [(False, "saving"), (False, "completed"), (True, "completed")])
async def test_final_cards_replace_progress_in_the_same_reply(runtime, active, slack, product, status):
    thread = await bind(slack, active)
    runtime.settings.public_url = slack.service.settings.public_url = "https://moyai.example"
    initial = await queued(runtime, active, product=product, kind="anchor", text="Working.")
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    requests, messages, handler = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, active[1].id)
            turn.status = status
            turn.result = runtime.security.seal(
                f"Both checks passed.\n\nReference PR (not created or reviewed here): [Keep native commentary]({PR}).",
            )
        await (slack.service.stage() if product else stage_deliveries(runtime))
        identity = f"product-slack:{turn.id}:completed:0" if product else f"slack-answer:{turn.id}"
        async with runtime.database.sessions() as db:
            final = await db.scalar(select(Delivery).where(Delivery.identity == identity))
        await deliver(client, final)
    assert [path for path, _ in requests] == ["/chat.postMessage", "/chat.update"]
    assert len(messages) == 1 and requests[1][1]["ts"] == next(iter(messages))
    message = requests[-1][1]
    assert message["attachments"][0]["color"] == "#5B3FD1"
    assert buttons(message)[0]["text"]["text"] == "View PR"
    assert buttons(message)[-1]["url"] == f"https://moyai.example/#{thread.session_id}"
    assert "Open conversation:" not in message["text"]
    assert message["blocks"][0]["text"]["text"] == "Both checks passed."
    assert not any("Open session:" in block.get("text", {}).get("text", "") for block in message["blocks"])
    if product:
        assert "thread_ts" not in message
        assert message["metadata"]["event_payload"]["id"] == final.id
        assert message["blocks"][0]["expand"] is True
        for _path, payload in requests:
            assert payload["parse"] == "none" and payload["link_names"] is False
            assert payload["unfurl_links"] is False and payload["unfurl_media"] is False


@pytest.mark.parametrize("product", [False, True])
async def test_progress_keeps_session_button_and_final_clears_old_attachments(runtime, active, slack, monkeypatch, product):
    await bind(slack, active)
    runtime.settings.public_url = slack.service.settings.public_url = "https://moyai.example"
    await queued(runtime, active, product=product, kind="anchor", text="Working.")
    if product:
        from moyai.product_slack_models import ProductSlackBinding
        async with runtime.database.sessions.begin() as db:
            (await db.get(ProductSlackBinding, active[0].id)).source_run_id = "a" * 32

    async def progress(*_):
        return {"messages": [{"id": "progress-one", "text": "Reading the changes."}], "subagents": []}

    monkeypatch.setattr(runtime.work_progress, "read", progress)
    await stage_progress(runtime, product=product)
    async with runtime.database.sessions() as db:
        row = await db.scalar(select(Delivery).where(Delivery.destination.contains('"progress": true')))
    payload = json.loads(runtime.security.unseal(row.content))
    expected = "#run=" + "a" * 32 if product else "#" + active[0].id
    assert buttons(payload)[0]["url"].endswith(expected)
    assert payload["attachments"] == []
    assert all("style" not in action for action in buttons(payload))
    # A plain legacy row must clear rich content when it edits the same timestamp.
    final = await queued(runtime, active, product=product, kind="final", text="Done.")
    assert "rich_reply" not in json.loads(final.destination)
    deliver = slack.service.deliver if product else lambda client, item: deliver_one(runtime, client, item)
    requests, _messages, handler = transport()
    async with runtime.database.sessions() as db:
        anchor = await db.scalar(select(Delivery).where(
            Delivery.identity == (f"product-slack:{active[1].id}:accepted" if product
                                  else f"slack-progress:{active[1].id}"),
        ))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        await deliver(client, anchor)
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = "completed"
        await deliver(client, final)
    assert requests[-1][0] == "/chat.update"
    assert requests[-1][1]["attachments"] == [] and requests[-1][1]["blocks"] == []


async def test_product_long_answer_has_one_card_and_preserves_approval_controls(runtime, active, slack):
    await bind(slack, active)
    slack.service.settings.public_url = "https://moyai.example"
    async with runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, active[1].id)
        turn.status = "completed"
        turn.result = runtime.security.seal("First paragraph.\n" * 400 + f"\n[Changes]({PR})")
    await slack.service.stage()
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).where(
            Delivery.identity.startswith(f"product-slack:{turn.id}:completed:"),
        ).order_by(Delivery.identity)))
    contents = [json.loads(runtime.security.unseal(row.content)) for row in rows]
    assert len(contents) == 1
    assert sum(len(item.get("attachments", [])) for item in contents) == 1
    assert contents[0]["attachments"]
    assert PR.casefold() in contents[0]["text"].casefold()  # Accessible fallback still has the destination.
    control = {"type": "button", "action_id": "moyai_native_approve", "value": "turn", "style": "primary",
               "text": {"type": "plain_text", "text": "Approve"}}
    message = thread_message("Approval required.", URL, controls=[control])
    assert buttons(message) == [control, buttons(message)[1]]
    assert buttons(message)[1]["action_id"] == "moyai_open_session"
    assert "style" not in buttons(message)[1]
    assert message["blocks"][-1]["elements"] == buttons(message)
    assert message["attachments"] == []


async def test_signed_navigation_ack_does_not_create_a_turn(runtime):
    runtime.settings.slack_signing_secret = SecretStr("local-signature")
    runtime.settings.slack_team_id = "TTEST"
    payload = {"type": "block_actions", "team": {"id": "TTEST"}, "actions": [
        {"type": "button", "action_id": "moyai_open_session"},
    ]}
    async def send(client, value, signed=True):
        body = urlencode({"payload": json.dumps(value)}).encode()
        stamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"local-signature", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        return await client.post("/hooks/slack/interactions", content=body, headers={
            "x-slack-request-timestamp": stamp, "x-slack-signature": signature if signed else "invalid",
        })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)), base_url="http://test",
    ) as client:
        assert (await send(client, payload)).status_code == 200
        assert (await send(client, payload, signed=False)).status_code == 401
        assert (await send(client, {**payload, "team": {"id": "TOTHER"}})).status_code == 403
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
    assert not navigation_action({**payload, "actions": [{"type": "button", "action_id": []}]})


async def test_product_navigation_does_not_enter_approval_or_agent_path(slack):
    await slack.service.interact({
        "type": "block_actions", "team": {"id": "TTEST0001"}, "user": {"id": "UTEST0001"},
        "actions": [{"type": "button", "action_id": "moyai_view_pr"}],
    })
