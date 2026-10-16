"""Validated public requests and the native agent's trusted swarm context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .contracts import NewMessage


class NewSwarm(NewMessage):
    workers: int = Field(default=20, ge=2, strict=True)
    visibility: Literal["private", "shared"] = "private"
    worker_models: list[str] = Field(default_factory=list, max_length=20)
    budget_seconds: int = Field(default=0, ge=0, le=2_592_000, strict=True)

    @model_validator(mode="after")
    def valid_mission(self) -> NewSwarm:
        if not self.prompt.strip():
            raise ValueError("Describe the swarm's shared goal.")
        if self.budget_seconds and self.budget_seconds < 60:
            raise ValueError("A finite work window must be at least one minute.")
        if len(set(self.worker_models)) != len(self.worker_models):
            raise ValueError("Select each worker model once.")
        if self.mode != "when_idle":
            raise ValueError("A new swarm cannot steer an existing turn.")
        return self


class SwarmControl(BaseModel):
    action: Literal["pause", "resume", "stop", "time"]
    revision: int = Field(ge=0, strict=True)
    budget_seconds: int | None = Field(default=None, ge=0, le=2_592_000, strict=True)

    @model_validator(mode="after")
    def valid_window(self) -> SwarmControl:
        if self.action == "time" and self.budget_seconds is None:
            raise ValueError("Choose the new work window.")
        if self.budget_seconds is not None and 0 < self.budget_seconds < 60:
            raise ValueError("A finite work window must be at least one minute.")
        if self.action != "time" and self.budget_seconds is not None:
            raise ValueError("Only a time change accepts a new work window.")
        return self


@dataclass
class MissionTick:
    exists: bool = True
    delay_seconds: float = 5


@dataclass
class MissionRun:
    mission_id: str
    generation: int
