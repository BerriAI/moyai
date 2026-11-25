"""Bind attention decisions to one structured provider record and account."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime

from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import ToolReturnPart

from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .dot_contracts import DotDecision, DotEvidence

# Prefer a provider's canonical URL when both URL and local ID are returned.
# An unsupported record shape fails closed instead of guessing its identity.
IDENTITY_FIELDS = ("html_url", "web_url", "permalink", "url", "id", "uid", "uuid")
VERSION_FIELDS = ("updated_at", "updatedAt", "updated", "last_modified", "lastModified", "version", "revision", "etag")
COLLECTION_FIELDS = ("data", "items", "result", "results", "records", "events", "messages", "issues", "pull_requests", "nodes", "edges", "node", "comments", "issue", "pull_request")


def structured_records(value: object) -> Iterator[dict | None]:
    """Traverse collections; None marks an unsupported row, never silent loss."""
    if isinstance(value, dict):
        yield value
        for key in COLLECTION_FIELDS:
            if key in value:
                yield from structured_records(value[key])
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (list, tuple)):
                # SDK-decoded MCP text blocks can contain nested collections.
                # Keep their evidence for focused checks; discovery treats the
                # ambiguous page boundary as incomplete coverage.
                yield None
            yield from structured_records(child)
    else:
        yield None


def decode_payload(value: str) -> object:
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def records(value: object) -> Iterator[dict | None]:
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


def connected_records(conversation: str, grants: list[Grant]) -> list[tuple[str, dict]]:
    allowed = {
        f"{grant.kind}_{grant.id[:8]}_{name}": grant.id
        for grant in grants
        for name in (ConnectionPolicy.model_validate(grant.policy).allowed_tools or [])
    }
    return [
        (allowed[part.tool_name], record)
        for message in ConversationTypeAdapter.validate_json(conversation).messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.outcome == "success" and part.tool_name in allowed
        for record in records(part.content)
        if record is not None
    ]


def match(item: DotEvidence, account: str, record: dict) -> tuple[str, str, str] | None:
    fields = {key: str(value) for key, value in record.items() if isinstance(value, (str, int))}
    ids = [fields[key] for key in IDENTITY_FIELDS if fields.get(key)]
    versions = [fields[key] for key in VERSION_FIELDS if fields.get(key)]
    if (
        account == item.connection_id and item.source in ids and item.source_version in versions
        and any(item.quote in value for value in fields.values())
    ):
        return account, ids[0], versions[0]
    return None


def validate_evidence(evidence: list[DotEvidence], conversation: str, grants: list[Grant]) -> list[tuple[str, str, str]]:
    sources = connected_records(conversation, grants)
    identities = []
    for item in evidence:
        matches = {identity for account, record in sources if (identity := match(item, account, record))}
        if len(matches) != 1:
            raise ValueError("Dot evidence must identify one connected account's structured provider record.")
        identities.append(matches.pop())
    return identities


def verified_closed(evidence: DotEvidence, conversation: str, grants: list[Grant]) -> bool:
    """Only provider fields with verified semantics may close a tracked item."""
    from .dot_sources import github_item, github_reader

    grant = next((grant for grant in grants if grant.id == evidence.connection_id), None)
    if grant is None or not github_reader(grant):
        return False
    sources = connected_records(conversation, grants)
    matched = [
        record for account, record in sources
        if (identity := match(evidence, account, record)) and github_item(identity[1])
    ]
    if not matched:
        return False
    aliases = {str(record[key]) for record in matched for key in IDENTITY_FIELDS if record.get(key)}
    related = [
        record for account, record in sources if account == evidence.connection_id
        and any(str(record.get(key)) in aliases for key in IDENTITY_FIELDS if record.get(key))
    ]
    # Inspect every version, not only the version selected by the model.
    # Contradictory or unknown state remains owner-controlled.
    if not all(
        str(record.get("state", "")).casefold() == "closed" or record.get("merged") is True
        for record in related
    ):
        return False
    versions = {
        str(record[key]) for record in related
        for key in VERSION_FIELDS if record.get(key)
    }
    if versions == {evidence.source_version}:
        return True

    def timestamp(record: dict) -> datetime | None:
        for key in VERSION_FIELDS:
            value = record.get(key)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError:
                    continue
                if parsed.tzinfo is not None:
                    return parsed
        return None

    times = [timestamp(record) for record in related]
    if any(value is None for value in times):
        return False  # Opaque versions cannot be ordered safely.
    latest = max(value for value in times if value is not None)
    return all(timestamp(record) == latest for record in matched)


def validate_decision(output: str, conversation: str, grants: list[Grant]) -> tuple[DotDecision, str | None]:
    decision = DotDecision.model_validate_json(output)
    identities = validate_evidence(decision.evidence, conversation, grants)
    fingerprint = hashlib.sha256(json.dumps(identities[0], ensure_ascii=False).encode()).hexdigest() if identities else None
    return decision, fingerprint
