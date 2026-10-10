"""Typed agent definitions for the public swarm interface.

Harness values identify built-in runtimes; the host still decides which runtime
and model are available. These definitions grant no permissions or time budget.
"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Assignment


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
    """A named worker and its assigned task, submitted by an owning agent.

    Names and tasks are free-form text. Select a built-in harness explicitly;
    omitting the model asks the host to inherit the owner's configured model.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=3, max_length=12000)
    harness: Harness
    model: str | None = Field(default=None, min_length=1, max_length=120)

    def to_assignment(self) -> Assignment:
        """Translate to the existing host contract without changing its wire shape."""
        return Assignment(label=self.name, prompt=self.task,
                          harness=self.harness.value, model=self.model)
