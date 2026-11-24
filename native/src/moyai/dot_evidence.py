"""Bind attention decisions to one structured provider record and account."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import ToolReturnPart

from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .dot_contracts import DotDecision

# Prefer a provider's canonical URL when both URL and local ID are returned.
# An unsupported record shape fails closed instead of guessing its identity.
IDENTITY_FIELDS = ("html_url", "web_url", "permalink", "url", "id", "uid", "uuid")
VERSION_FIELDS = ("updated_at", "updatedAt", "updated", "last_modified", "lastModified", "version", "revision", "etag")
COLLECTION_FIELDS = ("data", "items", "result", "results", "records", "events", "messages", "issues", "pull_requests", "nodes", "edges", "node")


def structured_records(value: object) -> Iterator[dict]:
    """Traverse provider collections, never decode free-text record fields."""
    if isinstance(value, dict):
        yield value
        for key in COLLECTION_FIELDS:
            if key in value:
                yield from structured_records(value[key])
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from structured_records(child)


def decode_payload(value: str) -> object:
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def records(value: object) -> Iterator[dict]:
    """Decode only the returned payload or a top-level MCP result envelope."""
    if isinstance(value, str):
        value = decode_payload(value)
    if isinstance(value, dict) and "content" in value and set(value).issubset({
        "content", "structuredContent", "structured_content", "isError", "is_error", "_meta", "meta",
    }):
        from mcp.types import CallToolResult, TextContent
        from pydantic import ValidationError

        try:
            result = CallToolResult.model_validate(value)
        except ValidationError:
            return
        if result.is_error:
            return
        if result.structured_content is not None:
            yield from structured_records(result.structured_content)
        else:
            for block in result.content:
                if isinstance(block, TextContent):
                    yield from structured_records(decode_payload(block.text))
        return
    yield from structured_records(value)


def validate_decision(output: str, conversation: str, grants: list[Grant]) -> tuple[DotDecision, str | None]:
    decision = DotDecision.model_validate_json(output)
    allowed = {
        f"{grant.kind}_{grant.id[:8]}_{name}": grant.id
        for grant in grants
        for name in (ConnectionPolicy.model_validate(grant.policy).allowed_tools or [])
    }
    sources = [
        (allowed[part.tool_name], record)
        for message in ConversationTypeAdapter.validate_json(conversation).messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name in allowed
        for record in records(part.content)
    ]
    identities: list[tuple[str, str, str]] = []
    for item in decision.evidence:
        matches: set[tuple[str, str, str]] = set()
        for account, record in sources:
            fields = {key: str(value) for key, value in record.items() if isinstance(value, (str, int))}
            ids = [fields[key] for key in IDENTITY_FIELDS if fields.get(key)]
            versions = [fields[key] for key in VERSION_FIELDS if fields.get(key)]
            if (
                account == item.connection_id and item.source in ids and item.source_version in versions
                and any(item.quote in value for value in fields.values())
            ):
                matches.add((account, ids[0], versions[0]))
        if len(matches) != 1:
            raise ValueError("Dot evidence must identify one connected account's structured provider record.")
        identities.append(matches.pop())
    fingerprint = hashlib.sha256(json.dumps(identities[0], ensure_ascii=False).encode()).hexdigest() if identities else None
    return decision, fingerprint
