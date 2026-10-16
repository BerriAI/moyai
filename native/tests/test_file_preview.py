"""Supplementary file-preview HTTP tests; actual deployed-app proof is separate."""

from __future__ import annotations

import asyncio
import hashlib
import io
import wave
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from test_application_boundaries import sign_in

from moyai import file_preview
from moyai.contracts import NewMessage, NewSession
from moyai.file_preview import MAX_TEXT_BYTES, MAX_TEXT_LINES, routes
from moyai.models import Artifact, Session, Upload, User, new_id


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), (30, 60, 90)).save(stream, format="PNG")
    return stream.getvalue()


def wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 16)
    return stream.getvalue()


async def member(runtime):
    async with runtime.database.sessions.begin() as db:
        user = User(id=new_id(), username=new_id(), display_name="Member", password="unused")
        db.add(user)
    return user


async def saved_file(
    runtime, actor, raw=b"Preview content.", *, name="report.txt", media_type="text/plain",
    kind="artifact", session=None, size=None,
):
    session = session or await runtime.database.create_session(actor, NewSession(visibility="private"))
    digest = await runtime.objects.put(raw)
    fields = {
        "id": new_id(), "session_id": session.id, "name": name, "media_type": media_type,
        "digest": digest, "size": len(raw) if size is None else size,
    }
    if kind == "artifact":
        turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Save file.", request_key=new_id()))
        row = Artifact(**fields, turn_id=turn.id, content_hash=hashlib.sha256(raw).hexdigest())
    else:
        row = Upload(**fields, owner_id=actor.id)
    async with runtime.database.sessions.begin() as db:
        db.add(row)
    return session, row, f"/api/sessions/{session.id}/file-preview/{kind}/{row.id}"


@asynccontextmanager
async def client_for(runtime, user=None):
    app = FastAPI()

    async def actor(request: Request):
        identity = runtime.security.read_session(request.cookies.get("preview_session", ""))
        if not identity:
            raise HTTPException(401, "Sign in to continue.")
        current = await runtime.database.user(identity["user"])
        if current.auth_version != identity["version"]:
            raise HTTPException(401, "Your session expired.")
        return current

    app.include_router(routes(runtime, actor))
    cookies = {}
    if user:
        cookies["preview_session"] = runtime.security.issue_session(user.id, user.auth_version)[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=cookies,
    ) as client:
        yield client


@pytest.mark.parametrize(("name", "raw", "kind", "media_type", "language"), [
    ("report.txt", b"Hello world.", "text", "text/plain", None),
    ("report.md", b"# Report\n\n**Result**", "markdown", "text/markdown", None),
    ("module.tsx", b"export const hello = 1;", "code", "text/plain", "tsx"),
    ("record.json", b'{"ok": true}', "code", "application/json", "json"),
    ("empty.txt", b"", "text", "text/plain", None),
    ("notes.txt", b"ID3 is an audio metadata format.", "text", "text/plain", None),
    ("index.html", b"<script>window.pwned = true</script>", "code", "text/html", "html"),
    ("image.svg", b'<svg onload="alert(1)"></svg>', "code", "image/svg+xml", "xml"),
    ("pretend.png", b"<!doctype html><script>alert(1)</script>", "code", "text/html", "html"),
])
async def test_text_metadata_and_active_source_are_inert(runtime, actor, name, raw, kind, media_type, language):
    _session, row, path = await saved_file(runtime, actor, raw, name=name, media_type="image/png")
    async with client_for(runtime, actor) as client:
        response = await client.get(path)
        assert response.status_code == 200
        result = response.json()
        assert result["preview_kind"] == kind and result["media_type"] == media_type
        assert result["text"] == raw.decode() and result["language"] == language
        assert result["content_url"] is None and result["download_url"] == f"/api/files/{row.id}"
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(path + "/content")).status_code == 415
        download = await client.get(path + "/download")
        assert download.content == raw
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert not result["truncated"]


@pytest.mark.parametrize("raw", [
    ("x" * (MAX_TEXT_BYTES - 1) + "🙂" * 8).encode(),
    b"line\n" * (MAX_TEXT_LINES + 10),
])
async def test_text_preview_byte_and_line_limits_preserve_original(runtime, actor, raw):
    _session, row, path = await saved_file(runtime, actor, raw)
    async with client_for(runtime, actor) as client:
        result = (await client.get(path)).json()
        assert result["truncated"]
        assert len(result["text"].encode()) <= MAX_TEXT_BYTES
        assert len(result["text"].splitlines()) <= MAX_TEXT_LINES
        assert "\ufffd" not in result["text"]
        assert result["size"] == len(raw)
        assert (await client.get(path + "/download")).content == raw
    # Existing download route is unchanged, including original bytes/type.
    async with sign_in(runtime) as client:
        original = await client.get(f"/api/files/{row.id}")
        assert original.content == raw and original.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("kind", ["artifact", "upload"])
async def test_raster_bytes_determine_mime_not_filename_or_stored_type(runtime, actor, kind):
    raw = png_bytes()
    _session, _row, path = await saved_file(
        runtime, actor, raw, kind=kind, name="misnamed.html", media_type="text/html",
    )
    async with client_for(runtime, actor) as client:
        metadata = (await client.get(path)).json()
        assert metadata["preview_kind"] == "image" and metadata["media_type"] == "image/png"
        assert metadata["content_url"] == path + "/content"
        response = await client.get(metadata["content_url"])
        assert response.content == raw and response.headers["content-type"] == "image/png"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("inline;")
        download = await client.get(path + "/download")
        assert download.content == raw and download.headers["content-type"] == "application/octet-stream"
        if kind == "upload":
            assert metadata["download_url"] == path + "/download"


@pytest.mark.parametrize(("raw", "kind", "media"), [
    (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42", "video", "video/mp4"),
    (b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom", "audio", "audio/mp4"),
    (b"\x1aE\xdf\xa3\x42\x82\x84webm", "video", "video/webm"),
    (b"ID3\x04\x00\x00\x00\x00\x00\x00", "audio", "audio/mpeg"),
    (b"OggS\x00\x00OpusHead", "audio", "audio/ogg"),
])
async def test_supported_container_signatures(runtime, actor, raw, kind, media):
    # These are format-envelope tests, not browser playback/E2E fixtures.
    _session, _row, path = await saved_file(runtime, actor, raw, name="unknown.bin", media_type="text/html")
    async with client_for(runtime, actor) as client:
        data = (await client.get(path)).json()
        assert data["preview_kind"] == kind and data["media_type"] == media
        response = await client.get(path + "/content")
        assert response.content == raw and response.headers["content-type"] == media


@pytest.mark.parametrize(("header", "expected", "status"), [
    (None, None, 200), ("bytes=0-3", (0, 3), 206), ("bytes=4-", (4, 75), 206),
    ("bytes=-5", (71, 75), 206), ("bytes=99999-", None, 416),
    ("bytes=0-1,3-4", None, 416), ("bytes=-0", None, 416), ("bytes=4-2", None, 416),
    ("bytes=" + "9" * 100 + "-", None, 416),
])
async def test_audio_byte_ranges_are_bounded(runtime, actor, header, expected, status):
    raw = wav_bytes()
    assert len(raw) == 76
    _session, _row, path = await saved_file(runtime, actor, raw, name="sound.wav")
    async with client_for(runtime, actor) as client:
        response = await client.get(path + "/content", headers={"Range": header} if header else {})
        assert response.status_code == status
        if status == 416:
            assert response.headers["content-range"] == "bytes */76"
        else:
            assert response.headers["content-type"] == "audio/wav"
            assert response.headers["accept-ranges"] == "bytes"
            assert response.content == (raw[expected[0]:expected[1] + 1] if expected else raw)
            if expected:
                assert response.headers["content-range"] == f"bytes {expected[0]}-{expected[1]}/76"


@pytest.mark.parametrize(("raw", "kind", "media"), [
    (b"%PDF-1.7\n%%EOF", "pdf", "application/pdf"),
    (b"\x00binary\xff", "unsupported", "application/octet-stream"),
    ("Hello".encode("utf-16"), "unsupported", "application/octet-stream"),
    (b"\x89PNG\r\n\x1a\nnot-an-image", "unsupported", "application/octet-stream"),
])
async def test_unsupported_or_active_documents_never_get_inline_content(runtime, actor, raw, kind, media):
    _session, _row, path = await saved_file(runtime, actor, raw)
    async with client_for(runtime, actor) as client:
        result = (await client.get(path)).json()
        assert result["preview_kind"] == kind and result["media_type"] == media
        assert result["text"] is None and result["content_url"] is None and result["notice"]
        assert (await client.get(path + "/content")).status_code == 415
        assert (await client.get(path + "/download")).content == raw


async def test_size_metadata_rejects_large_objects_before_io(runtime, actor, monkeypatch):
    _session, _row, path = await saved_file(runtime, actor, size=file_preview.MAX_ARTIFACT_BYTES + 1)

    async def unexpected(_digest):
        raise AssertionError("Oversized metadata must not read object bytes")

    monkeypatch.setattr(runtime.objects, "get", unexpected)
    async with client_for(runtime, actor) as client:
        assert (await client.get(path)).status_code == 413


@pytest.mark.parametrize(("claimed", "actual", "status"), [(64, 65, 413), (12, 13, 409)])
async def test_actual_bytes_are_checked_against_limit_and_immutable_metadata(runtime, actor, monkeypatch, claimed, actual, status):
    monkeypatch.setattr(file_preview, "MAX_ARTIFACT_BYTES", 64)
    _session, _row, path = await saved_file(runtime, actor, b"x" * actual, size=claimed)
    async with client_for(runtime, actor) as client:
        response = await client.get(path)
        assert response.status_code == status and "x" * 10 not in response.text


async def test_authentication_idor_deleted_sessions_and_external_kinds_fail_before_io(runtime, actor, monkeypatch):
    session, row, path = await saved_file(runtime, actor)
    requester = await member(runtime)
    own = await runtime.database.create_session(requester, NewSession())

    async def unexpected(_digest):
        raise AssertionError("An unauthorized request read object bytes")

    monkeypatch.setattr(runtime.objects, "get", unexpected)
    async with client_for(runtime) as anonymous, client_for(runtime, requester) as other:
        assert (await anonymous.get(path)).status_code == 401
        for suffix in ("", "/content", "/download"):
            assert (await other.get(path + suffix)).status_code == 404
            wrong_session = f"/api/sessions/{own.id}/file-preview/artifact/{row.id}{suffix}"
            assert (await other.get(wrong_session)).status_code == 404
    async with client_for(runtime, actor) as owner:
        assert (await owner.get(f"/api/sessions/{session.id}/file-preview/link/https:example.invalid")).status_code == 422
        async with runtime.database.sessions.begin() as db:
            (await db.get(Session, session.id)).deleted = True
        assert (await owner.get(path)).status_code == 404


async def test_upload_must_be_attached_to_exact_session_even_for_owner(runtime, actor, monkeypatch):
    session, row, path = await saved_file(runtime, actor, png_bytes(), kind="upload")
    another = await runtime.database.create_session(actor, NewSession())

    async def unexpected(_digest):
        raise AssertionError("Unattached or differently scoped upload must not be loaded")

    monkeypatch.setattr(runtime.objects, "get", unexpected)
    async with client_for(runtime, actor) as client:
        assert (await client.get(path.replace(session.id, another.id))).status_code == 404
        async with runtime.database.sessions.begin() as db:
            (await db.get(Upload, row.id)).session_id = None
        assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize("kind", ["artifact", "upload"])
@pytest.mark.parametrize("suffix", ["", "/content", "/download"])
@pytest.mark.parametrize("revocation", ["deleted", "private", "disabled", "rotated"])
async def test_authority_is_rechecked_after_object_io(runtime, actor, monkeypatch, kind, suffix, revocation):
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    session, _row, path = await saved_file(runtime, actor, png_bytes(), kind=kind, session=shared)
    requester = await member(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.objects.get

    async def delayed(digest):
        raw = await original(digest)
        entered.set()
        await release.wait()
        return raw

    monkeypatch.setattr(runtime.objects, "get", delayed)
    async with client_for(runtime, requester) as client:
        request = asyncio.create_task(client.get(path + suffix))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with runtime.database.sessions.begin() as db:
                if revocation in {"disabled", "rotated"}:
                    user = await db.get(User, requester.id)
                    if revocation == "disabled":
                        user.enabled = False
                    else:
                        user.auth_version += 1
                else:
                    saved = await db.get(Session, session.id)
                    if revocation == "deleted":
                        saved.deleted = True
                    else:
                        saved.visibility = "private"
            release.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code == (401 if revocation in {"disabled", "rotated"} else 404)
            assert png_bytes() not in response.content and "report.txt" not in response.text
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("change", ["removed", "rebound", "metadata"])
async def test_file_identity_is_rechecked_after_object_io(runtime, actor, monkeypatch, change):
    _session, row, path = await saved_file(runtime, actor, png_bytes(), kind="upload")
    another = await runtime.database.create_session(actor, NewSession())
    original = runtime.objects.get

    async def modified(digest):
        raw = await original(digest)
        async with runtime.database.sessions.begin() as db:
            upload = await db.get(Upload, row.id)
            if change == "removed":
                await db.delete(upload)
            elif change == "rebound":
                upload.session_id = another.id
            else:
                upload.name = "changed.png"
        return raw

    monkeypatch.setattr(runtime.objects, "get", modified)
    async with client_for(runtime, actor) as client:
        response = await client.get(path)
        assert response.status_code == (409 if change == "metadata" else 404)


async def test_download_filename_cannot_inject_headers(runtime, actor):
    raw = b"unaltered\r\nbytes\x00"
    _session, _row, path = await saved_file(runtime, actor, raw, name='nested/naïve"\r\nname.bin')
    async with client_for(runtime, actor) as client:
        response = await client.get(path + "/download")
        assert response.content == raw
        header = response.headers["content-disposition"]
        assert "nested" not in header and "\r" not in header and "\n" not in header
        assert "%0D%0A" in header and "%22" in header and "%C3%AF" in header


@pytest.mark.parametrize("failure", ["oversized", "missing"])
async def test_account_is_rechecked_even_when_object_read_is_unsuccessful(runtime, actor, monkeypatch, failure):
    monkeypatch.setattr(file_preview, "MAX_ARTIFACT_BYTES", 64)
    _session, _row, path = await saved_file(runtime, actor)

    async def revoked(_digest):
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, actor.id)).enabled = False
        if failure == "missing":
            raise FileNotFoundError
        return b"x" * 65

    monkeypatch.setattr(runtime.objects, "get", revoked)
    async with client_for(runtime, actor) as client:
        assert (await client.get(path)).status_code == 401
