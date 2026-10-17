"""Authorized sidebar ancestry without changing history pagination."""

import pytest
from sqlalchemy import select

from moyai.delegation import DelegationService
from moyai.models import Session, User, new_id
from moyai.product_routes import ProductReadService, Reader


async def listing(runtime, actor, **kwargs):
    return await ProductReadService(runtime).listing(Reader(actor), source="native", scope="mine", **kwargs)


async def test_child_page_includes_authorized_parent_without_consuming_a_history_slot(runtime, actor, active):
    parent, _, prepared = active
    service = DelegationService(runtime.database)
    children = [await service.start(prepared.scope, str(i), f"Task {i}") for i in range(3)]
    first = await listing(runtime, actor, limit=1)
    assert len(first["sessions"]) == 1 and first["has_more"]
    assert first["sessions"][0]["parent_session_id"] == parent.id
    assert [row["id"] for row in first["ancestors"]] == [parent.id]
    ids = [first["sessions"][0]["id"]]
    page = first
    while page["next_cursor"]:
        page = await listing(runtime, actor, limit=1, cursor=page["next_cursor"])
        ids.extend(row["id"] for row in page["sessions"])
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == {parent.id, *(child["session_id"] for child in children)}


async def test_grandchild_page_retains_both_levels_of_ancestry(runtime, actor, active):
    parent, _, prepared = active
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "child", "Child work")
    turn = await runtime.database.claim_next()
    context = await runtime.database.prepare(turn.id)
    grandchild = await service.start(context.scope, "grandchild", "Nested work")
    page = await listing(runtime, actor, limit=1)
    assert page["sessions"][0]["id"] == grandchild["session_id"]
    assert {row["id"] for row in page["ancestors"]} == {parent.id, child["session_id"]}
    child_view = next(row for row in page["ancestors"] if row["id"] == child["session_id"])
    assert child_view["parent_session_id"] == parent.id


@pytest.mark.parametrize("change", ["archived", "deleted", "private_other_owner"])
async def test_unavailable_parent_does_not_hide_the_child_or_leak_parent_context(runtime, actor, active, change):
    parent, _, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "child", "Independent work")
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Session, parent.id)
        if change == "private_other_owner":
            other = User(id=new_id(), username="other", display_name="Other", password="unused")
            db.add(other)
            await db.flush()
            row.owner_id, row.visibility, row.title = other.id, "private", "Hidden parent"
        else:
            setattr(row, change, True)
    page = await listing(runtime, actor, limit=1)
    assert page["sessions"][0]["id"] == child["session_id"]
    assert not page["ancestors"]
    if change != "archived":
        assert page["sessions"][0]["parent_session_id"] is None
    assert "Hidden parent" not in str(page)


async def test_other_viewer_cannot_discover_private_children(runtime, actor, active):
    parent, _, prepared = active
    await DelegationService(runtime.database).start(prepared.scope, "child", "Private task")
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Session, parent.id)
        row.visibility = "shared"
        other = User(id=new_id(), username="other", display_name="Other", password="unused", role="admin")
        db.add(other)
    result = await ProductReadService(runtime).listing(Reader(other), source="native")
    assert [row["id"] for row in result["sessions"]] == [parent.id]
    assert not result["ancestors"]
    assert "Private task" not in str(result)


async def test_search_does_not_inflate_matches_with_parent_context(runtime, actor, active):
    parent, _, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "child", "Task")
    async with runtime.database.sessions.begin() as db:
        row = await db.scalar(select(Session).where(Session.id == child["session_id"]))
        row.title = "Needle child"
    result = await listing(runtime, actor, query="Needle", limit=1)
    assert [row["id"] for row in result["sessions"]] == [child["session_id"]]
    assert result["sessions"][0]["parent_session_id"] == parent.id
    assert not result["ancestors"] and not result["has_more"]
