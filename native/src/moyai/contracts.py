"""Serializable, server-authored inputs to native agent execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .product_identity import ProductGrant


class GuidanceChanged(PermissionError):
    """A newer authenticated input fenced an action before it executed."""


@dataclass
class Grant:
    id: str
    kind: str
    revision: int
    credential: str  # Encrypted; opened only on the trusted worker.
    read_only: bool = True
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContext:
    """Policy context, independent of the conversation's display/ACL context."""

    visibility: str = "private"
    surface: str = "web"


@dataclass
class RunScope:
    session_id: str
    turn_id: str
    actor_id: str
    model: str
    grants: list[Grant] = field(default_factory=list)
    visibility: str = "private"
    surface: str = "web"
    product_grant: ProductGrant | None = None
    swarm_id: str | None = None
    swarm_name: str | None = None
    delegation_depth: int = 0
    delegation_generated: bool = False
    # Chosen by the workflow and carried into tool activities. Old payloads
    # must retain SDK delegation and its shared parent computer.
    interactive_delegation: bool = False
    execution_context: ExecutionContext | None = None

    @property
    def authority(self) -> ExecutionContext:
        return self.execution_context or ExecutionContext(self.visibility, self.surface)

    @property
    def grants_key(self) -> str:
        return hashlib.sha256(json.dumps(
            sorted((grant.id, grant.revision) for grant in self.grants)
        ).encode()).hexdigest()

    @property
    def conversation_id(self) -> str:
        # Native history search must have the same privacy boundary as native
        # history reuse. A shared product session is not a shared working context.
        return hashlib.sha256(json.dumps([
            self.session_id, self.actor_id, self.model, self.grants_key,
        ]).encode()).hexdigest()

    @property
    def memory_namespace(self) -> str:
        # Harness memory paths require bounded ASCII segments. Preserve existing
        # native UUID namespaces; canonical product IDs can contain ":"/Unicode.
        actor_segment = self.actor_id if re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", self.actor_id,
        ) else "identity-" + hashlib.sha256(self.actor_id.encode()).hexdigest()
        if self.authority.surface == "slack":
            return f"slack/{self.session_id}/{actor_segment}/{self.grants_key}"
        return f"{self.authority.visibility}/{actor_segment}/{self.grants_key}"

    @property
    def workspace_key(self) -> str:
        return hashlib.sha256(json.dumps([
            self.visibility, self.actor_id if self.visibility == "private" else "shared",
            self.grants_key,
        ]).encode()).hexdigest()


@dataclass
class InputImage:
    digest: str
    media_type: str
    name: str
    data: str = ""  # Hydrated by the activity; encrypted/offloaded in Temporal.
    # Keep the serialized type/field names compatible with recorded workflows.
    # Non-image uploads carry a reference for the shared attachment_copy tool.
    upload_id: str = ""
    size: int = 0


@dataclass
class PreparedTurn:
    scope: RunScope
    prompt: str
    conversation: str | None = None
    workspace: dict[str, Any] | None = None
    deferred_results: dict[str, Any] | None = None
    images: list[InputImage] = field(default_factory=list)
    # Result settlement intent, retained if a checkpoint activity is interrupted.
    checkpoint_workspace: bool = False
    checkpoint_waiting: bool = False


@dataclass
class SteeringInput:
    turn_id: str
    prompt: str
    mode: Literal["asap", "when_idle"] = "asap"
    images: list[InputImage] = field(default_factory=list)


@dataclass
class CompletedTurn:
    scope: RunScope
    conversation: str
    output: str
    workspace: dict[str, Any] | None
    usage: dict[str, Any]
    deferred: bool = False
    checkpoint_workspace: bool = False


class NewSession(BaseModel):
    title: str = Field(default="New conversation", max_length=300)
    visibility: Literal["private", "shared"] = "shared"
    model: str | None = None


class NewMessage(BaseModel):
    prompt: str = Field(default="", max_length=500_000)
    request_key: str = Field(min_length=1, max_length=200)
    model: str | None = None
    mode: Literal["asap", "when_idle"] = "when_idle"
    connections: list[str] = Field(default_factory=list, max_length=30)
    attachments: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def nonblank_prompt(self) -> NewMessage:
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("Enter a message or attach a file.")
        if len(set(self.attachments)) != len(self.attachments):
            raise ValueError("A file can only be attached once per message.")
        return self


class SessionEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    archived: bool | None = None
    pinned: bool | None = None
    folder: str | None = Field(default=None, max_length=200)
    unread: bool | None = None

    @model_validator(mode="after")
    def trim_title(self) -> SessionEdit:
        if self.title is not None:
            self.title = self.title.strip()
            if not self.title:
                raise ValueError("Enter a session name.")
        return self


class ApprovalReply(BaseModel):
    turn_id: str
    request_key: str = Field(min_length=1, max_length=200)
    approvals: dict[str, bool] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)


class QueuedEdit(BaseModel):
    prompt: str = Field(min_length=1, max_length=500_000)


class EffectResolution(BaseModel):
    outcome: Literal["completed", "not_executed"]
    evidence: str = Field(min_length=10, max_length=20_000)

