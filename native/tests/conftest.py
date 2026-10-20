from __future__ import annotations

import asyncio
import gc
import shutil
from pathlib import Path

import product_database_fixtures as database_fixtures
import pytest
import pytest_asyncio
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from moyai.contracts import NewMessage, NewSession
from moyai.models import User
from moyai.runtime import Runtime
from moyai.settings import Settings

prepared_product_database = database_fixtures.prepared_product_database
product_database_template = database_fixtures.product_database_template


@pytest.fixture(autouse=True)
def collect_test_cycles():
    yield
    # Release cycles from closed runtimes after the other fixtures tear down.
    # Otherwise a later Temporal activation can pay for many earlier tests'
    # garbage collection and exceed the SDK's two-second deadlock watchdog.
    gc.collect()


@pytest_asyncio.fixture
async def model_gates():
    return asyncio.Event(), asyncio.Event()


@pytest_asyncio.fixture
async def runtime(tmp_path, request, model_gates):
    workspace = tmp_path / "computer"
    workspace.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", workspace / "skills")
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data",
        workspace_password="test-password-12345", default_model="test",
        database_url="", encryption_key="",
    )
    runtime = Runtime(settings)
    async def stream(messages, info):
        yield "Task "
        yield "completed."

    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("Task completed.")]),
        stream_function=stream,
    )
    if hasattr(request, "param"):
        from scenarios import integration_model
        model = integration_model(request.param, gates=model_gates)
    await runtime.start(
        models={"test": model}, workspace=LocalWorkspace(workspace), skills_directory="skills"
    )
    yield runtime
    await runtime.close()


@pytest_asyncio.fixture
async def actor(runtime):
    async with runtime.database.sessions() as db:
        return await db.scalar(select(User).where(User.username == "admin"))


@pytest_asyncio.fixture
async def active(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Perform the task.", request_key="request-one")
    )
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    return session, turn, prepared
