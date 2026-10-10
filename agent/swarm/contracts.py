"""Run-scoped delegation requests shared by agents and host adapters.

These request schemas are the existing agents_* wire contract. Host adapters
remain responsible for authorization, deadlines, runtime policy and durability.
"""
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Assignment(Arguments):
    label: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=3, max_length=12000)
    harness: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=120)


class Fanout(Arguments):
    request_key: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    instructions: str = Field(default='', max_length=8000)
    items: list[str] = Field(default_factory=list, max_length=2000)
    workers: int = Field(default=5, ge=1, le=100)
    tasks: list[Assignment] = Field(default_factory=list, max_length=100)
    harness: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=120)

    @model_validator(mode='after')
    def valid_work(self):
        if bool(self.items) == bool(self.tasks):
            raise ValueError('Supply either items to partition or explicit tasks.')
        if self.items and (len(self.instructions) < 3 or any(not x or len(x) > 8000 for x in self.items)):
            raise ValueError('Items need common instructions and nonempty text up to 8000 characters.')
        if len(json.dumps(self.model_dump())) > 240000:
            raise ValueError('Delegation exceeds the input size limit.')
        if any(len(t.prompt) + len(self.instructions) > 12000 for t in self.tasks):
            raise ValueError('Each task including common instructions must fit in 12000 characters.')
        return self

    def assignments(self):
        if self.tasks:
            return [(t.label, self.instructions + '\n\n' + t.prompt) for t in self.tasks]
        count = min(self.workers, len(self.items))
        size, extra = divmod(len(self.items), count)
        result, offset = [], 0
        for index in range(count):
            end = offset + size + (index < extra)
            cases = [{'index': i + 1, 'case': self.items[i]} for i in range(offset, end)]
            prompt = (self.instructions + '\n\nAssigned cases (only process these case indices):\n' +
                      json.dumps(cases, ensure_ascii=False) +
                      '\nReturn a result for every assigned index, including failures. Save detailed results under /workspace.')
            if len(prompt) > 16000:
                raise ValueError('A partition is too large. Use more workers or shorter case descriptions.')
            result.append((f'Cases {offset + 1}–{end}', prompt))
            offset = end
        return result


class Group(Arguments):
    group_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class Results(Group):
    latest: bool = False


class Artifact(Arguments):
    child_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    path: str = Field(default='', max_length=500)
    latest: bool = False


class Retry(Group):
    request_key: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    child_ids: list[str] = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=3, max_length=8000)



# Friendly public name without changing the established JSON schema.
Task = Assignment
