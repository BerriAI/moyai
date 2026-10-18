"""Supplementary compatibility tests against existing-format, read-only SQLite.

These fixtures do not claim deployed-app or real-provider qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moyai.models import Base, SessionPreference, Turn, User
from moyai.models import Message as NativeMessage
from moyai.models import Session as NativeSession
from moyai.product_files import PREVIEW_BYTES, ProductFilesAdapter
from moyai.product_files import routes as file_routes
from moyai.product_history import MAX_MESSAGE_CHARS, ProductHistoryAdapter, routes
from moyai.product_routes import ProductReadService, Reader, merge_session_lists
from moyai.product_routes import routes as unified_routes
from moyai.security import Security

ROOT = "1" * 32
PRIVATE = "2" * 32
CHILD = "3" * 32
OTHER = "4" * 32
ATTACHMENT = "a" * 32
ALICE = "google:original-subject-with-an-id-longer-than-36-characters"
BOB = "cloudflare:bob"

# Existing product column names, not native tables. No legacy Store constructor,
# bootstrap, execution service or migration is used by the compatibility reader.
SCHEMA = """
CREATE TABLE users(id TEXT PRIMARY KEY,kind TEXT,name TEXT,linked_user_id TEXT);
CREATE TABLE runs(id TEXT PRIMARY KEY,owner_id TEXT,private_owner_id TEXT DEFAULT '',
 status TEXT DEFAULT 'completed',model TEXT DEFAULT 'old-model',harness TEXT DEFAULT 'hermes',
 parent_run_id TEXT DEFAULT '',side_chat_of TEXT DEFAULT '',display_title TEXT DEFAULT '',
 agent_label TEXT DEFAULT '',prompt TEXT DEFAULT '',created_at TEXT DEFAULT '2026-10-01',
 updated_at TEXT DEFAULT '2026-10-02',deleted_at TEXT DEFAULT '',deletion_requested_at TEXT DEFAULT '',
 token_hash TEXT DEFAULT 'DO-NOT-EXPOSE',pending_result TEXT DEFAULT 'PRIVATE-WORKING-CONTEXT',
 side_chat_context TEXT DEFAULT 'PRIVATE-TOOLS');
CREATE TABLE run_roots(run_id TEXT PRIMARY KEY,root_id TEXT);
CREATE TABLE run_ancestry(run_id TEXT,ancestor_id TEXT,PRIMARY KEY(run_id,ancestor_id));
CREATE TABLE messages(id INTEGER PRIMARY KEY,run_id TEXT,role TEXT,content TEXT,status TEXT DEFAULT 'completed',
 user_id TEXT DEFAULT '',started_at TEXT DEFAULT '2026-10-01',created_at TEXT DEFAULT '2026-10-01',
 response_to_id INTEGER,model TEXT DEFAULT 'old-model',revision INTEGER DEFAULT 1);
CREATE TABLE session_pins(owner_id TEXT,run_id TEXT);
CREATE TABLE session_archives(owner_id TEXT,run_id TEXT);
CREATE TABLE session_folders(id TEXT PRIMARY KEY,owner_id TEXT,name TEXT,revision INTEGER DEFAULT 1);
CREATE TABLE session_folder_memberships(owner_id TEXT,run_id TEXT,folder_id TEXT);
CREATE TABLE slack_threads(run_id TEXT);
CREATE TABLE slack_events(run_id TEXT);
CREATE TABLE attachments(id TEXT PRIMARY KEY,message_id INTEGER,owner_id TEXT,name TEXT,size INTEGER,
 media_type TEXT DEFAULT 'text/plain',sha256 TEXT,created_at TEXT DEFAULT '2026-10-01',
 upload_state TEXT DEFAULT 'ready',part_size INTEGER DEFAULT 0,data BLOB,data_ref TEXT DEFAULT '');
CREATE TABLE attachment_parts(attachment_id TEXT,part INTEGER,size INTEGER,sha256 TEXT,reference TEXT,data BLOB);
CREATE TABLE published_artifacts(run_id TEXT,path TEXT,revision TEXT,upload_id TEXT,size INTEGER,
 media_type TEXT,created_at TEXT DEFAULT '2026-10-01',source_turn INTEGER);
CREATE TABLE artifact_parts(run_id TEXT,upload_id TEXT,part INTEGER,data BLOB,reference TEXT,sha256 TEXT,size INTEGER);
CREATE TABLE artifact_objects(name TEXT PRIMARY KEY,reference TEXT,size INTEGER,sha256 TEXT,created_at TEXT);
"""


@dataclass(frozen=True)
class Person:
    id: str = ALICE
    role: str = "member"
    individual: bool = True
    version: int = 1

    @property
    def grant(self):
        return self.id, self.version


class Identities:
    def __init__(self):
        self.person = Person()
        self.revoked = False
        self.checks = 0
        self.on_check = None

    async def authenticate(self, request, *, mutation=False, admin=False):
        if request.headers.get("x-test-auth") != "yes":
            raise HTTPException(401, "Sign in.")
        return await self.require_current(self.person.grant)

    async def require_current(self, grant):
        self.checks += 1
        if self.on_check is not None:
            await self.on_check(self.checks)
        if self.revoked or grant != self.person.grant:
            raise HTTPException(401, "Sign in again.")
        return self.person


@pytest_asyncio.fixture
async def product(tmp_path):
    path = tmp_path / "existing.db"
    writer = create_async_engine("sqlite+aiosqlite:///" + str(path))
    async with writer.begin() as conn:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        for person, kind in ((ALICE, "google"), (BOB, "cloudflare"), ("slack:T:U", "slack")):
            await conn.execute(text("INSERT INTO users VALUES(:id,:kind,:name,:linked)"),
                               {"id": person, "kind": kind, "name": person.split(":")[-1],
                                "linked": ALICE if kind == "slack" else None})
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    identity = Identities()
    history = ProductHistoryAdapter(identity, engine)
    files = ProductFilesAdapter(history)

    async def sql(query, **params):
        async with writer.begin() as conn:
            return await conn.execute(text(query), params)

    async def run(id=ROOT, owner=ALICE, private="", parent="", title="Earlier session", **fields):
        values = {"id": id, "owner_id": owner, "private_owner_id": private,
                  "parent_run_id": parent, "display_title": title, **fields}
        await sql("INSERT INTO runs(" + ",".join(values) + ") VALUES("
                  + ",".join(":" + key for key in values) + ")", **values)
        await sql("INSERT INTO run_roots VALUES(:id,:root)", id=id, root=parent or id)
        await sql("INSERT INTO run_ancestry VALUES(:id,:id)", id=id)
        if parent:
            await sql("INSERT INTO run_ancestry VALUES(:id,:root)", id=id, root=parent)

    async def message(id=1, run_id=ROOT, role="user", content="Public question", **fields):
        values = {"id": id, "run_id": run_id, "role": role, "content": content, "user_id": ALICE, **fields}
        await sql("INSERT INTO messages(" + ",".join(values) + ") VALUES("
                  + ",".join(":" + key for key in values) + ")", **values)

    async def attachment(raw=b"original bytes", id=ATTACHMENT, message_id=1, **fields):
        values = {"id": id, "message_id": message_id, "owner_id": ALICE, "name": "original.txt",
                  "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "data": raw, **fields}
        await sql("INSERT INTO attachments(" + ",".join(values) + ") VALUES("
                  + ",".join(":" + key for key in values) + ")", **values)

    async def publish(raw=b"# Original output", *, path="output.md", run_id=ROOT, pieces=None, **fields):
        revision = hashlib.sha256(raw).hexdigest()
        pieces = pieces if pieces is not None else [raw] if raw else []
        upload_id = revision[:32]
        values = {"run_id": run_id, "path": path, "revision": revision, "upload_id": upload_id,
                  "size": len(raw), "media_type": "text/markdown", "source_turn": 1, **fields}
        await sql("INSERT INTO published_artifacts(" + ",".join(values) + ") VALUES("
                  + ",".join(":" + key for key in values) + ")", **values)
        for part, chunk in enumerate(pieces):
            await sql("INSERT INTO artifact_parts VALUES(:run,:upload,:part,:data,'',:sha,:size)",
                      run=run_id, upload=upload_id, part=part, data=chunk,
                      sha=hashlib.sha256(chunk).hexdigest(), size=len(chunk))
        return revision

    await run()
    await message()
    app = FastAPI()
    app.include_router(routes(history))
    app.include_router(file_routes(files))
    queries = []
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, many: queries.append(statement))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"x-test-auth": "yes"}) as client:
        yield SimpleNamespace(history=history, files=files, identity=identity, sql=sql, run=run,
                              message=message, attachment=attachment, publish=publish, client=client,
                              queries=queries, engine=engine, writer=writer)
    await engine.dispose()
    await writer.dispose()


async def test_preserves_identity_and_only_public_read_fields(product):
    detail = await product.history.detail(product.identity.person, ROOT)
    session = detail["session"]
    assert session["id"] == ROOT and session["owner_id"] == ALICE
    assert session["url"] == "/#run=" + ROOT
    assert session["execution_engine"] == "legacy:hermes"
    assert session["source_format"] == "moyai-product-v1"
    assert session["capabilities"]["continue"] is False
    assert session["capabilities"]["restore"] is False
    assert session["read_marker_key"] == f"moyai-session-read:{ALICE}:{ROOT}"
    assert session["completion_marker"] == "2026-10-02" and session["unread"] is None
    serialized = json.dumps(detail)
    assert "PRIVATE" not in serialized and "DO-NOT-EXPOSE" not in serialized
    assert detail["turns"] == [] and detail["continuation_supported"] is False
    assert all(q.lstrip().startswith(("SELECT", "WITH")) for q in product.queries)


@pytest.mark.parametrize("role,individual,owner,allowed", [
    ("member", True, ALICE, True), ("admin", True, BOB, False),
    ("member", False, ALICE, False), ("admin", False, ALICE, False),
    ("member", True, "slack:T:U", False),
])
async def test_private_access_never_uses_admin_or_linked_identity(product, role, individual, owner, allowed):
    await product.run(PRIVATE, private=owner, owner=owner, title="Secret")
    person = replace(product.identity.person, role=role, individual=individual)
    product.identity.person = person
    if allowed:
        assert (await product.history.detail(person, PRIVATE))["session"]["visibility"] == "private"
    else:
        with pytest.raises(HTTPException) as exc:
            await product.history.detail(person, PRIVATE)
        assert exc.value.status_code == 404
        assert PRIVATE not in [s["id"] for s in (await product.history.search(person, "Secret"))["sessions"]]


async def test_linked_slack_contribution_navigation_and_personal_metadata(product):
    await product.run(OTHER, owner=BOB, title="Contributed")
    await product.message(2, run_id=OTHER, user_id="slack:T:U")
    await product.sql("INSERT INTO session_folders VALUES('f',:actor,'Folder',4)", actor=ALICE)
    await product.sql("INSERT INTO session_folder_memberships VALUES(:actor,:run,'f')", actor=ALICE, run=OTHER)
    await product.sql("INSERT INTO session_pins VALUES(:actor,:run)", actor=ALICE, run=OTHER)
    result = await product.history.sessions(product.identity.person)
    row = next(s for s in result["sessions"] if s["id"] == OTHER)
    assert row["participated"] and row["pinned"] and row["folder"] == "Folder" and row["folder_id"] == "f"
    assert (await product.history.folders(product.identity.person))["folders"] == [
        {"id": "f", "name": "Folder", "revision": 4},
    ]
    product.identity.person = Person(BOB)
    row = (await product.history.detail(product.identity.person, OTHER))["session"]
    assert not row["pinned"] and row["folder"] == "" and not row["participated"]


async def test_shared_deep_link_broader_than_navigation_and_pin_restores_reachability(product):
    await product.run(OTHER, owner=BOB, title="Unrelated shared")
    person = product.identity.person
    assert (await product.history.detail(person, OTHER))["session"]["id"] == OTHER
    assert (await product.history.search(person, "Unrelated"))["sessions"] == []
    await product.sql("INSERT INTO session_pins VALUES(:actor,:run)", actor=ALICE, run=OTHER)
    assert (await product.history.search(person, "Unrelated"))["sessions"][0]["id"] == OTHER


async def test_archives_personal_deleted_tombstones_no_history(product):
    person = product.identity.person
    await product.sql("INSERT INTO session_archives VALUES(:actor,:run)", actor=ALICE, run=ROOT)
    assert (await product.history.sessions(person))["sessions"] == []
    assert (await product.history.sessions(person, archived=True))["sessions"][0]["archived"]
    await product.sql("UPDATE runs SET deleted_at='2026-10-03' WHERE id=:id", id=ROOT)
    assert (await product.history.sessions(person, deleted=True))["sessions"][0]["deleted"]
    with pytest.raises(HTTPException) as exc:
        await product.history.messages(person, ROOT)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("column,value", [
    ("deleted_at", "2026-10-03"), ("deletion_requested_at", "2026-10-03"),
    ("private_owner_id", BOB),
])
async def test_child_cannot_escape_ancestor_acl_or_deletion(product, column, value):
    await product.run(CHILD, parent=ROOT, title="Child needle")
    await product.message(2, run_id=CHILD, content="child needle")
    await product.sql(f"UPDATE runs SET {column}=:value WHERE id=:id", value=value, id=ROOT)
    with pytest.raises(HTTPException):
        await product.history.messages(product.identity.person, CHILD)
    assert (await product.history.search(product.identity.person, "needle"))["sessions"] == []


async def test_child_results_and_public_message_search_before_limit(product):
    await product.run(CHILD, parent=ROOT, title="Subagent")
    await product.message(2, run_id=CHILD, content="public needle")
    await product.run(PRIVATE, owner=BOB, private=BOB, title="private needle", updated_at="2026-10-09")
    result = await product.history.search(product.identity.person, "needle", limit=1)
    assert [s["id"] for s in result["sessions"]] == [CHILD] and not result["has_more"]
    assert result["sessions"][0]["parent_session_id"] == ROOT
    assert result["sessions"][0]["match"] == {"message_id": 2, "snippet": "public needle"}
    assert (await product.history.children(product.identity.person, ROOT))["sessions"][0]["id"] == CHILD


async def test_search_filters_private_child_and_tool_deleted_content(product):
    await product.run(CHILD, owner=BOB, private=BOB, parent=ROOT, title="Hidden")
    await product.message(2, run_id=CHILD, content="secretonly")
    await product.message(3, role="tool", content="toolonly")
    await product.message(4, status="deleted", content="deletedonly")
    await product.message(5, role="system", content="systemonly")
    for query in ("secretonly", "toolonly", "deletedonly", "systemonly", "PRIVATE-WORKING"):
        assert (await product.history.search(product.identity.person, query))["sessions"] == []
    messages = (await product.history.messages(product.identity.person, ROOT))["messages"]
    assert [m["id"] for m in messages] == [1]


async def test_literal_wildcards_and_sql_input_cannot_broaden_search(product):
    for query in ("%", "_", "' OR 1=1 --", "\\"):
        assert (await product.history.search(product.identity.person, query))["sessions"] == []
    await product.message(2, content="100%_literal")
    assert len((await product.history.search(product.identity.person, "%_"))["sessions"]) == 1


async def test_message_bounds_pagination_and_attachments(product):
    await product.message(2, role="assistant", content="x" * (MAX_MESSAGE_CHARS + 1), response_to_id=1)
    await product.attachment()
    await product.attachment(id="b" * 32, message_id=None)
    page = await product.history.messages(product.identity.person, ROOT, limit=1)
    assert page["has_more"] and page["next_offset"] == 1
    assert [a["id"] for a in page["messages"][0]["attachments"]] == [ATTACHMENT]
    second = await product.history.messages(product.identity.person, ROOT, limit=1, offset=1)
    assert second["messages"][0]["content_truncated"] and not second["has_more"]
    assert len(second["messages"][0]["content"]) == MAX_MESSAGE_CHARS
    assert second["messages"][0]["turn_id"] == "legacy:1"


async def test_rechecks_revocation_and_changed_acl_after_snapshot(product):
    async def revoke(count):
        if count == 2:
            product.identity.revoked = True
    product.identity.on_check = revoke
    with pytest.raises(HTTPException) as exc:
        await product.history.detail(product.identity.person, ROOT)
    assert exc.value.status_code == 401
    product.identity.revoked = False
    product.identity.checks = 0

    async def privatize(count):
        if count == 2:
            await product.sql("UPDATE runs SET private_owner_id=:owner WHERE id=:id", owner=BOB, id=ROOT)
    product.identity.on_check = privatize
    assert (await product.history.sessions(product.identity.person))["sessions"] == []


@pytest.mark.parametrize("path", [
    "/api/product/sessions?limit=101", "/api/product/search?q=x&offset=10001",
    f"/api/product/sessions/{ROOT}/messages?limit=0", "/api/product/search?q=",
    "/api/product/sessions/not-an-old-id",
])
async def test_transport_bounds(product, path):
    response = await product.client.get(path)
    assert response.status_code in {404, 422}


async def test_transport_auth_no_store_and_no_write_routes(product):
    response = await product.client.get("/api/product/sessions")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert (await product.client.get("/api/product/sessions", headers={"x-test-auth": "no"})).status_code == 401
    assert (await product.client.post(f"/api/product/sessions/{ROOT}/messages", json={})).status_code == 405


def test_refuses_create_capable_sqlite_and_invalid_schema(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "never-created.db"))
    with pytest.raises(ValueError, match="mode=ro"):
        ProductHistoryAdapter(Identities(), engine)
    assert not (tmp_path / "never-created.db").exists()
    with pytest.raises(ValueError, match="schema"):
        ProductHistoryAdapter(Identities(), engine, schema='";DROP SCHEMA moyai;--')


async def test_read_only_source_cannot_initialize_missing_database(tmp_path):
    target = tmp_path / "missing.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{target}?mode=ro&uri=true")
    adapter = ProductHistoryAdapter(Identities(), engine)
    with pytest.raises(HTTPException) as exc:
        await adapter.sessions(Person())
    assert exc.value.status_code == 503 and not target.exists()
    await engine.dispose()


async def test_workspace_original_references_and_download_bytes(product):
    await product.attachment()
    revision = await product.publish()
    response = await product.client.get(f"/api/product/sessions/{ROOT}/workspace")
    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["original_url"] == "/api/attachments/" + ATTACHMENT
    output = data["files"][0]
    assert output["source_format"] == "legacy-published-parts-v1"
    assert revision in output["original_url"]
    download = await product.client.get(output["download_url"])
    assert download.content == b"# Original output"
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("kind", ["attachment", "published"])
async def test_file_idor_private_deleted_and_draft_rejected(product, kind):
    await product.run(PRIVATE, owner=BOB, private=BOB)
    await product.message(2, run_id=PRIVATE, user_id=BOB)
    if kind == "attachment":
        await product.attachment(message_id=2)
        params = {"kind": kind, "id": ATTACHMENT}
    else:
        revision = await product.publish(run_id=PRIVATE)
        params = {"kind": kind, "path": "output.md", "revision": revision}
    assert (await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params)).status_code == 404
    assert (await product.client.get(f"/api/product/sessions/{PRIVATE}/files/content", params=params)).status_code == 404
    if kind == "attachment":
        await product.sql("UPDATE attachments SET message_id=NULL WHERE id=:id", id=ATTACHMENT)
        assert (await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params)).status_code == 404


@pytest.mark.parametrize("content_type", ["text/html", "image/svg+xml", "application/xhtml+xml"])
async def test_active_content_is_json_text_or_original_download_never_html(product, content_type):
    raw = b"<script>window.parent.secret()</script>"
    revision = await product.publish(raw, path="active.html", media_type=content_type)
    params = {"kind": "published", "path": "active.html", "revision": revision}
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content",
                                        params=params | {"preview": "true"})
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    assert response.json()["text"] == raw.decode()
    assert response.json()["preview_kind"] == "code" and response.json()["content_url"] is None
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params)
    assert response.content == raw and response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-security-policy"].startswith("sandbox")


async def test_disguised_html_markdown_is_visible_source_not_hidden_markup(product):
    raw = b"<script>secret()</script>"
    revision = await product.publish(raw, path="report.md")
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content",
                                        params={"kind": "published", "path": "report.md",
                                                "revision": revision, "preview": "true"})
    assert response.json()["preview_kind"] == "code"
    assert response.json()["text"] == raw.decode() and response.json()["language"] == "html"


async def test_bounded_preview_and_exact_multipart_ranges(product):
    raw = b"x" * (PREVIEW_BYTES + 100)
    revision = await product.publish(raw, pieces=[raw[:1000], raw[1000:]])
    params = {"kind": "published", "path": "output.md", "revision": revision}
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content",
                                        params=params | {"preview": "true"})
    assert response.json()["truncated"] and len(response.json()["text"]) == PREVIEW_BYTES
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params,
                                        headers={"Range": "bytes=998-1005"})
    assert response.status_code == 206 and response.content == raw[998:1006]
    assert response.headers["content-range"] == f"bytes 998-1005/{len(raw)}"
    for bad in ("bytes=0-1,4-5", "bytes=-0", "bytes=9999999-", "https://example.com"):
        assert (await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params,
                                         headers={"Range": bad})).status_code == 416


async def test_attachment_multipart_and_missing_or_corrupt_parts(product):
    raw = b"abcdef"
    await product.attachment(raw, part_size=3, data=b"")
    for part, piece in enumerate((raw[:3], raw[3:])):
        await product.sql("INSERT INTO attachment_parts VALUES(:id,:part,3,:sha,'',:data)",
                          id=ATTACHMENT, part=part, sha=hashlib.sha256(piece).hexdigest(), data=piece)
    params = {"kind": "attachment", "id": ATTACHMENT}
    url = f"/api/product/sessions/{ROOT}/files/content"
    assert (await product.client.get(url, params=params)).content == raw
    assert (await product.client.get(url, params=params, headers={"Range": "bytes=-2"})).content == b"ef"
    await product.sql("UPDATE attachment_parts SET sha256='invalid' WHERE part=0")
    assert (await product.client.get(url, params=params)).status_code == 503


async def test_real_legacy_object_codec_and_destination_no_native_reinterpretation(product):
    from app.blob_storage import ObjectStorage
    settings = SimpleNamespace(object_storage_bucket="original-bucket",
                               object_storage_endpoint="https://objects.example.test",
                               object_storage_prefix="existing-prefix")
    objects = ObjectStorage(settings)
    raw = b"original raw bytes, not native encrypted bytes"
    checksum = hashlib.sha256(raw).hexdigest()
    calls = []

    class Client:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {"Body": io.BytesIO(raw), "ContentLength": len(raw)}

    objects._client = Client()
    product.files.objects = objects
    await product.attachment(raw, data=b"", data_ref=f"s3:{objects.identity}:{checksum}")
    params = {"kind": "attachment", "id": ATTACHMENT}
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params)
    assert response.content == raw
    assert calls == [{"Bucket": "original-bucket", "Key": "existing-prefix/blobs/" + checksum}]
    await product.sql("UPDATE attachments SET data_ref=:ref", ref=f"s3:{'0' * 32}:{checksum}")
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content", params=params)
    assert response.status_code == 503 and len(calls) == 1
    assert "original-bucket" not in response.text


@pytest.mark.parametrize("change", ["account", "session", "file"])
async def test_revalidation_after_object_io(product, change):
    raw = b"protected"
    await product.attachment(raw, data=b"", data_ref="legacy-test-reference")
    loop = asyncio.get_running_loop()

    class Objects:
        def read(self, reference, limit):
            if change == "account":
                product.identity.revoked = True
            elif change == "session":
                asyncio.run_coroutine_threadsafe(product.sql(
                    "UPDATE runs SET private_owner_id=:id", id=BOB,
                ), loop).result(5)
            else:
                asyncio.run_coroutine_threadsafe(product.sql(
                    "DELETE FROM attachments WHERE id=:id", id=ATTACHMENT,
                ), loop).result(5)
            return raw

    product.files.objects = Objects()
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content",
                                        params={"kind": "attachment", "id": ATTACHMENT})
    assert response.status_code in {401, 404} and raw not in response.content


async def test_zero_byte_published_file_and_wrong_hash(product):
    revision = await product.publish(b"")
    url = f"/api/product/sessions/{ROOT}/files/content"
    params = {"kind": "published", "path": "output.md", "revision": revision}
    response = await product.client.get(url, params=params)
    assert response.status_code == 200 and response.content == b""
    await product.sql("UPDATE published_artifacts SET revision=:revision", revision="f" * 64)
    params["revision"] = "f" * 64
    assert (await product.client.get(url, params=params)).status_code == 503


async def test_preview_external_urls_are_never_fetch_targets(product):
    response = await product.client.get(f"/api/product/sessions/{ROOT}/files/content",
                                        params={"kind": "published", "path": "http://169.254.169.254/",
                                                "revision": "f" * 64})
    assert response.status_code == 404


async def test_actual_artifact_store_local_captures_archive_and_symlinks(product, tmp_path):
    from app.blob_storage import ArtifactStore, ObjectStorage
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    capture_dir = artifact_root / (ROOT + "-captures")
    capture_dir.mkdir()
    raw = b"\x89PNG\r\n\x1a\nsaved-original"
    (capture_dir / "saved.png").write_bytes(raw)
    (capture_dir / "unsafe.png").symlink_to(capture_dir / "saved.png")
    (artifact_root / (ROOT + ".zip")).write_bytes(b"original-archive-bytes")

    @contextmanager
    def connect():
        with sqlite3.connect(f"file:{product.writer.url.database}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    store = SimpleNamespace(connect=connect, objects=ObjectStorage(), schema_updates=False)
    product.files.artifacts = ArtifactStore(store, tmp_path, initialize=False)
    response = await product.client.get(f"/api/product/sessions/{ROOT}/workspace")
    resources = response.json()["files"]
    assert {r["name"] for r in resources} == {"saved.png", "Workspace archive.zip"}
    capture = next(r for r in resources if r["name"] == "saved.png")
    assert (await product.client.get(capture["url"])).content == raw
    archive = next(r for r in resources if r["name"] == "Workspace archive.zip")
    assert archive["original_url"] == f"/api/runs/{ROOT}/artifact"
    assert (await product.client.get(archive["url"])).content == b"original-archive-bytes"
    (capture_dir / "saved.png").write_bytes(b"changed")
    assert (await product.client.get(capture["url"])).status_code == 409


async def test_large_saved_files_are_explicitly_preserved_without_unbounded_reads(product):
    await product.sql(
        "INSERT INTO artifact_objects VALUES(:name,:reference,:size,:sha,'2026-10-01')",
        name=ROOT + ".zip", reference="s3:original", size=33 * 1024 * 1024, sha="f" * 64,
    )
    result = await product.client.get(f"/api/product/sessions/{ROOT}/workspace")
    archive = result.json()["files"][0]
    assert archive["original_url"] == f"/api/runs/{ROOT}/artifact"
    assert archive["compatibility_download_supported"] is False
    assert (await product.client.get(archive["url"])).status_code == 413


def test_import_does_not_start_legacy_runtime():
    root = Path(__file__).parents[2]
    code = (
        "import sys; sys.path[:0]=['.', 'native/src']; "
        "import moyai.product_history, moyai.product_files; "
        "assert not ({'app.main','app.runner','app.session_lifecycle'} & set(sys.modules))"
    )
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, capture_output=True)


def test_unified_router_imports_without_legacy_app_package(tmp_path):
    native_source = Path(__file__).parents[1] / "src"
    code = """
import importlib.abc
import sys
class NoLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'app' or fullname.startswith('app.'):
            raise AssertionError('Standalone native imported legacy product: ' + fullname)
sys.meta_path.insert(0, NoLegacy())
sys.path.insert(0, sys.argv[1])
import moyai.product_routes, moyai.product_history, moyai.product_files
assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-c", code, str(native_source)], cwd=tmp_path,
                   check=True, capture_output=True)


@pytest_asyncio.fixture
async def unified(product, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "native.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security = Security(Fernet.generate_key())
    user = User(id=ALICE, username="alice", display_name="Alice", password="", role="member", auth_version=1)
    async with sessions.begin() as db:
        db.add(user)
        db.add(User(id=BOB, username="bob", display_name="Bob", password="", role="admin"))

    async def current_user(user_id):
        async with sessions() as db:
            current = await db.get(User, user_id)
            if current is None or not current.enabled:
                raise HTTPException(401, "Sign in.")
            return current

    async def add_session(id=ROOT, title="Native conversation", owner=ALICE, visibility="private", **fields):
        async with sessions.begin() as db:
            db.add(NativeSession(id=id, title=title, owner_id=owner, visibility=visibility,
                                 model="native-model", **fields))

    async def add_message(content="Native public answer", id=1, session_id=ROOT, role="assistant"):
        async with sessions.begin() as db:
            db.add(NativeMessage(id=id, session_id=session_id, turn_id="native-turn",
                                 author_id=ALICE, role=role, content=security.seal(content), identity=str(id)))

    await add_session(updated_at="2026-10-04")
    await add_message()
    runtime = SimpleNamespace(
        database=SimpleNamespace(sessions=sessions, user=current_user), security=security,
        product_identity=product.identity, product_history=product.history, product_files=product.files,
    )
    from types import MethodType

    from moyai.database import Database
    runtime.database.security = security
    runtime.database.product_identity = None
    runtime.database.product_authority_enabled = False
    for method in ("allowed_session", "execution_session", "check_source_audience", "same_person"):
        setattr(runtime.database, method, MethodType(getattr(Database, method), runtime.database))

    async def actor():
        return user

    app = FastAPI()
    app.include_router(unified_routes(runtime, actor))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"x-test-auth": "yes"}) as client:
        yield SimpleNamespace(runtime=runtime, service=ProductReadService(runtime), reader=Reader(user, product.identity.person),
                              user=user, sessions=sessions, add_session=add_session, add_message=add_message, client=client,
                              product=product, actor=actor)
    await engine.dispose()


def test_merge_keeps_original_ids_and_source_collisions():
    native = [{"id": ROOT, "title": "Native", "updated_at": "2026-10-02"}]
    legacy = [{"id": ROOT, "title": "Legacy", "updated_at": "2026-10-01"}]
    rows = merge_session_lists(native, legacy)
    assert [(r["key"], r["url"]) for r in rows] == [
        ("native:" + ROOT, "/#" + ROOT), ("legacy:" + ROOT, "/#run=" + ROOT),
    ]
    assert rows[0]["id"] == rows[1]["id"] == ROOT
    assert "source" not in native[0] and "source" not in legacy[0]


async def test_inventory_sources_overlap_and_final_acl_recheck_still_filters(unified, monkeypatch):
    native_started, legacy_started = asyncio.Event(), asyncio.Event()
    native_list = unified.service.native_list
    legacy_list = unified.product.history.sessions

    async def native(*args, **kwargs):
        native_started.set()
        await asyncio.wait_for(legacy_started.wait(), 2)
        return await native_list(*args, **kwargs)

    async def legacy(*args, **kwargs):
        legacy_started.set()
        await asyncio.wait_for(native_started.wait(), 2)
        return await legacy_list(*args, **kwargs)

    monkeypatch.setattr(unified.service, "native_list", native)
    monkeypatch.setattr(unified.product.history, "sessions", legacy)
    result = await unified.service.listing(unified.reader)
    assert {row["source"] for row in result["sessions"]} == {"native", "legacy"}
    # A permission change after one source completes must still be fenced
    # when the merged result is published.
    async def privatized(*args, **kwargs):
        result = await legacy_list(*args, **kwargs)
        await unified.product.sql("UPDATE runs SET private_owner_id=:owner WHERE id=:id", owner=BOB, id=ROOT)
        return result
    monkeypatch.setattr(unified.product.history, "sessions", privatized)
    result = await unified.service.listing(unified.reader)
    assert all(row["source"] != "legacy" for row in result["sessions"])


async def test_unified_source_dispatch_does_not_guess_from_identical_id(unified):
    for source, expected in (("native", "Native conversation"), ("legacy", "Earlier session")):
        result = await unified.client.get(f"/api/history/sessions/{ROOT}", params={"source": source})
        assert result.status_code == 200 and result.json()["session"]["title"] == expected
        result = await unified.client.get(f"/api/history/sessions/{ROOT}/messages", params={"source": source})
        assert result.status_code == 200
        assert result.json()["messages"][0]["content"] == ("Native public answer" if source == "native" else "Public question")
    assert (await unified.client.get(f"/api/history/sessions/{ROOT}")).status_code == 422
    assert (await unified.client.get(f"/api/history/sessions/{ROOT}?source=all")).status_code == 422


async def test_merged_pagination_consumes_only_selected_rows_from_each_source(unified):
    for number, day in ((5, 6), (6, 3), (7, 1)):
        await unified.add_session(id=str(number) * 32, updated_at=f"2026-10-0{day}")
    for number, day in ((8, 7), (9, 5)):
        await unified.product.run(str(number) * 32, updated_at=f"2026-10-0{day}")
    cursor, found = None, []
    for _ in range(5):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        result = (await unified.client.get("/api/history/sessions", params=params)).json()
        found.extend(row["key"] for row in result["sessions"])
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
    assert found == [
        "legacy:" + "8" * 32, "native:" + "5" * 32, "legacy:" + "9" * 32,
        "native:" + ROOT, "native:" + "6" * 32, "legacy:" + ROOT, "native:" + "7" * 32,
    ]
    assert len(found) == len(set(found)) and cursor is None


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_personal_sidebar_filters_before_pagination_and_keeps_shared_access(unified, role):
    unified.user.role = role
    unified.product.identity.person = replace(unified.product.identity.person, role=role)
    joined, pinned, filed, unrelated = ("5" * 32, "6" * 32, "7" * 32, "8" * 32)
    for id in (joined, pinned, filed, unrelated):
        await unified.add_session(id=id, owner=BOB, visibility="shared", updated_at="2026-10-10",
                                  title="Shared " + id[0])
    async with unified.sessions.begin() as db:
        db.add(Turn(session_id=joined, actor_id=ALICE, request_key="contribution",
                    request_fingerprint="contribution", prompt="sealed-input", model="native-model",
                    workflow_id="contribution", status="completed"))
        db.add(SessionPreference(user_id=ALICE, session_id=pinned, pinned=True))
        db.add(SessionPreference(user_id=ALICE, session_id=filed, folder="Research"))
    await unified.product.run("9" * 32, owner=BOB, updated_at="2026-10-12", title="Unrelated history")
    await unified.product.run("a" * 32, owner="slack:T:U", updated_at="2026-10-05", title="My Slack history")
    await unified.product.run("b" * 32, owner=BOB, updated_at="2026-10-06", title="Joined history")
    await unified.product.message(id=10, run_id="b" * 32, user_id="slack:T:U")
    cursor, rows = None, []
    for _ in range(10):
        response = await unified.client.get("/api/history/sessions", params={
            "scope": "mine", "limit": 1, **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200
        page = response.json()
        assert len(page["sessions"]) == 1
        rows.extend(page["sessions"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert cursor is None
    indexed = {row["key"]: row for row in rows}
    assert set(indexed) == {
        "native:" + ROOT, "native:" + joined, "native:" + pinned, "native:" + filed,
        "legacy:" + ROOT, "legacy:" + "a" * 32, "legacy:" + "b" * 32,
    }
    assert indexed["native:" + ROOT]["created_by_me"]
    assert indexed["legacy:" + "a" * 32]["created_by_me"]
    assert not indexed["legacy:" + "a" * 32]["participated"]
    for key in ("native:" + joined, "legacy:" + "b" * 32):
        assert indexed[key]["participated"] and not indexed[key]["created_by_me"]
    assert (await unified.client.get(f"/api/history/sessions/{unrelated}?source=native")).status_code == 200
    search = await unified.client.get("/api/history/search", params={"source": "native", "q": "Shared 8"})
    assert [row["id"] for row in search.json()["sessions"]] == [unrelated]
    # A broad-list cursor cannot silently skip personal entries.
    broad = (await unified.client.get("/api/history/sessions?limit=1")).json()
    assert (await unified.client.get("/api/history/sessions", params={
        "scope": "mine", "cursor": broad["next_cursor"],
    })).status_code == 422


async def test_native_encrypted_search_filters_acl_before_decryption(unified, monkeypatch):
    await unified.add_session(OTHER, owner=BOB, title="Other private", updated_at="2026-10-09")
    await unified.add_message("hidden needle", id=2, session_id=OTHER)
    await unified.add_message("visible needle", id=3)
    await unified.add_message("working needle", id=4, role="tool")
    decrypted = []
    original = unified.runtime.security.unseal

    def record(value):
        raw = original(value)
        decrypted.append(raw)
        return raw

    monkeypatch.setattr(unified.runtime.security, "unseal", record)
    response = await unified.client.get("/api/history/search", params={"q": "needle", "source": "native"})
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["sessions"]] == [ROOT]
    assert response.json()["sessions"][0]["match"]["snippet"] == "visible needle"
    assert b"hidden needle" not in decrypted and b"working needle" not in decrypted


async def test_native_admin_never_bypasses_private_and_nonindividual_cannot_read_private(unified):
    unified.user.role = "admin"
    unified.product.identity.person = replace(unified.product.identity.person, role="admin")
    await unified.add_session(OTHER, owner=BOB, title="Secret admin target")
    result = await unified.client.get("/api/history/search", params={"q": "Secret", "source": "native"})
    assert result.json()["sessions"] == []
    unified.product.identity.person = replace(unified.product.identity.person, individual=False)
    assert (await unified.client.get(f"/api/history/sessions/{ROOT}?source=native")).status_code == 404


async def test_canonical_identity_mismatch_and_native_revocation(unified):
    unified.product.identity.person = Person(BOB)
    assert (await unified.client.get("/api/history/sessions")).status_code == 403
    unified.product.identity.person = Person()
    async with unified.sessions.begin() as db:
        user = await db.get(User, ALICE)
        user.enabled = False
    assert (await unified.client.get("/api/history/sessions")).status_code == 401


async def test_cursor_is_bounded_and_bound_to_query_and_actor(unified):
    page = (await unified.client.get("/api/history/sessions?limit=1")).json()
    assert page["next_cursor"]
    response = await unified.client.get("/api/history/search", params={"q": "different", "cursor": page["next_cursor"]})
    assert response.status_code == 422
    for cursor in ("!", "bnVsbA", "W10", "e30"):
        assert (await unified.client.get("/api/history/sessions", params={"cursor": cursor})).status_code == 422


async def test_partial_source_outage_never_downgrades_auth_error(unified, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise HTTPException(503, "Internal storage details must not escape.")
    monkeypatch.setattr(unified.product.history, "sessions", unavailable)
    response = await unified.client.get("/api/history/sessions")
    assert response.status_code == 200 and response.json()["partial"]
    assert response.json()["sessions"][0]["source"] == "native"
    assert "Internal storage" not in response.text

    async def expired(*args, **kwargs):
        raise HTTPException(401, "Expired authority.")
    monkeypatch.setattr(unified.product.history, "sessions", expired)
    assert (await unified.client.get("/api/history/sessions")).status_code == 401


async def test_final_native_acl_check_after_other_source_read(unified, monkeypatch):
    original = unified.product.history.sessions

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with unified.sessions.begin() as db:
            session = await db.get(NativeSession, ROOT)
            session.owner_id = BOB
        return result

    monkeypatch.setattr(unified.product.history, "sessions", changed)
    response = await unified.client.get("/api/history/sessions")
    assert [r["key"] for r in response.json()["sessions"]] == ["legacy:" + ROOT]


async def test_native_search_budget_is_reported_not_complete(unified, monkeypatch):
    monkeypatch.setattr("moyai.product_routes.SEARCH_ROWS", 1)
    await unified.add_message("newest message", id=2)
    response = await unified.client.get("/api/history/search", params={"source": "native", "q": "missing"})
    assert response.status_code == 200 and response.json()["sessions"] == []
    assert "limited" in response.json()["notice"]


async def test_standalone_search_no_product_adapter_required(unified):
    runtime = SimpleNamespace(database=unified.runtime.database, security=unified.runtime.security)
    app = FastAPI()
    app.include_router(unified_routes(runtime, unified.actor))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.get("/api/history/search?q=answer")).json()
        assert result["sources"] == {"native": "available", "legacy": "disabled"}
        assert result["sessions"][0]["id"] == ROOT and not result["partial"]
        assert (await client.get("/api/history/sessions?source=legacy")).status_code == 503


async def test_original_zip_alias_reuses_real_download_helper_above_preview_bound(unified, tmp_path):
    from app.blob_storage import ArtifactStore, ObjectStorage
    artifact_root = tmp_path / "legacy" / "artifacts"
    artifact_root.mkdir(parents=True)
    archive = artifact_root / (ROOT + ".zip")
    with archive.open("wb") as handle:
        handle.write(b"PK")
        handle.seek(33 * 1024 * 1024 - 1)
        handle.write(b"Z")

    @contextmanager
    def connect():
        with sqlite3.connect(f"file:{unified.product.writer.url.database}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    store = SimpleNamespace(connect=connect, objects=ObjectStorage(), schema_updates=False,
                            path=artifact_root.parent / "workspace.db")
    unified.product.files.artifacts = ArtifactStore(store, artifact_root.parent, initialize=False)
    result = await unified.client.get(f"/api/product/sessions/{ROOT}/files")
    file = result.json()["files"][0]
    assert file["original_download_available"] and file["download_url"].startswith(f"/api/runs/{ROOT}/artifact?")
    response = await unified.client.get(file["download_url"])
    assert response.status_code == 200 and len(response.content) == 33 * 1024 * 1024
    assert response.content[:2] == b"PK" and response.content[-1:] == b"Z"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["cache-control"] == "no-store"


async def test_original_zip_revocation_after_staging_cleans_temporary_copy(unified, tmp_path):
    from fastapi.responses import FileResponse
    staged = tmp_path / "private-staging.zip"

    class Artifacts:
        def info(self, name):
            return {"revision": "f" * 64, "size": 6}

        def download(self, name, filename):
            staged.write_bytes(b"secret")
            unified.product.identity.revoked = True
            return FileResponse(staged, filename=filename)

    unified.product.files.artifacts = Artifacts()
    response = await unified.client.get(f"/api/runs/{ROOT}/artifact")
    assert response.status_code == 401 and b"secret" not in response.content
    assert not staged.exists()
