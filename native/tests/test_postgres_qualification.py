"""Opt-in qualification against an isolated local PostgreSQL server."""

import asyncio
import itertools
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import httpx
import pytest
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from scenarios import integration_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.testing import WorkflowEnvironment

from moyai.api import create_app
from moyai.computer import DESKTOP_VERSION, lease
from moyai.computer_stream import bind, release, validate
from moyai.contracts import NewMessage, NewSession
from moyai.models import Artifact, ComputerDesktop, Delivery, Session, User, new_id
from moyai.runtime import Runtime
from moyai.settings import Settings
from moyai.slack import deliver_one
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
async def test_native_postgres_stores_in_temporal(tmp_path, monkeypatch):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = f"moyai_test_{uuid4().hex}"
    admin = await asyncpg.connect(urlunsplit(base))
    await admin.execute(f'CREATE DATABASE {name} ENCODING \'UTF8\' TEMPLATE template0')
    runtime = Runtime(Settings(
        _env_file=None, data_dir=tmp_path / "data",
        database_url=urlunsplit(base._replace(path=f"/{name}")),
        encryption_key="local-postgres-qualification-key-2026-only",
        workspace_password="test-password-12345", default_model="test",
    ))
    workspace = tmp_path / "computer"
    workspace.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", workspace / "skills")
    try:
        await runtime.start(
            models={"test": integration_model("tools")},
            workspace=LocalWorkspace(workspace), skills_directory="skills",
        )
        async with runtime.database.sessions() as db:
            actor = await db.scalar(select(User))
        session = await runtime.database.create_session(actor, NewSession())
        turn = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Verify the native stores.", request_key="postgres-proof"
        ))
        await runtime.database.claim_next()
        environment = await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()],
            data_converter=encrypted_converter(runtime.security, runtime.objects),
        )
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                result = await runtime.client.execute_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id,
                    task_queue=runtime.settings.temporal_queue,
                )
        assert result["status"] == "completed"
        assert len(await runtime.stores.plan(session.id).get_items()) == 1
        snapshot = await runtime.stores.steps.latest_snapshot(run_id=turn.id)
        assert snapshot is not None and snapshot.messages
        async with runtime.database.sessions() as db:
            saved = await db.scalar(select(Artifact).where(Artifact.turn_id == turn.id))
            assert saved.name == "proof.txt"
        await qualify_pool_wait(runtime, session.id, turn.id, monkeypatch)
        # Qualify the admission policy in PostgreSQL without making a provider
        # call: the real Modal desktop is exercised by the separate live proof.
        async with runtime.database.sessions.begin() as db:
            saved_session = await db.get(Session, session.id)
            saved_session.workspace = json.dumps({"provider": "modal", "id": "policy-only"})
        await lease(runtime, session.id, actor, "claim")
        following = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Continue after handoff.", request_key="postgres-human-control",
        ))
        assert (await runtime.database.claim_next()).id == following.id
        await lease(runtime, session.id, actor, "release")
        assert await runtime.database.claim_next() is None
        async with runtime.database.sessions.begin() as db:
            desktop = await db.get(ComputerDesktop, session.id)
            desktop.workspace_id = "policy-only"
            desktop.version = DESKTOP_VERSION
        client_id = new_id()
        await lease(runtime, session.id, actor, "claim", client_id)
        previous = await bind(runtime, session.id, actor, client_id, controlling=True)
        current = await bind(runtime, session.id, actor, client_id, controlling=True)
        await release(runtime, previous)
        await validate(runtime, current)
        await release(runtime, current)
    finally:
        await runtime.close()
        await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()


async def qualify_pool_wait(runtime, session_id, turn_id, monkeypatch):
    """Exercise Slack and health through a real exhausted PostgreSQL pool."""
    async with runtime.database.sessions.begin() as db:
        delivery = Delivery(
            id=new_id(), identity="pool-proof", session_id=session_id, turn_id=turn_id,
            destination=json.dumps({"channel": "CTEST", "thread_ts": "123.456"}),
            content=runtime.security.seal("Pool proof."),
        )
        db.add(delivery)
    engine = create_async_engine(runtime.settings.sqlalchemy_url, pool_size=1, max_overflow=0)
    ticks = []
    sent = []

    async def heartbeat():
        while True:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.02)

    async def transport(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "ts": "123.999"})

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.database, "sessions", async_sessionmaker(engine, expire_on_commit=False))
            async with (
                httpx.AsyncClient(
                    transport=httpx.MockTransport(transport), base_url="https://slack.test/"
                ) as slack,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
                    base_url="http://test",
                ) as web,
                engine.connect() as held,
            ):
                started = asyncio.get_running_loop().time()
                pulse = asyncio.create_task(heartbeat())
                sending = asyncio.create_task(deliver_one(runtime, slack, delivery))
                health = asyncio.create_task(web.get("/health"))
                try:
                    await asyncio.sleep(0.65)
                    assert not sending.done() and not health.done() and sent == []
                finally:
                    await held.close()
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
                response = await asyncio.wait_for(health, 5)
                await asyncio.wait_for(sending, 5)
                assert response.status_code == 200
                assert asyncio.get_running_loop().time() - started >= 0.65
        assert len(ticks) >= 10
        assert max(b - a for a, b in itertools.pairwise(ticks)) < 0.3
        assert sent == ["/chat.postMessage"]
    finally:
        await engine.dispose()
