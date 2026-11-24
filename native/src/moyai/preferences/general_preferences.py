"""Composer defaults and encrypted, owner-bound personal instructions."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..security import Security

INSTRUCTION_LIMIT = 4000


class GeneralPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    delivery_mode: Literal["when_idle", "asap"] = "when_idle"
    connection_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None, max_length=30,
    )
    personal_instructions: str = Field(default="", max_length=INSTRUCTION_LIMIT)
    personal_instructions_revision: int = Field(default=0, ge=0)


def preference_document(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def instruction_record(raw: str) -> dict:
    value = preference_document(raw).get("private_instructions", {})
    return value if isinstance(value, dict) else {}


def instruction_revision(raw: str) -> int:
    value = instruction_record(raw).get("revision", 0)
    return value if type(value) is int and value >= 0 else 0


def read_instructions(raw: str, security: Security, owner_id: str) -> str:
    """Never accept plaintext or ciphertext copied from another account."""
    sealed = instruction_record(raw).get("sealed")
    if not isinstance(sealed, str) or not sealed:
        return ""
    try:
        value = json.loads(security.unseal(sealed))
    except (InvalidToken, ValueError, TypeError):
        return ""
    if not isinstance(value, dict) or value.get("owner_id") != owner_id:
        return ""
    text = value.get("text")
    return text if isinstance(text, str) and len(text) <= INSTRUCTION_LIMIT else ""


def general_preferences(
    raw: str, *, security: Security | None = None, owner_id: str | None = None,
) -> dict:
    stored = preference_document(raw).get("general", {})
    if not isinstance(stored, dict):
        stored = {}
    # Ignore unknown stored keys so newer servers can add preferences safely.
    stored = {key: value for key, value in stored.items() if key in {"delivery_mode", "connection_ids"}}
    try:
        result = GeneralPreferences.model_validate(stored).model_dump()
    except ValidationError:
        result = GeneralPreferences().model_dump()
    if security is not None and owner_id is not None:
        result["personal_instructions"] = read_instructions(raw, security, owner_id)
        result["personal_instructions_revision"] = instruction_revision(raw)
    return result
