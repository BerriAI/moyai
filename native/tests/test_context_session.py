import asyncio
from collections.abc import Sequence
from typing import ClassVar

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from moyai.context import ContextSource, SessionContext
from moyai.context.github import GitHubRepositories
from moyai.contracts import Grant, RunScope


class Source(ContextSource):
    name: ClassVar[str] = "source"

    def __init__(self, value: str):
        self.value = value

    async def render(self, scope: RunScope) -> str:
        return self.value


class DelayedSource(ContextSource):
    name: ClassVar[str] = "delayed"

    def __init__(self, value: str, started: asyncio.Event):
        self.value = value
        self.started = started

    async def render(self, scope: RunScope) -> str:
        await self.started.wait()
        return self.value


class StartingSource(ContextSource):
    name: ClassVar[str] = "starting"

    def __init__(self, started: asyncio.Event):
        self.started = started

    async def render(self, scope: RunScope) -> str:
        self.started.set()
        return "second"


class RaisingSource(ContextSource):
    name: ClassVar[str] = "broken"

    async def render(self, scope: RunScope) -> str:
        raise RuntimeError("source unavailable")


def scope() -> RunScope:
    return RunScope(session_id="session", turn_id="turn", actor_id="actor", model="test")


@pytest.mark.asyncio
async def test_sources_render_concurrently_and_keep_source_order() -> None:
    started = asyncio.Event()
    context = SessionContext(sources=[
        DelayedSource("first", started),
        StartingSource(started),
        Source(""),
        Source("last"),
    ])

    rendered = await asyncio.wait_for(
        context.get_instructions()(
            RunContext(deps=scope(), model=TestModel(), usage=RunUsage()),
        ),
        timeout=1,
    )

    assert rendered == "first\n\nsecond\n\nlast"


@pytest.mark.asyncio
async def test_failing_source_is_logged_and_other_sources_are_rendered(caplog: pytest.LogCaptureFixture) -> None:
    context = SessionContext(sources=[
        Source("before"),
        RaisingSource(),
        Source("after"),
    ])

    rendered = await context.get_instructions()(
        RunContext(deps=scope(), model=TestModel(), usage=RunUsage()),
    )

    assert rendered == "before\n\nafter"
    assert "Session context source broken failed" in caplog.text


@pytest.mark.asyncio
async def test_async_context_is_included_in_the_model_request() -> None:
    repository_grant = Grant(
        id="grant",
        kind="github",
        revision=1,
        credential="",
        policy={"product": {"repository_id": 42}},
    )

    class RepositoryNames:
        async def __call__(self, repository_ids: Sequence[int]) -> list[str]:
            assert repository_ids == (42,)
            return ["BerriAI/moyai"]

    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("done")]))
    capability = SessionContext(sources=[GitHubRepositories(RepositoryNames())])
    agent = Agent(
        model,
        deps_type=RunScope,
        capabilities=[capability],
    )

    result = await agent.run(
        "What does our team work on?",
        deps=RunScope(
            session_id="session",
            turn_id="turn",
            actor_id="actor",
            model="test",
            grants=[repository_grant],
        ),
    )

    request = next(message for message in result.all_messages() if isinstance(message, ModelRequest))
    assert request.instructions == (
        "Connected GitHub repositories for this session: BerriAI/moyai. "
        "Use them to work out what the user's team works on."
    )
