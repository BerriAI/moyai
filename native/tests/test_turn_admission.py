"""Admission through its public interface with real PostgreSQL concurrency."""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_dispatch_capacity import postgres_database
from test_product_role_authority import role

from moyai.contracts import NewMessage, NewSession
from moyai.models import Event, Message, Turn
from moyai.product_role_authority import ProductOwnershipLost

__all__ = ["postgres_database", "role"]


async def test_concurrent_duplicates_keep_one_input_and_preparation(postgres_database):
    database, actor = postgres_database
    session = await database.create_session(actor, NewSession())
    body = NewMessage(prompt="One admitted message", request_key="same-request")
    turns = await asyncio.wait_for(asyncio.gather(*(
        database.admission.submit(actor, session.id, body) for _ in range(8)
    )), 10)
    assert len({turn.id for turn in turns}) == 1
    with pytest.raises(HTTPException) as conflict:
        await database.admission.submit(
            actor, session.id, body.model_copy(update={"prompt": "Different message"}),
        )
    assert conflict.value.status_code == 409
    claimed = await database.claim_next()
    assert claimed is not None and claimed.id == turns[0].id
    first = await database.admission.prepare(claimed.id)
    assert first.prompt == body.prompt
    assert await database.admission.prepare(claimed.id) == first
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        assert list(await db.scalars(select(Event.kind).order_by(Event.id))) == [
            "performance.v1.received", "performance.v1.worker",
        ]


async def test_late_role_factory_fences_both_new_input_and_cached_preparation(postgres_database, role):
    database, actor = postgres_database
    admission = database.admission
    session = await database.create_session(actor, NewSession())
    turn = await admission.submit(actor, session.id, NewMessage(prompt="Saved", request_key="saved"))
    await database.claim_next()
    original_sessions = database.sessions
    # Runtime.start installs this real factory after constructing admission.
    database.sessions = role.authority.sessions(database.engine)
    prepared = await admission.prepare(turn.id)
    assert prepared.prompt == "Saved"
    await role.admin.execute(
        f'UPDATE "{role.schema}".runtime_api_policy SET fingerprint=$1', "c" * 64,
    )
    with pytest.raises(ProductOwnershipLost):
        await admission.submit(actor, session.id, NewMessage(prompt="Revoked", request_key="revoked"))
    with pytest.raises(ProductOwnershipLost):
        await admission.prepare(turn.id)
    async with original_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
        assert (await db.get(Turn, turn.id)).status == "running"
