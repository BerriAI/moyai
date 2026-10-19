"""User-level composer defaults stored alongside other user preferences."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class GeneralPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    delivery_mode: Literal["when_idle", "asap"] = "when_idle"
    connection_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None, max_length=30,
    )


def preference_document(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def general_preferences(raw: str) -> dict:
    stored = preference_document(raw).get("general", {})
    if not isinstance(stored, dict):
        stored = {}
    # Ignore unknown stored keys so newer servers can add preferences safely.
    stored = {key: value for key, value in stored.items() if key in GeneralPreferences.model_fields}
    try:
        return GeneralPreferences.model_validate(stored).model_dump()
    except ValidationError:
        return GeneralPreferences().model_dump()
