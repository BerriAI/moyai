"""Connection policy shared by discovery, approval and dispatch."""

from typing import Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.tools import ToolDefinition

PROHIBITED_TOOLS = frozenset({
    "merge_pull_request", "delete_repository", "delete_file",
    "pull_request_review_write", "create_and_submit_pull_request_review",
})
ApprovalRequirement = Literal["always", "never", "connection"]


def read_only_tool(tool: ToolDefinition) -> bool:
    annotations = (tool.metadata or {}).get("annotations")
    if not isinstance(annotations, dict):
        return False
    return annotations.get("readOnlyHint") is True


def approval_requirement(tool: ToolDefinition) -> ApprovalRequirement:
    if tool.kind == "unapproved":
        return "always"
    if read_only_tool(tool):
        return "never"
    return "connection"


class ConnectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[str] | None = Field(default=None, max_length=200)
    require_approval: bool = True
    product: dict[str, object] | None = None

    def allows(self, operation: str) -> bool:
        if operation in PROHIBITED_TOOLS:
            return False
        if self.allowed_tools is None:
            return True
        return operation in self.allowed_tools

    def needs_approval(self, requirement: ApprovalRequirement) -> bool:
        match requirement:
            case "always":
                return True
            case "never":
                return False
            case "connection":
                return self.require_approval
            case _:
                assert_never(requirement)
