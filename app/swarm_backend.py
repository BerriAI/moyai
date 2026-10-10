"""Moyai adapter for the agent-owned swarm interface.

The trusted host binds an owner once. Every operation goes through the same
coordinator as broker tools, including active-turn, ownership, deadline and
admission checks. Persistence and checkpoint scheduling remain host concerns.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agents import AgentCoordinator


@dataclass(frozen=True)
class MoyaiSwarmBackend:
    coordinator: 'AgentCoordinator'
    owner_id: str

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self.coordinator.call(self.owner_id, name, dict(arguments))
