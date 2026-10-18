"""Provider-neutral message admission after an adapter verifies its event.

Adapters retain signature, identity, audience and provider-context checks. This
contract carries their result; constructing one never grants access by itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InboundEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=20, pattern=r"^[a-z][a-z0-9_]*$")
    sender_id: str = Field(min_length=1)
    # Stable provider message identity, not the webhook envelope ID. Some
    # providers deliver one message through multiple event types.
    deduplication_key: str = Field(min_length=1, max_length=170)
    text: str = Field(max_length=500_000)
    model_context: str = ""
    attachments: tuple[str, ...] = Field(default=(), max_length=8)
    # Retained encrypted; public consumers use an explicit display whitelist.
    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def request_key(self) -> str:
        return f"{self.source}-message:{self.deduplication_key}"

    @property
    def prompt(self) -> str:
        return self.text + self.model_context

    def source_data(self) -> dict[str, Any]:
        return {**self.provenance, "kind": self.source}

    def with_model_input(self, prompt: str, *, context_status: str | None = None) -> InboundEvent:
        if not prompt.startswith(self.text):
            raise ValueError("Model context cannot replace an event's authored text.")
        provenance = self.provenance
        if context_status is not None:
            provenance = {**provenance, "context_status": context_status}
        return self.model_copy(update={"model_context": prompt[len(self.text):], "provenance": provenance})
