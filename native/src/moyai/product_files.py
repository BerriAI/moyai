"""Legacy file references and bounded reads, using the original storage codec.

``objects`` must be app.blob_storage.ObjectStorage configured for the original
destination, never native ObjectStore. Optional ``artifacts`` is an existing
ArtifactStore(initialize=False) for local archive/capture compatibility. Neither
helper is constructed here and no URL supplied by a user is fetched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import MISSING
from sqlalchemy import text

from .product_history import (
    MAX_OFFSET,
    MAX_PAGE,
    HistoryAttachment,
    Principal,
    ProductHistoryAdapter,
    ResponseModel,
    absent,
    bounded,
    session_id,
)

PREVIEW_BYTES = 256 * 1024
PREVIEW_LINES = 2000
PART_BYTES = 16 * 1024 * 1024
SAVED_FILE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
CAPTURE_INVENTORY_LIMIT = 10_000
FILE_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'",
}
FileKind = Literal["attachment", "published", "capture", "archive"]


class ProductFile(ResponseModel):
    """A published output, saved capture or workspace archive of an earlier-product session."""
    id: str
    kind: Literal["artifact"]
    name: str
    size: int
    media_type: str
    url: str
    download_url: str
    original_url: str
    source_format: str
    preview_url: str | MISSING = absent()
    # Published outputs only.
    revision: str | MISSING = absent()
    created_at: str | MISSING = absent()
    source_turn: int | None | MISSING = absent()
    # Workspace archive only.
    preview_supported: bool | MISSING = absent()
    original_download_available: bool | MISSING = absent()
    compatibility_download_supported: bool | MISSING = absent()


class ProductFileGroupsMore(ResponseModel):
    files: bool
    sources: bool
    captures: bool


class ProductFileListing(ResponseModel):
    files: list[ProductFile]
    sources: list[HistoryAttachment]
    links: list[Any]
    has_more: ProductFileGroupsMore
    next_offset: int | None
    read_only: bool
    source_format: str
    capture_inventory_truncated: bool
    limitations: list[str]


class ProductFilePreview(ResponseModel):
    """JSON preview returned by the file content route when ``preview=true``."""
    name: str
    media_type: str
    size: int
    id: str
    kind: Literal["upload", "artifact"]
    preview_kind: Literal["unsupported", "code", "markdown", "text"]
    language: str | None
    text: str | None
    truncated: bool
    preview_bytes: int
    text_byte_limit: int
    text_line_limit: int
    download_url: str
    content_url: None
    notice: str
    source_format: str


class LegacyObjects(Protocol):
    def read(self, reference: str, limit: int) -> bytes: ...


class LegacyArtifacts(Protocol):
    def path(self, name: str) -> Path: ...

    def info(self, name: str) -> dict[str, Any] | None: ...

    def read(self, name: str, limit: int, *, revision: str | None = None) -> bytes: ...

    def download(self, name: str, filename: str) -> FileResponse: ...


def file_url(run_id: str, kind: FileKind, **params: Any) -> str:
    return f"/api/product/sessions/{run_id}/files/content?" + urlencode({"kind": kind, **params})


def attachment_view(run_id: str, row: dict[str, Any]) -> dict[str, Any]:
    url = file_url(run_id, "attachment", id=row["id"])
    return {
        "id": row["id"], "kind": "upload", "name": row["name"], "size": row["size"],
        "media_type": row["media_type"], "created_at": row["created_at"],
        "url": url, "download_url": url, "preview_url": url + "&preview=true",
        "original_url": "/api/attachments/" + quote(row["id"], safe=""),
        "source_format": "legacy-attachment-v1", "message_id": row["message_id"],
    }


def published_view(row: dict[str, Any]) -> dict[str, Any]:
    params = {"path": row["path"], "revision": row["revision"]}
    url = file_url(row["run_id"], "published", **params)
    return {
        "id": row["path"] + ":" + row["revision"], "kind": "artifact", "name": row["path"],
        "size": row["size"], "media_type": row["media_type"], "revision": row["revision"],
        "created_at": row["created_at"], "source_turn": row["source_turn"],
        "url": url, "download_url": url, "preview_url": url + "&preview=true",
        "original_url": f"/api/runs/{row['run_id']}/artifacts/content?" + urlencode(params),
        "source_format": "legacy-published-parts-v1",
    }


def byte_range(header: str | None, size: int) -> tuple[int, int, int]:
    if header is None:
        return 0, size, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header) if len(header) <= 100 else None
    if not match or not any(match.groups()):
        raise HTTPException(416, "Invalid byte range.", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    stop = min(size, int(last) + 1) if first and last else size
    if not 0 <= start < stop <= size:
        raise HTTPException(416, "Invalid byte range.", headers={"Content-Range": f"bytes */{size}"})
    return start, stop, 206


class ProductFilesAdapter:
    def __init__(
        self, history: ProductHistoryAdapter, *, objects: LegacyObjects | None = None,
        artifacts: LegacyArtifacts | None = None,
    ) -> None:
        self.history, self.objects, self.artifacts = history, objects, artifacts
        self._object_slots = asyncio.Semaphore(4)

    def local_captures(self, run_id: str) -> tuple[list[dict[str, Any]], bool]:
        if self.artifacts is None:
            return [], False
        rows = []
        try:
            # ArtifactStore.path rejects symlinked roots/directories. Capture
            # names are fixed by the existing capture service, not user paths.
            directory = self.artifacts.path(run_id + "-captures")
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries):
                    if count >= CAPTURE_INVENTORY_LIMIT:
                        return rows, True
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.(png|webm)", entry.name):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    rows.append({
                        "name": run_id + "-captures/" + entry.name, "size": info.st_size,
                        "revision": hashlib.sha256(f"{info.st_mtime_ns}:{info.st_size}".encode()).hexdigest()[:24],
                    })
        except FileNotFoundError:
            pass
        return rows, False

    async def listing(
        self, principal: Principal, run_id: str, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        h, t = self.history, self.history.table
        current = await h.identity.require_current(principal.grant)
        async with h.read() as conn:
            await h.allowed(conn, current, run_id)
            # Each group has a separate bounded cursor, avoiding hidden files when
            # a session contains a large attachment or publication inventory.
            outputs = [dict(r) for r in (await conn.execute(text(f"""
                SELECT p.run_id,p.path,p.revision,p.size,p.media_type,p.created_at,p.source_turn
                FROM {t('published_artifacts')} p WHERE p.run_id=:id AND NOT EXISTS(
                  SELECT 1 FROM {t('published_artifacts')} newer WHERE newer.run_id=p.run_id AND newer.path=p.path
                    AND (newer.created_at>p.created_at OR
                        (newer.created_at=p.created_at AND newer.revision>p.revision)))
                ORDER BY p.path,p.revision LIMIT :limit OFFSET :offset
            """), {"id": run_id, "limit": limit + 1, "offset": offset})).mappings()]
            sources = [dict(r) for r in (await conn.execute(text(f"""
                SELECT a.id,a.message_id,a.name,a.size,a.media_type,a.sha256,a.created_at
                FROM {t('attachments')} a JOIN {t('messages')} m ON m.id=a.message_id
                WHERE m.run_id=:id AND m.role IN ('user','assistant') AND m.status!='deleted'
                  AND a.upload_state='ready' ORDER BY a.created_at,a.id LIMIT :limit OFFSET :offset
            """), {"id": run_id, "limit": limit + 1, "offset": offset})).mappings()]
            captures = [dict(r) for r in (await conn.execute(text(f"""
                SELECT name,size,sha256 AS revision FROM {t('artifact_objects')}
                WHERE substr(name,1,:prefix_length)=:prefix ORDER BY name LIMIT :limit
            """), {"prefix": run_id + "-captures/", "prefix_length": len(run_id) + 10,
                   "limit": CAPTURE_INVENTORY_LIMIT + 1})).mappings()]
            archive = (await conn.execute(text(
                f"SELECT name,size,sha256 AS revision FROM {t('artifact_objects')} WHERE name=:name"
            ), {"name": run_id + ".zip"})).mappings().first()
        # A fixed-name legacy lookup also discovers archives written before object
        # manifests existed; no filesystem walking or construction of Store.
        if archive is None and self.artifacts is not None:
            archive = await asyncio.to_thread(self.artifacts.info, run_id + ".zip")
        local, local_limited = await asyncio.to_thread(self.local_captures, run_id)
        # Durable manifests are authoritative when a stale local copy remains.
        capture_map = {r["name"]: r for r in local}
        capture_map.update({r["name"]: r for r in captures[:CAPTURE_INVENTORY_LIMIT]})
        capture_limited = local_limited or len(captures) > CAPTURE_INVENTORY_LIMIT
        captures = sorted(capture_map.values(), key=lambda row: row["name"])[offset:offset + limit + 1]
        await h.require(current, run_id)
        saved = []
        for row in captures[:limit]:
            name = row["name"].removeprefix(run_id + "-captures/")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.(png|webm)", name):
                continue
            url = file_url(run_id, "capture", path=name, revision=row["revision"])
            saved.append({"id": name, "kind": "artifact", "name": name, "size": row["size"],
                          "media_type": "image/png" if name.endswith(".png") else "video/webm",
                          "url": url, "download_url": url, "preview_url": url + "&preview=true",
                          "source_format": "legacy-artifact-store-v1",
                          "original_url": f"/api/runs/{run_id}/computer/captures/{name}"})
        if archive:
            url = file_url(run_id, "archive", revision=archive["revision"])
            original_url = f"/api/runs/{run_id}/artifact"
            download_url = (
                original_url + "?" + urlencode({"revision": archive["revision"]})
                if self.artifacts is not None else url
            )
            saved.append({"id": run_id + ".zip", "kind": "artifact", "name": "Workspace archive.zip",
                          "size": archive["size"], "media_type": "application/zip", "url": url,
                          "download_url": download_url, "original_url": original_url,
                          "source_format": "legacy-artifact-store-v1",
                          "preview_supported": False,
                          "original_download_available": self.artifacts is not None,
                          "compatibility_download_supported": archive["size"] <= SAVED_FILE_BYTES})
        more = {key: len(rows) > limit for key, rows in
                (("files", outputs), ("sources", sources), ("captures", captures))}
        return {"files": [published_view(r) for r in outputs[:limit]] + saved,
                "sources": [attachment_view(run_id, r) for r in sources[:limit]],
                "links": [], "has_more": more,
                "next_offset": offset + limit if any(more.values()) else None,
                "read_only": True, "source_format": "moyai-product-v1",
                "capture_inventory_truncated": capture_limited,
                "limitations": ["Archive members are available by downloading the original ZIP.",
                               "Individual file parts over 16 MiB require the legacy file service.",
                               *([] if self.artifacts else
                                 ["Local captures and original ZIP downloads require the artifact-directory adapter."])]}

    async def original_archive(
        self, principal: Principal, run_id: str, *, revision: str = "",
    ) -> FileResponse:
        """Preserve /api/runs/{id}/artifact without importing the legacy app.

        ArtifactStore.download verifies the original storage codec into a bounded
        temporary-file copy and owns its cleanup after FileResponse completes.
        """
        session_id(run_id)
        await self.history.require(principal, run_id)
        if self.artifacts is None:
            raise HTTPException(503, "Original archive downloads require the product artifact store.")
        info = await asyncio.to_thread(self.artifacts.info, run_id + ".zip")
        if info is None:
            raise HTTPException(404, "No saved archive is available.")
        if revision and revision != info["revision"]:
            raise HTTPException(409, "Saved files changed. Refresh the file list.")
        await self.history.require(principal, run_id)
        async with self._object_slots:
            task = asyncio.create_task(asyncio.to_thread(
                self.artifacts.download, run_id + ".zip", f"moyai-{run_id[:8]}.zip",
            ))
            try:
                response = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The synchronous copy cannot be cancelled. Drain it before
                # deleting its private temporary output.
                with suppress(Exception):
                    response = await task
                    Path(response.path).unlink(missing_ok=True)
                raise
            try:
                after = await asyncio.to_thread(self.artifacts.info, run_id + ".zip")
                if after is None or after["revision"] != info["revision"]:
                    raise HTTPException(409, "Saved files changed. Refresh the file list.")
                await self.history.require(principal, run_id)
            except BaseException:
                Path(response.path).unlink(missing_ok=True)
                raise
        response.headers.update(FILE_HEADERS)
        return response

    async def manifest(
        self, principal: Principal, run_id: str, kind: FileKind, *,
        id: str = "", path: str = "", revision: str = "",
    ) -> dict[str, Any]:
        h, t = self.history, self.history.table
        current = await h.identity.require_current(principal.grant)
        name = ""
        if len(path) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise HTTPException(422, "Invalid saved file path.")
        async with h.read() as conn:
            await h.allowed(conn, current, run_id)
            if kind == "attachment":
                if not re.fullmatch(r"[a-f0-9]{32}", id):
                    raise HTTPException(404, "File not found.")
                result = await conn.execute(text(f"""
                    SELECT a.id,a.message_id,a.name,a.media_type,a.size,a.sha256,a.part_size,a.data_ref,
                        a.created_at FROM {t('attachments')} a JOIN {t('messages')} m ON m.id=a.message_id
                    WHERE a.id=:id AND m.run_id=:run_id AND m.role IN ('user','assistant')
                        AND m.status!='deleted' AND a.upload_state='ready'
                """), {"id": id, "run_id": run_id})
                row = result.mappings().first()
            elif kind == "published":
                if not re.fullmatch(r"[a-f0-9]{64}", revision):
                    raise HTTPException(422, "Choose an exact saved artifact revision.")
                result = await conn.execute(text(f"""
                    SELECT run_id,path,revision,upload_id,size,media_type,created_at,source_turn
                    FROM {t('published_artifacts')} WHERE run_id=:id AND path=:path AND revision=:revision
                """), {"id": run_id, "path": path, "revision": revision})
                row = result.mappings().first()
            else:
                if kind == "capture" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.(png|webm)", path):
                    raise HTTPException(404, "File not found.")
                name = run_id + ".zip" if kind == "archive" else run_id + "-captures/" + path
                row = (await conn.execute(text(f"""
                    SELECT name,size,sha256,sha256 AS revision,reference FROM {t('artifact_objects')}
                    WHERE name=:name
                """), {"name": name})).mappings().first()
        if row is None and kind in {"capture", "archive"} and self.artifacts is not None:
            row = await asyncio.to_thread(self.artifacts.info, name)
        if row is None:
            raise HTTPException(404, "File not found.")
        item = dict(row)
        if kind in {"capture", "archive"}:
            if not revision or item["revision"] != revision:
                raise HTTPException(409, "Saved files changed. Refresh the file list.")
            item["media_type"] = ("application/zip" if kind == "archive" else
                                  "image/png" if path.endswith(".png") else "video/webm")
        if not isinstance(item["size"], int) or item["size"] < 0:
            raise HTTPException(503, "Saved file metadata is invalid.")
        await h.require(current, run_id)
        return item | {"kind": kind}

    async def revalidate_item(self, principal: Principal, run_id: str, item: dict[str, Any]) -> None:
        kind = item["kind"]
        current = await self.manifest(
            principal, run_id, kind, id=item.get("id", ""),
            path=item.get("path", "") if kind == "published" else
            item.get("name", "").removeprefix(run_id + "-captures/"),
            revision=item.get("revision", ""),
        )
        if current != item:
            raise HTTPException(409, "Saved file changed. Refresh the file list.")

    async def raw_part(self, row: dict[str, Any]) -> bytes:
        size = row["size"]
        if not isinstance(size, int) or not 0 <= size <= PART_BYTES:
            raise HTTPException(413, "This saved part requires the original legacy download service.")
        if row.get("reference"):
            if self.objects is None:
                raise HTTPException(503, "This file requires its original product object storage configuration.")
            async with self._object_slots:
                raw = await asyncio.to_thread(self.objects.read, row["reference"], size)
        else:
            raw = bytes(row["data"])
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise HTTPException(503, "Saved file bytes failed integrity verification.")
        return raw

    async def chunks(
        self, principal: Principal, run_id: str, item: dict[str, Any], start: int, stop: int,
    ) -> AsyncGenerator[bytes, None]:
        h, t = self.history, self.history.table
        kind, size = item["kind"], item["size"]
        full = start == 0 and stop == size
        checksum = hashlib.sha256()
        if kind in {"capture", "archive"}:
            if size > SAVED_FILE_BYTES:
                raise HTTPException(413, "This archive requires the original legacy download service.")
            if self.artifacts is not None:
                async with self._object_slots:
                    raw = await asyncio.to_thread(self.artifacts.read, item["name"],
                                                  SAVED_FILE_BYTES, revision=item["revision"])
            elif item.get("reference"):
                if self.objects is None:
                    raise HTTPException(503, "This file requires its original product object storage configuration.")
                async with self._object_slots:
                    raw = await asyncio.to_thread(self.objects.read, item["reference"], size)
            else:
                raise HTTPException(503, "This file requires the original product artifact directory.")
            if len(raw) != size or (item.get("sha256") and hashlib.sha256(raw).hexdigest() != item["sha256"]):
                raise HTTPException(503, "Saved file bytes failed integrity verification.")
            await self.revalidate_item(principal, run_id, item)
            for position in range(start, stop, CHUNK_BYTES):
                await self.revalidate_item(principal, run_id, item)
                yield raw[position:min(stop, position + CHUNK_BYTES)]
            return
        position, part = 0, 0
        if kind == "published" and start:
            # Locate a range in one query, rather than fetching every preceding
            # part (and borrowing an authorization slot for each skipped part).
            async with h.read() as conn:
                row = (await conn.execute(text(f"""
                    WITH parts AS (
                        SELECT part,size,COALESCE(SUM(size) OVER (ORDER BY part
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) AS position
                        FROM {t('artifact_parts')} WHERE run_id=:run_id AND upload_id=:upload_id
                    ) SELECT part,position FROM parts WHERE position<=:start AND position+size>:start
                    ORDER BY part LIMIT 1
                """), {"run_id": run_id, "upload_id": item["upload_id"], "start": start})).mappings().first()
            if row is None:
                raise HTTPException(503, "Saved file parts are incomplete.")
            position, part = int(row["position"]), int(row["part"])
        elif kind == "attachment" and item["part_size"]:
            if not 0 < item["part_size"] <= PART_BYTES:
                raise HTTPException(413, "This attachment requires the original legacy download service.")
            part = start // item["part_size"]
            position = part * item["part_size"]
        while position < stop:
            await self.revalidate_item(principal, run_id, item)
            async with h.read() as conn:
                if kind == "published":
                    query = f"""SELECT part,size,sha256,reference,
                        CASE WHEN size<=:cap THEN data ELSE NULL END AS data FROM {t('artifact_parts')}
                        WHERE run_id=:run_id AND upload_id=:upload_id AND part=:part"""
                    params = {"run_id": run_id, "upload_id": item["upload_id"], "part": part, "cap": PART_BYTES}
                elif item["part_size"]:
                    query = f"""SELECT part,size,sha256,reference,
                        CASE WHEN size<=:cap THEN data ELSE NULL END AS data FROM {t('attachment_parts')}
                        WHERE attachment_id=:id AND part=:part"""
                    params = {"id": item["id"], "part": part, "cap": PART_BYTES}
                else:
                    query = f"""SELECT size,sha256,data_ref AS reference,
                        CASE WHEN size<=:cap THEN data ELSE NULL END AS data FROM {t('attachments')} WHERE id=:id"""
                    params = {"id": item["id"], "cap": PART_BYTES}
                row = (await conn.execute(text(query), params)).mappings().first()
            if row is None or row["size"] <= 0 or position + row["size"] > size:
                raise HTTPException(503, "Saved file parts are incomplete.")
            end = position + row["size"]
            if end > start:
                raw = await self.raw_part(dict(row))
                await self.revalidate_item(principal, run_id, item)
                if full:
                    checksum.update(raw)
                    if end == size and checksum.hexdigest() != (item["revision"] if kind == "published" else item["sha256"]):
                        raise HTTPException(503, "Saved file failed integrity verification.")
                for offset in range(max(0, start - position), min(len(raw), stop - position), CHUNK_BYTES):
                    await self.revalidate_item(principal, run_id, item)
                    yield raw[offset:min(len(raw), stop - position, offset + CHUNK_BYTES)]
            position, part = end, part + 1
        if full and size == 0 and hashlib.sha256(b"").hexdigest() != (
            item["revision"] if kind == "published" else item["sha256"]
        ):
            raise HTTPException(503, "Saved file failed integrity verification.")

    async def response(
        self, principal: Principal, run_id: str, kind: FileKind, *, id: str = "",
        path: str = "", revision: str = "", preview: bool = False, range_header: str | None = None,
    ) -> Response:
        session_id(run_id)
        item = await self.manifest(principal, run_id, kind, id=id, path=path, revision=revision)
        name = item.get("path") or item.get("name") or "Saved file"
        if preview:
            if kind == "archive":
                raise HTTPException(415, "Download the archive to view its contents.")
            raw = b"".join([chunk async for chunk in self.chunks(
                principal, run_id, item, 0, min(item["size"], PREVIEW_BYTES),
            )])
            await self.revalidate_item(principal, run_id, item)
            # Always JSON text, including HTML/SVG; never execute active documents.
            try:
                content = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                content = None
                if item["size"] > len(raw):
                    for tail in range(1, 4):
                        try:
                            content = raw[:-tail].decode("utf-8", errors="strict")
                            break
                        except UnicodeDecodeError:
                            pass
            if content is not None and any(ord(c) < 32 and c not in "\t\r\n" for c in content):
                content = None
            from .file_preview import LANGUAGES
            language = LANGUAGES.get(PurePosixPath(name).suffix.lower()) if content is not None else None
            if content is not None:
                prefix = content.lstrip()[:2048]
                if re.match(r"(?is)<!doctype\s+html\b|<html\b|<script\b", prefix):
                    language = "html"
                elif re.match(r"(?is)(?:<\?xml[^>]*>\s*)?<svg\b|<\?xml\b", prefix):
                    language = "xml"
            lines = content.splitlines(keepends=True) if content is not None else []
            line_truncated = len(lines) > PREVIEW_LINES
            if line_truncated:
                content = "".join(lines[:PREVIEW_LINES])
            preview_kind = (
                "unsupported" if content is None else "code" if language else
                "markdown" if PurePosixPath(name).suffix.lower() in {".md", ".markdown"} else "text"
            )
            return JSONResponse(ProductFilePreview(
                name=name, media_type=item["media_type"], size=item["size"],
                id=id or path or run_id + ".zip", kind="upload" if kind == "attachment" else "artifact",
                preview_kind=preview_kind, language=language,
                text=content, truncated=item["size"] > len(raw) or line_truncated, preview_bytes=len(raw),
                text_byte_limit=PREVIEW_BYTES, text_line_limit=PREVIEW_LINES,
                download_url=file_url(run_id, kind, id=id, path=path, revision=revision), content_url=None,
                notice="Historical file preview. Active documents remain source text; media is available by download.",
                source_format="moyai-product-v1",
            ).model_dump(mode="json"), headers=FILE_HEADERS)
        start, stop, status = byte_range(range_header, item["size"])
        stream = self.chunks(principal, run_id, item, start, stop)
        # Validate storage/auth failures before sending response headers.
        try:
            first = await anext(stream, b"")
            await self.revalidate_item(principal, run_id, item)
        except BaseException:
            await stream.aclose()
            raise

        async def body() -> AsyncIterator[bytes]:
            try:
                await self.revalidate_item(principal, run_id, item)
                if first:
                    yield first
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()

        headers = FILE_HEADERS | {
            "Accept-Ranges": "bytes", "Content-Length": str(stop - start),
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(PurePosixPath(name).name, safe=""),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{stop - 1}/{item['size']}"
        # Original bytes stay downloads; metadata carries their original MIME.
        return StreamingResponse(body(), status_code=status, media_type="application/octet-stream", headers=headers)


def routes(adapter: ProductFilesAdapter) -> APIRouter:
    router = APIRouter(prefix="/api/product")

    @router.get("/sessions/{run_id}/workspace", operation_id="listProductSessionWorkspace",
                response_model=ProductFileListing)
    @router.get("/sessions/{run_id}/files", operation_id="listProductSessionFiles",
                response_model=ProductFileListing)
    async def listing(
        run_id: str, request: Request, response: Response, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ):
        response.headers["Cache-Control"] = "no-store"
        return await adapter.listing(await adapter.history.identity.authenticate(request), run_id,
                                     limit=limit, offset=offset)

    @router.get("/sessions/{run_id}/files/content", operation_id="getProductSessionFileContent", responses={
        200: {"model": ProductFilePreview,
              "description": "A JSON preview when preview=true; otherwise the original bytes as a download."},
    })
    async def content(
        run_id: str, request: Request, kind: FileKind, id: str = Query("", max_length=100),
        path: str = Query("", max_length=2048), revision: str = Query("", max_length=64), preview: bool = False,
    ):
        return await adapter.response(
            await adapter.history.identity.authenticate(request), run_id, kind,
            id=id, path=path, revision=revision, preview=preview, range_header=request.headers.get("range"),
        )

    return router
