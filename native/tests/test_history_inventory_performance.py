"""Bound sidebar reads without weakening current session authorization."""

import pytest
from sqlalchemy import event, update

from moyai.models import Session, User
from moyai.product_routes import ProductReadService, Reader


@pytest.mark.parametrize("archived", [False, True])
async def test_web_inventory_query_count_does_not_grow_per_session(runtime, actor, archived):
    async with runtime.database.sessions.begin() as db:
        db.add_all([
            Session(id=f"inventory-{i:03}", owner_id=actor.id, title=f"Task {i}",
                    model="test", visibility="private", archived=archived)
            for i in range(101)
        ])
    service = ProductReadService(runtime)
    statements = []

    def count(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = runtime.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        first = await service.listing(Reader(actor), source="native", scope="mine", limit=1,
                                      archived=archived)
        small = len(statements)
        statements.clear()
        page = await service.listing(Reader(actor), source="native", scope="mine", limit=100,
                                     archived=archived)
        large = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(first["sessions"]) == 1
    assert len(page["sessions"]) == 100 and page["has_more"] and page["next_cursor"]
    assert all(row["can_manage"] and row["capabilities"]["rename"] for row in page["sessions"])
    assert large <= small + 2, f"Sidebar reads grew from {small} to {large} queries"
    assert large < 20


async def test_inventory_rechecks_web_visibility_after_source_read(runtime, actor, monkeypatch):
    async with runtime.database.sessions.begin() as db:
        db.add(User(id="other-viewer", username="other", display_name="Other", password="unused"))
        await db.flush()
        db.add_all([
            Session(id="shared-task", owner_id="other-viewer", title="Was shared", model="test"),
            Session(id="deleted-task", owner_id=actor.id, title="Was visible", model="test"),
            Session(id="own-task", owner_id=actor.id, title="Mine", model="test", visibility="private"),
        ])
    service = ProductReadService(runtime)
    original = service.native_list

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == "shared-task").values(visibility="private"))
            await db.execute(update(Session).where(Session.id == "deleted-task").values(deleted=True))
        return result

    monkeypatch.setattr(service, "native_list", revoke)
    page = await service.listing(Reader(actor), source="native")
    assert [row["id"] for row in page["sessions"]] == ["own-task"]


async def test_shared_inventory_checks_each_owner_once_and_preserves_capabilities(runtime, actor, monkeypatch):
    async with runtime.database.sessions.begin() as db:
        db.add(User(id="shared-owner", username="owner", display_name="Owner", password="unused"))
        await db.flush()
        db.add_all([
            Session(id=f"shared-{i:03}", owner_id="shared-owner", title=f"Shared task {i}", model="test")
            for i in range(100)
        ])
    checked = []
    original = runtime.database.same_person

    async def same_person(db, viewer, owner):
        checked.append(owner)
        return await original(db, viewer, owner)

    monkeypatch.setattr(runtime.database, "same_person", same_person)
    page = await ProductReadService(runtime).listing(Reader(actor), source="native", limit=100)
    assert len(page["sessions"]) == 100
    assert all(not row["can_manage"] and not row["capabilities"]["rename"] for row in page["sessions"])
    assert checked == ["shared-owner"]
