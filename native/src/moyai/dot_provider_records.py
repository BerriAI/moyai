"""Project native provider records into bounded, same-record evidence fields."""

from __future__ import annotations

import hashlib
import re


def rich_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    # Provider rich-text fragments belong to this property/block, not its children.
    return "".join(
        item["plain_text"] for item in value
        if isinstance(item, dict) and isinstance(item.get("plain_text"), str)
    )


def notion_record(record: dict) -> dict:
    if record.get("object") not in {"page", "block", "database", "data_source"}:
        return record
    result = dict(record)
    if isinstance(record.get("last_edited_time"), str):
        result["revision"] = record["last_edited_time"]
    if isinstance(record.get("title"), list):
        result["title"] = rich_text(record["title"])
    properties = record.get("properties")
    if record.get("object") == "page" and isinstance(properties, dict):
        for name, prop in properties.items():
            if not isinstance(prop, dict) or not isinstance(name, str):
                continue
            kind = prop.get("type")
            value = prop.get(kind) if isinstance(kind, str) else None
            key = f"property:{name}"
            if kind in {"title", "rich_text"}:
                result[key] = rich_text(value)
            elif kind in {"status", "select"} and isinstance(value, dict):
                result[key] = value.get("name")
            elif kind in {"checkbox", "number", "url", "email", "phone_number"}:
                result[key] = value
            elif kind == "date" and isinstance(value, dict):
                for field in ("start", "end"):
                    if isinstance(value.get(field), str):
                        result[f"{key}:{field}"] = value[field]
    kind = record.get("type")
    body = record.get(kind) if isinstance(kind, str) else None
    if record.get("object") == "block" and isinstance(body, dict):
        result["text"] = rich_text(body.get("rich_text"))
        if isinstance(body.get("checked"), bool):
            result["checked"] = body["checked"]
    return result


def slack_record(record: dict, tool: str, arguments: dict) -> dict:
    if tool not in {"search", "channel", "thread"}:
        return record
    stamp = record.get("ts")
    channel = record.get("channel")
    if isinstance(channel, dict):
        channel = channel.get("id")
    if channel is None and tool in {"channel", "thread"}:
        channel = arguments.get("channel")
    if not (
        isinstance(stamp, str) and re.fullmatch(r"[0-9]+\.[0-9]{6}", stamp)
        and isinstance(channel, str) and re.fullmatch(r"[CDG][A-Z0-9]+", channel)
        and isinstance(record.get("text"), str)
    ):
        return record
    # Search and history omit different optional metadata for the same message.
    # Fingerprint the text actually exposed as evidence, not that representation.
    # Each reply has its own ts and identity; reply-count changes do not create
    # a new revision of the unchanged root message.
    revision = hashlib.sha256(record["text"].encode()).hexdigest()
    result = dict(record)
    # Search supplies permalinks while history/replies need not. Use the same
    # account-scoped channel + message identity in every native read shape.
    for key in ("html_url", "web_url", "permalink", "url", "uid", "uuid"):
        if key in result:
            result[f"provider_{key}"] = result.pop(key)
    result["id"] = f"slack:{channel}:{stamp}"
    result["revision"] = f"observed:{revision}"
    return result


def native_record(provider: str | None, tool: str, arguments: dict, record: dict) -> dict:
    if provider == "notion":
        return notion_record(record)
    if provider == "slack":
        return slack_record(record, tool, arguments)
    return record
