"""Typed background judgment, separate from ordinary conversational execution."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DotView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    session_id: str | None
    attention_available: bool


class DotEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1, max_length=36, description="Exact selected account ID supplied by the host.")
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
        if any(not item.connection_id.strip() or not item.source.strip() or not item.source_version.strip() or not item.quote.strip() for item in self.evidence):
            raise ValueError("Use nonblank source evidence.")
        return self


class DotObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1200)
    evidence: list[DotEvidence] = Field(min_length=1, max_length=3)


class DotResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: Literal["complete", "partial", "blocked"]
    observations: list[DotObservation] = Field(max_length=12)


class DotItemChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: int = Field(ge=0, le=11)
    state: Literal["open", "resolved"]


class DotJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DotItemChange] = Field(max_length=12)
    notify_observation: int | None = Field(ge=0, le=11)
    message: str = Field(max_length=4000)

    @model_validator(mode="after")
    def grounded(self):
        if len({item.observation for item in self.items}) != len(self.items):
            raise ValueError("Choose each observation once.")
        if self.notify_observation is None:
            if self.message:
                raise ValueError("Leave the message empty when staying quiet.")
        elif not self.message.strip() or not any(
            item.observation == self.notify_observation and item.state == "open" for item in self.items
        ):
            raise ValueError("Notify about one open observation with a useful message.")
        return self


class DotItemView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str
    revision: int
    connection_id: str
    title: str
    summary: str
    source: str
    state: Literal["open", "resolved", "dismissed", "snoozed"]
    snoozed_until: str | None
    last_seen_at: str


class DotItemsView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    items: list[DotItemView]
    limited: bool


class DotItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    state: Literal["open", "resolved", "dismissed", "snoozed"]
    snoozed_until: datetime | None = None

    @model_validator(mode="after")
    def snooze(self):
        if self.state == "snoozed":
            if self.snoozed_until is None or self.snoozed_until.utcoffset() is None:
                raise ValueError("Choose a snooze time with its timezone.")
        elif self.snoozed_until is not None:
            raise ValueError("Only a snoozed item has a snooze time.")
        return self
