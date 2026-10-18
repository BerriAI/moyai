from __future__ import annotations

from abc import ABC, abstractmethod

import modal


class WorkspaceTool(ABC):
    """A tool required by the agent's workspace image."""

    @abstractmethod
    def install(self, image: modal.Image) -> modal.Image:
        ...
