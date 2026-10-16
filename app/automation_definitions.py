"""Shared automation validation contracts, independent of either execution runtime."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from temporalio.client import ScheduleCalendarSpec, ScheduleRange, ScheduleSpec

from .automation_sources import EventTrigger


class SelfManagement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instructions: bool = False
    schedule: bool = False

    def allowed(self):
        return [key for key, value in self.model_dump().items() if value]


class Timing(BaseModel):
    model_config = ConfigDict(extra='forbid')
    frequency: Literal['hourly', 'daily', 'weekdays', 'weekly', 'cron', 'once'] = 'weekdays'
    time: str = Field(default='09:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    weekday: int = Field(default=1, ge=0, le=6)  # Sunday = 0, as in Temporal.
    timezone: str = Field(default='America/Los_Angeles', max_length=100)
    cron: str = Field(default='', max_length=200)
    run_at: datetime | None = None

    @model_validator(mode='after')
    def valid_schedule(self):
        if self.frequency == 'once':
            if not self.run_at or self.run_at.utcoffset() is None:
                raise ValueError('Choose a one-time date with its timezone.')
            self.run_at = self.run_at.astimezone(UTC).replace(microsecond=0)
        if self.frequency == 'cron':
            fields = self.cron.split()
            if len(fields) != 5:
                raise ValueError('Use five cron fields: minute hour day month weekday.')
            for field, (low, high) in zip(fields, [(0,59),(0,23),(1,31),(1,12),(0,7)]):
                for part in field.split(','):
                    if not re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/\d+)?', part):
                        raise ValueError('Use numeric cron fields with *, lists, ranges, or / steps.')
                    value, *step = part.split('/')
                    values = [int(n) for n in value.split('-')] if value != '*' else []
                    if any(n < low or n > high for n in values) or (len(values) == 2 and values[0] > values[1]) or (step and not 1 <= int(step[0]) <= high-low+1):
                        raise ValueError('Cron contains an out-of-range value.')
        return self

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Choose a valid IANA timezone, such as America/Los_Angeles.') from None
        return value

    def spec(self):
        if self.frequency == 'cron':
            return ScheduleSpec(cron_expressions=[self.cron], time_zone_name=self.timezone)
        if self.frequency == 'once':
            at = self.run_at
            return ScheduleSpec(time_zone_name='UTC', calendars=[ScheduleCalendarSpec(
                year=[ScheduleRange(at.year)], month=[ScheduleRange(at.month)], day_of_month=[ScheduleRange(at.day)],
                hour=[ScheduleRange(at.hour)], minute=[ScheduleRange(at.minute)], second=[ScheduleRange(at.second)])])
        hour, minute = map(int, self.time.split(':'))
        days = [ScheduleRange(1, 5)] if self.frequency == 'weekdays' else [ScheduleRange(self.weekday)] if self.frequency == 'weekly' else [ScheduleRange(0, 6)]
        return ScheduleSpec(time_zone_name=self.timezone, calendars=[ScheduleCalendarSpec(
            hour=[ScheduleRange(0, 23)] if self.frequency == 'hourly' else [ScheduleRange(hour)],
            minute=[ScheduleRange(minute)], day_of_week=days)])


class Trigger(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r'^[A-Za-z0-9_-]{1,40}$')
    schedule: Timing | None = None
    event: EventTrigger | None = None

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.schedule) == bool(self.event):
            raise ValueError('Each trigger must have one schedule or one event source.')
        return self


class Definition(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=100)
    prompt: str = Field(min_length=3)
    triggers: list[Trigger] = Field(min_length=1)
    max_runs_per_hour: int | None = Field(default=50, ge=1)
    queue_events: bool = False
    metadata: dict[Annotated[str, Field(min_length=1)], str] = Field(default_factory=dict)
    self_management: SelfManagement = Field(default_factory=SelfManagement,
        description='Owner-authorized changes this automation and its workers may make to its own instructions or existing schedules. Enable only when the owner requests this. Other definition fields remain fixed.')
    repo_url: str = Field(default='', max_length=500)
    github_repository_id: int | None = Field(default=None, gt=0, strict=True)
    environment_id: str = Field(default='auto', pattern=r'^(auto|none|[0-9a-f]{32})$',
                                description='Reusable project environment id from automation_environments. Each run pins its active validated build. auto matches the repository, or the workspace default when no repository is set; none uses base tools.')
    plugins: list[Literal['linear', 'github', 'slack', 'notion']] = Field(default_factory=list, max_length=4)
    model: str = Field(default='', max_length=120)
    # Missing on old saved definitions: preserve their original runtime.
    harness: str = Field(default='hermes', min_length=1, max_length=80)
    mode: Literal['modal', 'demo'] = 'modal'

    @model_validator(mode='before')
    @classmethod
    def upgrade_single_trigger(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        timing, event = data.pop('timing', None), data.pop('event', None)
        if 'triggers' not in data:
            if event:
                event = dict(event)
                if 'max_runs_per_hour' in event:
                    data.setdefault('max_runs_per_hour', event.pop('max_runs_per_hour'))
                data['triggers'] = [{'id': 'default', 'event': event}]
            else:
                data['triggers'] = [{'id': 'default', 'schedule': timing or {}}]
        sources = [t.model_dump() if isinstance(t, BaseModel) else t for t in data['triggers']] if isinstance(data['triggers'], list) else []
        events = [t.get('event') for t in sources if isinstance(t, dict)]
        events = [e.model_dump() if isinstance(e, BaseModel) else e for e in events]
        if 'max_runs_per_hour' not in data and any(e.get('provider') == 'slack' and e.get('event') == 'message.posted' for e in events if isinstance(e, dict)):
            data['max_runs_per_hour'] = 150
        return data

    @model_validator(mode='after')
    def distinct_triggers(self):
        if len({t.id for t in self.triggers}) != len(self.triggers):
            raise ValueError('Each trigger needs a unique identifier.')
        return self

    @field_validator('repo_url')
    @classmethod
    def repository(cls, value):
        value = value.removesuffix('/')
        if value and not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
            raise ValueError('Use a https://github.com/owner/repository URL.')
        return value


class Save(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition: Definition
    revision: int = Field(default=0, ge=0)


class Toggle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    paused: bool


class Launch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    client_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    item_key: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/-]*$')
