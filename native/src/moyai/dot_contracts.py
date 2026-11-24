"""Typed background judgment, separate from ordinary conversational execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DotView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    session_id: str | None
    attention_available: bool


class DotEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=1000, description="Exact source URL or item ID returned by a connected tool.")
    source_version: str = Field(
        min_length=1, max_length=200,
        description="Exact provider revision or last-modified timestamp for this item. Put the primary item first.",
    )
    quote: str = Field(min_length=1, max_length=1000, description="Exact relevant quotation from that tool's result.")


class DotDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["quiet", "notify"]
    message: str = Field(max_length=4000, description="A useful message to the owner, or empty when quiet.")
    evidence: list[DotEvidence] = Field(max_length=5)

    @model_validator(mode="after")
    def grounded_notification(self):
        if self.action == "notify" and (not self.message.strip() or not self.evidence):
            raise ValueError("A notification needs a useful message and connected-source evidence.")
        if self.action == "quiet" and (self.message or self.evidence):
            raise ValueError("A quiet check has no notification or evidence.")
        if any(not item.source.strip() or not item.source_version.strip() or not item.quote.strip() for item in self.evidence):
            raise ValueError("Use nonblank source evidence.")
        return self
