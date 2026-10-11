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
    prompt: str = Field(min_length=3)
    harness: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=120)


class Fanout(Arguments):
    request_key: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    instructions: str = ''
    items: list[str] = Field(default_factory=list)
    workers: int = Field(default=5, ge=1)
    tasks: list[Assignment] = Field(default_factory=list)
    wait: bool = Field(default=False, description='Checkpoint and release the parent while all children work. Otherwise return handles immediately so the parent can continue independent work.')
    harness: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=120)

    @model_validator(mode='after')
    def valid_work(self):
        if bool(self.items) == bool(self.tasks):
            raise ValueError('Supply either items to partition or explicit tasks.')
        if self.items and (len(self.instructions) < 3 or any(not x.strip() for x in self.items)):
            raise ValueError('Items need common instructions and nonempty text.')
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
            result.append((f'Cases {offset + 1}–{end}', prompt))
            offset = end
        return result


class Group(Arguments):
    group_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class Results(Group):
    latest: bool = False


class Wait(Results):
    after: str = Field(default='', description='Cursor from agents_results or an earlier wait. Returns when a worker changes, all settle, or the wait interval ends.')
    seconds: float = Field(default=60, ge=0, allow_inf_nan=False)
    release: bool = Field(default=False, description='Save and release this execution until the group settles instead of polling locally.')


class Send(Group):
    child_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    message: str = Field(min_length=1)
    request_key: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


class Cancel(Group):
    child_ids: list[str] = Field(default_factory=list, description='Omit to stop the entire group; otherwise only these direct children are stopped.')


class Artifact(Arguments):
    child_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    path: str = ''
    revision: str = ''
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=65536, ge=1)
    latest: bool = False


class Retry(Group):
    request_key: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    child_ids: list[str] = Field(min_length=1)
    instructions: str = Field(min_length=3)
    wait: bool = False



# Friendly public name without changing the established JSON schema.
Task = Assignment
