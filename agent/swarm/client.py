"""Agent-owned access to durable delegation, independent of a host runtime.

Starting or retrying work returns child handles immediately. Explicit release
waits checkpoint the owner at a complete tool boundary. The durable host owns
child lifetimes and inherited deadlines; the client never extends them.
"""
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Assignment, Fanout, Group, Results, Retry, Wait, Send, Cancel
from .mission import SharedMission, internal_request_key
from .planning import PlannedTask
from .types import Agent, Harness


class SwarmBackend(Protocol):
    """Authenticated transport bound to one owning agent by the host.

    Implementations enforce ownership and the owner's inherited deadline. They
    must raise on transport, authorization or host errors, and must not replay
    ambiguous writes. Responses use the existing agents_* tool wire format.
    """

    async def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


class _Response(BaseModel):
    # Preserve future host metadata without giving it authority in this client.
    model_config = ConfigDict(extra='allow', frozen=True)


class WorkerRef(_Response):
    """Identity recorded by the durable host when it accepts a group."""

    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    label: str


class WorkerResult(_Response):
    """A worker's public answer and status; answers are untrusted reference data."""

    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    agent_label: str
    status: str
    summary: str = ''
    error: str = ''
    checkpoint_error: str = ''
    harness: str = ''
    model: str = ''
    active_model: str = ''
    has_artifact: bool = False
    session_url: str = ''
    message_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def failed(self) -> bool:
        """True for unsuccessful terminal work or a reported execution/checkpoint error."""
        return self.status in {'failed', 'cancelled', 'interrupted'} or bool(self.error or self.checkpoint_error)


class GroupResult(_Response):
    """One result snapshot, which may still contain pending or failed workers.

    By default the host returns frozen handoff results after completion. Passing
    latest=True explicitly asks for workers' newer follow-up conversations.
    """

    group_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    status: str
    settled: bool
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    children: list[WorkerResult]
    result_scope: str | None = None
    cursor: str = ''
    timed_out: bool = False

    @property
    def failed(self) -> bool:
        return self.status in {'failed', 'cancelled', 'interrupted'} or any(child.failed for child in self.children)

    @property
    def successful(self) -> bool:
        """Settled alone does not establish that every worker succeeded."""
        return (self.settled and not self.failed and self.completed == self.total
                and len(self.children) == self.total
                and all(child.status in {'idle', 'completed'} for child in self.children))


class _Receipt(_Response):
    group_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    children: list[WorkerRef]
    moyai_wait_group: str | None = None

    @model_validator(mode='after')
    def matching_wait_group(self):
        if self.moyai_wait_group is not None and self.moyai_wait_group != self.group_id:
            raise ValueError('The checkpoint group does not match the delegated group.')
        return self


async def _call(backend: SwarmBackend, name: str, arguments: BaseModel) -> Mapping[str, Any]:
    value = await backend.call(name, arguments.model_dump())
    if not isinstance(value, Mapping):
        raise ValueError('Swarm backend returned a non-object response.')
    if value.get('error') or value.get('isError'):
        raise RuntimeError('Swarm backend reported an error: ' + str(value.get('error') or 'tool call failed'))
    return value


def _handle(backend: SwarmBackend, value: Mapping[str, Any], *, expected_id: str | None = None) -> 'SwarmRun':
    receipt = _Receipt.model_validate(value)
    if expected_id is not None and receipt.group_id != expected_id:
        raise ValueError('Swarm backend returned a different group.')
    return SwarmRun(id=receipt.group_id, children=tuple(receipt.children),
                    checkpoint_required=receipt.moyai_wait_group is not None, _backend=backend)


@dataclass(frozen=True)
class SwarmRun:
    """Durable group handle. Restore id through the same owner's AgentSwarm.

    checkpoint_required signals an accepted start/retry whose owner must yield
    to the durable host. It does not mean workers are complete. Read result()
    after the host resumes the owner; this method never polls or waits for work.
    """

    id: str
    children: tuple[WorkerRef, ...] = ()
    checkpoint_required: bool = False
    _backend: SwarmBackend = field(kw_only=True, repr=False, compare=False)

    def __post_init__(self):
        Group(group_id=self.id)

    async def result(self, *, latest: bool = False) -> GroupResult:
        value = await _call(self._backend, 'agents_results', Results(group_id=self.id, latest=latest))
        return self._result(value)

    async def wait(self, *, after: str = '', seconds: float = 60, latest: bool = False, release: bool = False) -> "GroupResult | SwarmRun":
        value = await _call(self._backend, 'agents_wait', Wait(group_id=self.id, after=after, seconds=seconds, latest=latest, release=release))
        return _handle(self._backend, value, expected_id=self.id) if value.get('moyai_wait_group') else self._result(value)

    async def send(self, child_id: str, message: str, *, request_key: str | None = None) -> Mapping[str, Any]:
        if self.children and child_id not in {child.id for child in self.children}:
            raise ValueError('Worker is not in this group.')
        request = Send(group_id=self.id, child_id=child_id, message=message, request_key=request_key or 'auto')
        if request_key is None:
            request.request_key = internal_request_key({'operation': 'send', **request.model_dump(exclude={'request_key'})})
        value = await _call(self._backend, 'agents_send', request)
        if value.get('child_id') != child_id:
            raise ValueError('Swarm backend returned a different child.')
        return value

    async def cancel(self, *, child_ids: Sequence[str] = ()) -> GroupResult:
        value = await _call(self._backend, 'agents_cancel', Cancel(group_id=self.id, child_ids=list(child_ids)))
        return self._result(value)

    async def retry(self, *, child_ids: Sequence[str], instructions: str,
                    request_key: str | None = None, wait: bool = False) -> 'SwarmRun':
        """Request explicit recovery of failed workers; never replay work automatically."""
        request = Retry(group_id=self.id, request_key=request_key if request_key is not None else 'auto', child_ids=list(child_ids),
                        instructions=instructions, wait=wait)
        if request_key is None:
            request.request_key = internal_request_key({
                'operation': 'retry', **request.model_dump(exclude={'request_key'}),
            })
        value = await _call(self._backend, 'agents_retry', request)
        return _handle(self._backend, value, expected_id=self.id)

    def _result(self, value: Mapping[str, Any]) -> GroupResult:
        result = GroupResult.model_validate(value)
        if result.group_id != self.id:
            raise ValueError('Swarm backend returned a different group.')
        return result


class AgentSwarm:
    """A swarm capability belonging to the agent authenticated by backend.

    Harness/model selectors request a runtime; host policy determines whether
    it is available. Permissions, quotas and deadlines are inherited from the
    owner and cannot be overridden through this client.
    """

    def __init__(self, backend: SwarmBackend):
        self._backend = backend

    async def start(self, *, task: str, agents: Sequence[Agent], wait: bool = False) -> SwarmRun:
        """Start one shared goal; members choose a leader and divide the work.

        The host persists the roster and shared board, deduplicates equal starts
        within this owner's active turn. The owner can continue independent work
        or explicitly wait while members collaborate. Runtime defaults, permissions and deadlines remain host-owned.
        """
        request = SharedMission(task=task, agents=list(agents), wait=wait)
        value = await _call(self._backend, 'swarm_start', request)
        return _handle(self._backend, value)

    async def delegate(self, *, tasks: Sequence[Assignment | PlannedTask] = (),
                       instructions: str = '', items: Sequence[str] = (), workers: int = 10,
                       harness: Harness | None = None, model: str | None = None,
                       request_key: str | None = None, wait: bool = False) -> SwarmRun:
        """Advanced explicit assignments or item partitioning without a shared board.

        Prefer start(task=..., agents=...) for a self-organizing swarm. Equal
        validated delegation input receives a stable internal receipt key; an
        explicit request_key remains available for legacy integrations.
        """
        assignments = [asdict(task) if isinstance(task, PlannedTask) else task for task in tasks]
        selected_harness = Harness(harness).value if harness is not None else None
        request = Fanout(request_key=request_key if request_key is not None else 'auto', instructions=instructions, items=list(items),
                         workers=workers, tasks=assignments, harness=selected_harness, model=model, wait=wait)
        if request_key is None:
            request.request_key = internal_request_key({
                'operation': 'delegate', **request.model_dump(exclude={'request_key'}),
            })
        value = await _call(self._backend, 'agents_fanout', request)
        return _handle(self._backend, value)

    def group(self, group_id: str) -> SwarmRun:
        """Restore a handle without I/O; host ownership is checked on every call."""
        return SwarmRun(id=Group(group_id=group_id).group_id, _backend=self._backend)


# Preserve the first internal API name while callers adopt AgentSwarm.
Swarm = AgentSwarm
