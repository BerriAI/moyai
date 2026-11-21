"""Retained canonical readers cannot publish notes created by private native chats."""

from types import SimpleNamespace

import pytest
from app.config import Settings
from app.db import Store
from app.memory import Memory, Note, Read
from app.memory_review import MemoryReview
from app.schema_migrations import initialize_components
from app.security import Security
from fastapi import HTTPException


@pytest.fixture
def memory(tmp_path):
    store = Store(tmp_path)
    initialize_components(store)
    settings = Settings(
        _env_file=None, data_dir=tmp_path, public_url="http://127.0.0.1:8787",
        workspace_password="", google_client_id="", google_client_secret="",
        okta_issuer="", okta_client_id="", okta_client_secret="", cloudflare_access_login=False,
    )
    service = Memory(store, Security(settings), lambda owner, actor, **_: owner == actor, None)
    owner = store.identity({"method": "local", "role": "admin"})
    run = store.create_run("Write my update.", "", "demo", [], chat_enabled=True, user_id=owner)
    store.claim_message(run["id"])
    store.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
    run = store.run(run["id"])
    body = Note(key="private-writing", title="Private writing", content="Use Cedar check-ins.",
                request_id="private-capture")
    saved = service.save(owner, body, source={"type": "chat", "visibility": "private"})
    try:
        yield SimpleNamespace(service=service, store=store, settings=settings, owner=owner,
                              run=run, body=body, saved=saved)
    finally:
        store.close()


def test_retained_shared_readers_filter_private_notes_and_stale_selections(memory):
    ctx = memory
    assert ctx.service.search(ctx.run, ctx.owner, "")["matches"] == []
    with pytest.raises(HTTPException) as error:
        ctx.service.read(ctx.run, ctx.owner, Read(
            id=ctx.saved["id"], revision=1, turn_id=ctx.run["active_message_id"],
        ))
    assert error.value.status_code == 404
    ctx.store.execute(
        "INSERT INTO memory_selections(run_id,turn_id,actor_id,owner_id,memory_id) VALUES(?,?,?,?,?)",
        (ctx.run["id"], ctx.run["active_message_id"], ctx.owner, ctx.owner, ctx.saved["id"]),
    )
    context = ctx.service.context(ctx.run)
    assert "Cedar" not in context and "Private writing" not in context
    assert ctx.service.automatic_notes(ctx.run, ctx.owner, []) == []
    review = MemoryReview(ctx.service, ctx.settings, None, None)
    notes, _ = review.notes(ctx.owner, "", [{"content": "Cedar"}])
    assert notes == []


def test_retained_writers_cannot_broaden_private_notes_and_manual_edits_preserve_them(memory):
    ctx = memory
    with pytest.raises(HTTPException) as error:
        ctx.service.save(ctx.owner, ctx.body.model_copy(update={
            "revision": 1, "request_id": "shared-capture",
        }), source={"type": "chat", "run_id": ctx.run["id"]})
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        ctx.service.forget(ctx.owner, ctx.saved["id"], 1, run=ctx.run)
    assert error.value.status_code == 404
    updated = ctx.body.model_copy(update={"revision": 1, "request_id": "manual-private-edit"})
    first = ctx.service.save(ctx.owner, updated)
    assert ctx.service.save(ctx.owner, updated) == first
    assert ctx.service.listing(ctx.owner)[0]["source"] == {"type": "manual", "visibility": "private"}
