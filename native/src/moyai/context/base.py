from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..contracts import RunScope


class ContextSource(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def render(self, scope: RunScope) -> str:
        ...
