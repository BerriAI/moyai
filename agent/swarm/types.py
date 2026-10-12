"""Typed runtime preferences for agents joining a shared mission."""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Harness(StrEnum):
    """Built-in harness IDs, stable across the Python and JSON interfaces."""

    HERMES = 'hermes'
    CLAUDE_AGENT_SDK = 'claude-agent-sdk'
    CODEX = 'codex'
    OPENCODE = 'opencode'
    DEEPAGENTS = 'deepagents'
    TOOL_LOOP = 'tool-loop'
    PI = 'pi'


class Agent(BaseModel):
    """A participant, without a predefined job or leadership role.

    An omitted name receives a unique display name when the swarm starts.
    Omitting a harness or model asks the host to inherit the owner's runtime.
    These preferences grant no permissions or additional time budget.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    harness: Harness | None = None
    model: str | None = Field(default=None, min_length=1, max_length=120)
