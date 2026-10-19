from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import test_charge_receipts as receipt_fixtures
from pydantic import SecretStr

from moyai.charge_receipt_server import MAX_RECEIPT_BYTES, ReceiptServerSettings, create_receipt_application
from moyai.product_inference import ProductInferenceRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
billing = receipt_fixtures.billing
cutover = receipt_fixtures.cutover
receipts = receipt_fixtures.receipts
SECRET = "synthetic-receipt-listener-secret-for-local-tests"
AUTH = {"Authorization": "Bearer " + SECRET}


@pytest_asyncio.fixture
async def listener(receipts):
    url = receipts.store.engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://", 1,
    )
    settings = ReceiptServerSettings(
        database_url=SecretStr(url), native_schema=receipts.store.storage.native_schema,
        secret=SecretStr(SECRET), enabled=True, local_rehearsal=True,
    )
    app = create_receipt_application(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://receipts.test") as client,
    ):
        yield SimpleNamespace(receipts=receipts, client=client)


async def test_receipt_listener_commits_before_ack_and_reports_rejections_individually(listener):
    good = await receipt_fixtures.admitted(listener.receipts)
    wrong = (await receipt_fixtures.admitted(listener.receipts)).model_copy(update={"key_hash": "c" * 64})
    response = await listener.client.post(
        "/billing/receipts", json={"events": [good.model_dump(), wrong.model_dump()]}, headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {
        "acknowledged": [good.event_id],
        "rejected": [{"event_id": wrong.event_id, "error": "scope_mismatch"}],
    }
    admission, rows, _ = await receipt_fixtures.saved(listener.receipts, good.request_id)
    assert admission.cost_usd == rows[0].cost_usd == good.amount_usd
    assert (await receipt_fixtures.saved(listener.receipts, wrong.request_id))[0].cost_usd is None
    replay = await listener.client.post(
        "/billing/receipts", json={"events": [good.model_dump()]}, headers=AUTH,
    )
    assert replay.json() == {"acknowledged": [good.event_id], "rejected": []}
    assert (await receipt_fixtures.saved(listener.receipts, good.request_id))[0].cost_usd == good.amount_usd


async def test_authentication_and_body_bounds_precede_financial_writes(listener):
    event = await receipt_fixtures.admitted(listener.receipts)
    unauthenticated = await listener.client.post(
        "/billing/receipts", json={"events": [event.model_dump()]},
    )
    assert unauthenticated.status_code == 401
    oversized = await listener.client.post(
        "/billing/receipts", content=b"x" * (MAX_RECEIPT_BYTES + 1), headers=AUTH,
    )
    assert oversized.status_code == 413
    encoded = await listener.client.post(
        "/billing/receipts", content=b"not-an-accepted-encoding",
        headers={**AUTH, "Content-Encoding": "gzip"},
    )
    assert encoded.status_code == 415
    invalid = await listener.client.post(
        "/billing/receipts",
        json={"events": [{**event.model_dump(), "prompt": "private-canary-not-for-errors"}]},
        headers=AUTH,
    )
    assert invalid.status_code == 422 and "private-canary" not in invalid.text
    assert (await receipt_fixtures.saved(listener.receipts, event.request_id))[0].cost_usd is None


async def test_contended_receipt_is_not_acknowledged_and_can_be_retried(listener):
    event = await receipt_fixtures.admitted(listener.receipts)
    async with listener.receipts.sessions.begin() as held:
        await held.get(ProductInferenceRequest, event.request_id, with_for_update=True)
        unavailable = await listener.client.post(
            "/billing/receipts", json={"events": [event.model_dump()]}, headers=AUTH,
        )
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "temporarily_unavailable"}
    assert (await receipt_fixtures.saved(listener.receipts, event.request_id))[0].cost_usd is None
    accepted = await listener.client.post(
        "/billing/receipts", json={"events": [event.model_dump()]}, headers=AUTH,
    )
    assert accepted.json() == {"acknowledged": [event.event_id], "rejected": []}
    assert (await receipt_fixtures.saved(listener.receipts, event.request_id))[0].cost_usd == event.amount_usd
