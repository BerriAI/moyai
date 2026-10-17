"""Receipt semantics against the real consolidated PostgreSQL ledger."""

from __future__ import annotations

import asyncio
import hashlib
import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import test_billing_storage_cutover as storage_fixtures
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_billing_storage import projection, seed
from test_product_billing import new_row

from moyai.billing_storage_cutover import consolidate
from moyai.billing_storage_models import BillingRecord
from moyai.charge_receipts import ChargeReceipt, ChargeReceiptIngestor
from moyai.inference_usage import InferenceUsage
from moyai.product_inference import ProductInferenceRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
billing = storage_fixtures.billing
cutover = storage_fixtures.cutover


@pytest_asyncio.fixture
async def receipts(cutover):
    await consolidate(cutover.engine, cutover.storage)
    sessions = async_sessionmaker(cutover.native, expire_on_commit=False)
    return SimpleNamespace(
        store=cutover, sessions=sessions, ingestor=ChargeReceiptIngestor(sessions),
    )


async def admitted(ctx, *, observed_cost=None, state="unknown", purpose="memory_review"):
    request_id = str(uuid4())
    key = hashlib.sha256(request_id.encode()).hexdigest()
    usage = new_row(
        id=key, request_key=key, cost_usd=observed_cost,
        cost_status="actual" if observed_cost is not None else "missing",
        cost_source="response_header" if observed_cost is not None else None,
        receipt_key=None, status="interrupted", coverage_error="request_owner_lost",
    )
    await seed(ctx.store, new=[usage])
    async with ctx.sessions.begin() as db:
        db.add(ProductInferenceRequest(
            id=request_id, request_key=key, logical_key=key,
            session_id=usage["session_id"], turn_id=usage["turn_id"], actor_id=usage["actor_id"],
            authority_version="original-authority", model=usage["model"], purpose=purpose,
            gateway_scope="a" * 64, key_hash="b" * 64, status=state,
            cost_usd=observed_cost, created_at=usage["created_at"], finished_at=usage["finished_at"],
            owner_pid=0, owner_started=usage["created_at"],
        ))
    return ChargeReceipt(
        event_id=str(uuid4()), request_id=request_id,
        gateway_scope="a" * 64, key_hash="b" * 64, currency="USD", amount_usd="0.123456789012345678901234",
        model="served-model", input_tokens=17, output_tokens=9, total_tokens=26,
    )


async def saved(ctx, request_id):
    async with ctx.sessions() as db:
        admission = await db.get(ProductInferenceRequest, request_id)
        rows = list((await db.scalars(select(BillingRecord).where(
            BillingRecord.billing_request_id == request_id,
        ))).all())
        primary = await db.get(InferenceUsage, admission.request_key)
        return admission, rows, primary


async def test_delayed_receipt_restores_cost_and_preserves_original_owner_and_unknown_execution(receipts):
    event = await admitted(receipts)
    result = await receipts.ingestor.accept(event)
    assert result.acknowledged and not result.duplicate
    admission, rows, usage = await saved(receipts, event.request_id)
    row, = rows
    assert admission.status == "unknown" and usage.status == "interrupted"
    assert usage.coverage_error == "request_owner_lost"
    assert admission.cost_usd == row.cost_usd == usage.cost_usd == event.amount_usd
    assert row.cost_status == "actual" and row.cost_source == "gateway_receipt"
    assert row.billing_purpose == admission.purpose == "memory_review"
    assert (row.actor_id, row.session_id, row.turn_id) == (
        admission.actor_id, admission.session_id, admission.turn_id,
    )
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (17, 9, 26)


async def test_receipt_ack_overrides_an_asynchronous_database_commit_default(receipts):
    event = await admitted(receipts)
    async with receipts.store.native.connect() as connection:
        await connection.execute(text(
            "ALTER TABLE moyai_billing_records ADD CONSTRAINT receipt_requires_durable_commit "
            "CHECK (billing_request_id IS NULL OR current_setting('synchronous_commit') = 'on') NOT VALID"
        ))
        await connection.execute(text("SET synchronous_commit = off"))
        await connection.commit()
        ingestor = ChargeReceiptIngestor(async_sessionmaker(connection, expire_on_commit=False))
        result = await ingestor.accept(event)
        assert result.acknowledged
        assert await connection.scalar(text("SHOW synchronous_commit")) == "off"
    admission, rows, _ = await saved(receipts, event.request_id)
    assert len(rows) == 1 and admission.cost_usd == event.amount_usd


async def test_concurrent_duplicate_delivery_has_one_effect_and_rejects_changed_payload(receipts):
    event = await admitted(receipts)
    results = await asyncio.gather(*(receipts.ingestor.accept(event) for _ in range(8)))
    assert all(result.acknowledged for result in results)
    assert sum(not result.duplicate for result in results) == 1
    conflict = await receipts.ingestor.accept(event.model_copy(update={"amount_usd": "99"}))
    assert not conflict.acknowledged and conflict.error == "receipt_conflict"
    admission, rows, _ = await saved(receipts, event.request_id)
    assert len(rows) == 1 and admission.cost_usd == event.amount_usd


async def test_multiple_charge_events_for_one_request_count_once_each_in_same_ledger(receipts):
    first = await admitted(receipts, observed_cost="999")
    second = first.model_copy(update={"event_id": str(uuid4()), "amount_usd": "0.000000000000000000000001"})
    results = await asyncio.gather(receipts.ingestor.accept(first), receipts.ingestor.accept(second))
    assert all(result.acknowledged for result in results)
    assert (await receipts.ingestor.accept(first)).duplicate
    assert (await receipts.ingestor.accept(second)).duplicate
    admission, rows, _ = await saved(receipts, first.request_id)
    assert len(rows) == 1 and len(rows[0].billing_receipts) == 2
    assert admission.cost_usd == "0.123456789012345678901235"
    assert sum(Decimal(row.cost_usd) for row in rows) == Decimal(admission.cost_usd)
    assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].total_tokens) == (34, 18, 52)
    assert {row.actor_id for row in rows} == {admission.actor_id}


async def test_multiple_receipts_remain_one_fully_attributed_charge_in_product_report(receipts):
    first = (await admitted(receipts)).model_copy(update={"amount_usd": "0.2"})
    second = first.model_copy(update={"event_id": str(uuid4()), "amount_usd": "0.3"})
    assert (await receipts.ingestor.accept(first)).acknowledged
    assert (await receipts.ingestor.accept(second)).acknowledged
    _, rows, _ = await saved(receipts, first.request_id)
    identifiers = {row.source_id for row in rows}
    report = await projection(receipts.store)
    charges = tuple(
        charge for charge in report.charges
        if any(observation.source.id in identifiers for observation in charge.observations)
    )
    assert len(charges) == 1
    assert charges[0].amount == Decimal("0.5") and charges[0].cost_state == "final"
    assert charges[0].request_count == 1
    assert charges[0].issues == ("request_owner_lost",)


async def test_authoritative_zero_corrects_cached_generation_cost(receipts):
    event = (await admitted(receipts, observed_cost="12.5", state="completed")).model_copy(
        update={"amount_usd": "0"},
    )
    assert (await receipts.ingestor.accept(event)).acknowledged
    admission, rows, usage = await saved(receipts, event.request_id)
    assert admission.status == "completed"
    assert admission.cost_usd == rows[0].cost_usd == usage.cost_usd == "0"
    assert usage.cost_status == "actual"


@pytest.mark.parametrize("field", ["gateway_scope", "key_hash"])
async def test_foreign_scope_is_rejected_without_accounting_changes(receipts, field):
    event = await admitted(receipts)
    result = await receipts.ingestor.accept(event.model_copy(update={field: "c" * 64}))
    assert not result.acknowledged and result.error == "scope_mismatch"
    admission, rows, usage = await saved(receipts, event.request_id)
    assert admission.cost_usd is None and usage.cost_usd is None and not rows
    assert (await receipts.ingestor.accept(event)).acknowledged


async def test_known_unsent_request_cannot_acquire_a_charge(receipts):
    event = await admitted(receipts, state="not_dispatched")
    result = await receipts.ingestor.accept(event)
    assert not result.acknowledged and result.error == "not_dispatched"
    assert (await saved(receipts, event.request_id))[1] == []


async def test_same_gateway_event_cannot_be_reassigned_to_another_request(receipts):
    first = await admitted(receipts)
    second = (await admitted(receipts)).model_copy(update={"event_id": first.event_id})
    results = await asyncio.gather(receipts.ingestor.accept(first), receipts.ingestor.accept(second))
    assert sum(result.acknowledged for result in results) == 1
    assert [result.error for result in results if not result.acknowledged] == ["receipt_conflict"]


async def test_secondary_receipt_is_globally_unique_across_request_rows(receipts):
    first = await admitted(receipts)
    assert (await receipts.ingestor.accept(first)).acknowledged
    secondary = first.model_copy(update={"event_id": str(uuid4())})
    foreign = (await admitted(receipts)).model_copy(update={"event_id": secondary.event_id})
    results = await asyncio.gather(receipts.ingestor.accept(secondary), receipts.ingestor.accept(foreign))
    assert sum(result.acknowledged for result in results) == 1
    assert [result.error for result in results if not result.acknowledged] == ["receipt_conflict"]


async def test_duplicate_amount_formatting_is_canonical_without_rounding(receipts):
    event = (await admitted(receipts)).model_copy(update={"amount_usd": "9999999999999999.123456789012345678901234"})
    assert (await receipts.ingestor.accept(event)).acknowledged
    changed = event.model_copy(update={"amount_usd": "9999999999999999.123456789012345678901235"})
    assert (await receipts.ingestor.accept(changed)).error == "receipt_conflict"
    assert (await saved(receipts, event.request_id))[0].cost_usd == event.amount_usd
    other = (await admitted(receipts)).model_copy(update={"amount_usd": "0.10"})
    assert (await receipts.ingestor.accept(other)).acknowledged
    assert (await receipts.ingestor.accept(other.model_copy(update={"amount_usd": "0.1"}))).duplicate


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "1e50", "0." + "1" * 25])
async def test_invalid_monetary_values_never_enter_the_ledger(receipts, amount):
    event = await admitted(receipts)
    with pytest.raises(ValidationError):
        ChargeReceipt.model_validate({**event.model_dump(), "amount_usd": amount})
    admission, rows, _ = await saved(receipts, event.request_id)
    assert admission.cost_usd is None and not rows
