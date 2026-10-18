"""Slack delivery consumes commit hints without weakening its durable fallback."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_dispatch_capacity import postgres_database as database_fixture

from moyai.contracts import NewMessage, NewSession
from moyai.dispatch_wakeups import DispatchWakeups
from moyai.models import Turn
from moyai.product_slack import ProductSlack
from moyai.worker import dispatcher

postgres_database = database_fixture


async def test_settled_answer_wakes_slack_on_existing_dispatch_connection(postgres_database, monkeypatch):
    import moyai.product_slack as slack_module
    import moyai.worker as worker_module

    monkeypatch.setattr(slack_module, "DELIVERY_POLL_SECONDS", 30)
    monkeypatch.setattr(slack_module, "MIN_DELIVERY_INTERVAL", .01)
    database, actor = postgres_database
    session = await database.create_session(actor, NewSession())
    turn = await database.submit(actor, session.id, NewMessage(prompt="Test", request_key="wake-answer"))
    await database.claim_next()
    prepared = await database.prepare(turn.id)
    assert await database.save_result(
        prepared.scope, conversation="{}", output="Ready", workspace=None, usage={},
    )
    first_scan, delivered = asyncio.Event(), asyncio.Event()

    async def pump():
        async with database.sessions() as db:
            saved = await db.get(Turn, turn.id)
            if saved.status == "completed":
                delivered.set()
        first_scan.set()

    slack = SimpleNamespace(delivery_wakeup=asyncio.Event(), stopping=asyncio.Event(), pump=pump)
    runtime = SimpleNamespace(
        database=database, settings=database.settings, product_slack=slack, product_automations=None,
        connect=AsyncMock(return_value=SimpleNamespace(start_workflow=AsyncMock())),
    )
    listeners = []

    def observe(settings, **kwargs):
        listener = DispatchWakeups(settings, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(worker_module, "DispatchWakeups", observe)
    dispatch = asyncio.create_task(dispatcher(runtime))
    delivery = asyncio.create_task(ProductSlack.watch(slack))
    try:
        await asyncio.wait_for(first_scan.wait(), 2)
        assert len(listeners) == 1
        await asyncio.wait_for(listeners[0].ready.wait(), 5)
        await database.answers.settle(turn.id)
        # The ordinary fallback is 30 seconds here; the committed settlement
        # wakes the real watcher through the dispatcher's single SQL listener.
        await asyncio.wait_for(delivered.wait(), 2)
        assert listeners[0].observers == (slack.delivery_wakeup,)
    finally:
        dispatch.cancel()
        delivery.cancel()
        await asyncio.gather(dispatch, delivery, return_exceptions=True)


async def test_hint_during_pump_is_retained_and_bursts_are_paced(monkeypatch):
    import moyai.product_slack as slack_module

    monkeypatch.setattr(slack_module, "DELIVERY_POLL_SECONDS", 30)
    monkeypatch.setattr(slack_module, "MIN_DELIVERY_INTERVAL", .05)
    starts = []
    twice = asyncio.Event()
    slack = SimpleNamespace(delivery_wakeup=asyncio.Event(), stopping=asyncio.Event())

    async def pump():
        starts.append(time.monotonic())
        slack.delivery_wakeup.set()  # Arrives while reading/sending, before wait.
        if len(starts) == 2:
            twice.set()

    slack.pump = pump
    task = asyncio.create_task(ProductSlack.watch(slack))
    try:
        await asyncio.wait_for(twice.wait(), 1)
        assert starts[1] - starts[0] >= .05
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("first_fails", [False, True])
async def test_slack_watcher_keeps_polling_without_notifications(monkeypatch, first_fails):
    import moyai.product_slack as slack_module

    monkeypatch.setattr(slack_module, "DELIVERY_POLL_SECONDS", .02)
    monkeypatch.setattr(slack_module, "MIN_DELIVERY_INTERVAL", .01)
    completed = asyncio.Event()
    attempts = []

    async def pump():
        attempts.append(1)
        if len(attempts) == 1 and first_fails:
            raise OSError("Temporary store outage")
        if len(attempts) == 2:
            completed.set()

    slack = SimpleNamespace(delivery_wakeup=asyncio.Event(), stopping=asyncio.Event(), pump=pump)
    task = asyncio.create_task(ProductSlack.watch(slack))
    try:
        await asyncio.wait_for(completed.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_planned_stop_interrupts_idle_wait_without_another_pump(monkeypatch):
    import moyai.product_slack as slack_module

    monkeypatch.setattr(slack_module, "DELIVERY_POLL_SECONDS", 30)
    monkeypatch.setattr(slack_module, "MIN_DELIVERY_INTERVAL", 1)
    entered = asyncio.Event()
    calls = []

    async def pump():
        calls.append(True)
        entered.set()

    slack = SimpleNamespace(delivery_wakeup=asyncio.Event(), stopping=asyncio.Event(), pump=pump)
    task = asyncio.create_task(ProductSlack.watch(slack))
    await entered.wait()
    slack.stopping.set()
    await asyncio.wait_for(task, .2)
    assert calls == [True]
