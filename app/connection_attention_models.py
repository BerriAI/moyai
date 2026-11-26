"""Public attention state shared by canonical and native connection routes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

AttentionStatus = Literal["disabled", "unavailable", "paused", "pending", "active"]
AttentionReason = Literal[
    "automation_unavailable", "shared_account", "disconnected", "unsupported_provider",
    "read_tools_required", "provider_disabled",
]


class ConnectionAttentionView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    eligible: bool
    reason: AttentionReason | None
    use_proactively: bool
    status: AttentionStatus
    readable_tools: list[str]
    consent_revision: int | None = None
