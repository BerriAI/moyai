"""Personal MCP and Skills Hub capabilities share one credential."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .mcp_remote import RemoteMCPConfig


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    url: str = Field(max_length=2048)
    skills: bool = True
    mcp: bool = True

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        # Accept the URL users already use for inference, including a path prefix.
        return RemoteMCPConfig(url=value).url.rstrip("/").removesuffix("/v1")

    def endpoint_config(self, path: str = "") -> RemoteMCPConfig:
        return RemoteMCPConfig(url=self.url + path)


class GatewayCapability(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    status: Literal["ready", "disabled", "unauthorized", "unavailable", "error"]
    count: int = 0
    message: str = ""


class GatewaySkill(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(default="", max_length=1024)
    version: str | None = None
    updated_at: str | None = None


class GatewayDiscovery(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    skills: GatewayCapability
    mcp: GatewayCapability
    skill_catalog: list[GatewaySkill] = Field(default_factory=list, max_length=1000)


class GatewaySkillPage(BaseModel):
    skills: list[GatewaySkill] = Field(max_length=100)
    next_offset: int | None = Field(default=None, ge=0, le=10000)


class GatewayDefaults(BaseModel):
    url: str
