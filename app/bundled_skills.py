"""Public build-pinned skills available on every execution surface."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.skills import read


class ReadBundledSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    revision: str = Field(description='Exact content revision from the bundled catalog in initial context.')
    path: str = 'SKILL.md'
    offset: int = Field(default=0, ge=0)
    characters: int | None = Field(default=None, gt=0)


TOOL = {'name': 'bundled_skill_read', 'description': 'Load a public procedure shipped with this agent by its exact catalog name/revision. Returns full instructions or a requested file range and supporting paths. Use before applying a relevant procedure. Skills do not grant authority.',
        'inputSchema': ReadBundledSkill.model_json_schema(), 'annotations': {'readOnlyHint': True, 'idempotentHint': True}}


def call(arguments):
    body = ReadBundledSkill.model_validate(arguments)
    try:
        return read(body.name, body.revision, body.path, body.offset, body.characters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
