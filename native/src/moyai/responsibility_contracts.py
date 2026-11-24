"""Typed opt-in, controls and evidence for the private responsibility inbox."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class ResponsibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewResponsibility(ResponsibilityInput):
    request_key: str = Field(min_length=1, max_length=100)
    source_session_id: str = Field(min_length=1, max_length=36)
    objective: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    interval_seconds: int = Field(default=30, ge=10, le=3600)
    max_evaluations: int = Field(default=10, ge=1, le=100)


class ResponsibilityControl(ResponsibilityInput):
    generation: int = Field(ge=1)
    action: Literal["pause", "resume"]


class ResponsibilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class ResponsibilityEvidence(ResponsibilityOutput):
    message_id: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1000)

    @field_validator("quote")
    @classmethod
    def usable_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Quote nonblank source evidence.")
        return value


class ResponsibilityDecision(ResponsibilityOutput):
    action: Literal["wait", "notify"]
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[ResponsibilityEvidence] = Field(max_length=5)

    @model_validator(mode="after")
    def cited_finding(self):
        if self.action == "notify" and not self.evidence:
            raise ValueError("A finding requires a quotation from the supplied evidence.")
        return self


class ResponsibilityView(ResponsibilityOutput):
    id: str
    source_session_id: str
    objective: str
    model: str
    status: Literal["waiting", "running", "paused", "blocked", "budget_limited"]
    generation: int
    watermark: int
    interval_seconds: int
    max_evaluations: int
    evaluations: int
    active_turn_id: str | None
    reason: str
    next_check_at: str
    created_at: str


class ResponsibilityInboxItem(ResponsibilityOutput):
    turn_id: str
    responsibility_id: str
    source_session_id: str
    worker_session_id: str
    objective: str
    summary: str
    evidence: list[ResponsibilityEvidence]
    created_at: str
    read_at: str | None


class ResponsibilityInbox(ResponsibilityOutput):
    items: list[ResponsibilityInboxItem]


class ResponsibilityList(ResponsibilityOutput):
    items: list[ResponsibilityView]


class ResponsibilityReview(ResponsibilityOutput):
    turn_id: str
    generation: int
    through_sequence: int
    status: Literal["queued", "starting", "running", "waiting", "saving", "completed", "failed", "stopped"]
    reconciled: bool
    accepted: bool
    created_at: str
    finished_at: str | None
    error: str | None
    usage: dict[str, JsonValue]
    validation: Literal["pending", "valid", "invalid", "not_accepted"]
    decision: ResponsibilityDecision | None


class ResponsibilityHistory(ResponsibilityOutput):
    items: list[ResponsibilityReview]
