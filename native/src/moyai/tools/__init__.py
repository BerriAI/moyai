from .base import WorkspaceTool
from .open_computer_use import OpenComputerUse

__all__ = ["WORKSPACE_TOOLS", "OpenComputerUse", "WorkspaceTool"]

WORKSPACE_TOOLS: tuple[WorkspaceTool, ...] = (OpenComputerUse(),)
