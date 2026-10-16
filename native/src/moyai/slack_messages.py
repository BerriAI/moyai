"""Slack thread presentation; no model calls, credentials, or external writes."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from .links import PR_MENTION
from .pull_requests import parse_pr_url

VIOLET = "#5B3FD1"
NAVIGATION_ACTIONS = {"moyai_open_session", "moyai_view_pr", "moyai_review_pr"}


def session_url(public_url: str, session_id: str, source_run_id: str = "") -> str:
    if not public_url:
        return ""
    route = "run=" + source_run_id if re.fullmatch(r"[0-9a-f]{32}", source_run_id) else session_id
    return public_url.rstrip("/") + "/#" + route


def button(label: str, url: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    return {
        "type": "button", "action_id": action, "text": {"type": "plain_text", "text": label},
        "url": url, **({"style": "primary"} if primary else {}),
    }


def navigation_action(payload: dict[str, Any]) -> bool:
    """URL clicks still need an acknowledgment, but never invoke an agent/action."""
    actions = payload.get("actions")
    return bool(
        payload.get("type") == "block_actions" and isinstance(actions, list) and len(actions) == 1
        and isinstance(actions[0], dict) and actions[0].get("type") == "button"
        and isinstance(actions[0].get("action_id"), str)
        and actions[0].get("action_id") in NAVIGATION_ACTIONS
    )


def pr_cards(answer: str, url: str) -> list[dict[str, Any]]:
    """Present PRs explicitly linked in the answer, without claiming publication.

    Markdown labels are authored answer text, not fetched GitHub metadata. Plain
    links get a factual repository/number heading. Code examples are excluded.
    """
    prose = re.sub(r"```.*?(?:```|\Z)|`[^`\n]*`", "", answer, flags=re.DOTALL)
    labels = {
        match[2]: match[1]
        for match in re.finditer(r"\[([^\]\n]{1,200})\]\((https://github\.com/[^\s<>)]{1,2048})\)", prose)
    }
    cards, seen = [], set()
    for match in PR_MENTION.finditer(prose):
        if match.start() and prose[match.start() - 1] not in " \t\r\n([<\"':":
            continue
        suffix = prose[match.end():match.end() + 1]
        if suffix and suffix not in " \t\r\n?/#)>]}.,;:!\"'|":
            continue
        try:
            pr = parse_pr_url(match[0])
        except HTTPException:
            continue
        if pr.url in seen:
            continue
        seen.add(pr.url)
        title = labels.get(match[0], "")
        # A URL used as its own Markdown label adds no useful title.
        if title.startswith(("https://", "http://")):
            title = ""
        heading = f"PR #{pr.number}" + (" · " + title if title else "")
        actions = [
            button("View PR", pr.url, "moyai_view_pr", primary=True),
            button("Review changes", pr.url + "/files", "moyai_review_pr"),
        ]
        if url:
            actions.append(button("Open session", url, "moyai_open_session"))
        cards.append({
            "color": VIOLET, "fallback": f"{pr.repository} #{pr.number}: {pr.url}",
            "blocks": [
                {"type": "context", "elements": [
                    {"type": "plain_text", "text": "🚅 LiteLLM · Moyai"},
                ]},
                {"type": "section", "text": {"type": "plain_text", "text": heading}},
                {"type": "context", "elements": [
                    {"type": "plain_text", "text": "Repo: " + pr.repository},
                ]},
                {"type": "actions", "elements": actions},
            ],
        })
        if len(cards) == 5:
            break
    return cards


def thread_message(
    text: str, url: str, *, cards: list[dict[str, Any]] | None = None,
    controls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep fallback text accessible and all section text inside Slack's limit."""
    from app.slack_format import split_reply

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": part, "verbatim": True}}
        for part in split_reply(text, limit=2900)
    ]
    actions = list(controls or [])
    if url and not cards:
        actions.append(button("Open session", url, "moyai_open_session", primary=not controls))
    if actions:
        blocks.append({"type": "actions", "elements": actions})
    return {
        "text": text + ("\n\nOpen session: " + url if url else ""),
        "blocks": blocks, "attachments": cards or [],
    }


def saved_message(content: bytes, destination: dict[str, Any]) -> dict[str, Any]:
    """Old standalone outbox rows remain plain text until they have drained."""
    import json

    if destination.get("rich_reply") is True:
        return json.loads(content)
    return {"text": content.decode(), "blocks": [], "attachments": []}
