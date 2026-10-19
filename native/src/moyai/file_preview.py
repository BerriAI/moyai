"""Bounded presentation of immutable session files; never fetch arbitrary URLs.

Only raster images and recognized audio/video containers have inline byte URLs.
Text, Markdown, HTML, XML and SVG are returned as JSON strings, never documents.
The original /api/files/{id} attachment route remains unchanged.
"""

from __future__ import annotations

import asyncio
import codecs
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from botocore.exceptions import ClientError
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from .artifacts import MAX_ARTIFACT_BYTES
from .images import MAX_UPLOAD_BYTES, image_type
from .models import Artifact, Upload, User

if TYPE_CHECKING:
    from .runtime import Runtime

ResourceKind = Literal["artifact", "upload"]
PreviewKind = Literal["text", "markdown", "code", "image", "video", "audio", "pdf", "unsupported"]
MAX_TEXT_BYTES = 128 * 1024
MAX_TEXT_LINES = 2000
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".json": "json", ".jsonl": "json", ".css": "css", ".scss": "scss",
    ".html": "html", ".htm": "html", ".svg": "xml", ".xml": "xml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".conf": "ini",
    ".go": "go", ".rs": "rust", ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin", ".diff": "diff",
}
TEXT_MEDIA = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".json": "application/json",
    ".jsonl": "application/x-ndjson", ".css": "text/css", ".html": "text/html", ".htm": "text/html",
    ".svg": "image/svg+xml", ".xml": "application/xml", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".js": "text/javascript", ".jsx": "text/javascript", ".py": "text/x-python",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".toml": "application/toml",
}


@dataclass(frozen=True)
class FileRecord:
    id: str
    kind: ResourceKind
    session_id: str
    name: str
    media_type: str
    size: int
    digest: str

    @property
    def path(self) -> str:
        return f"/api/sessions/{quote(self.session_id, safe='')}/file-preview/{self.kind}/{quote(self.id, safe='')}"

    @property
    def download_url(self) -> str:
        return f"/api/files/{quote(self.id, safe='')}" if self.kind == "artifact" else self.path + "/download"


class SessionFilePreview(BaseModel):
    """Metadata and bounded text of a native session file preview."""
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: str
    kind: ResourceKind
    name: str
    size: int
    media_type: str
    preview_kind: PreviewKind
    text: str | None
    language: str | None
    truncated: bool
    notice: str | None
    text_byte_limit: int
    text_line_limit: int
    download_url: str
    content_url: str | None


@dataclass(frozen=True)
class Preview:
    kind: PreviewKind
    media_type: str
    text: str | None = None
    language: str | None = None
    truncated: bool = False
    notice: str | None = None

    @property
    def inline(self) -> bool:
        return self.kind in {"image", "video", "audio"}


def classify(raw: bytes, name: str, declared_type: str) -> Preview:
    """Verify raster data and sniff media envelopes; extension alone never enables playback."""
    if raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    ):
        try:
            return Preview("image", image_type(raw))
        except ValueError:
            return Preview("unsupported", "application/octet-stream", notice="This image could not be safely decoded.")
    if raw.startswith(b"%PDF-"):
        return Preview(
            "pdf", "application/pdf",
            notice="PDF document. Inline PDF rendering is unavailable in this workspace. Download the original to view its pages.",
        )
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in {b"M4A ", b"M4B "}:
            return Preview("audio", "audio/mp4")
        if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}:
            return Preview("video", "video/mp4")
    if raw.startswith(b"\x1aE\xdf\xa3") and b"webm" in raw[:4096].lower():
        return Preview("audio", "audio/webm") if declared_type == "audio/webm" else Preview("video", "video/webm")
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return Preview("audio", "audio/wav")
    if raw.startswith(b"OggS"):
        if b"OpusHead" in raw[:8192] or b"vorbis" in raw[:8192]:
            return Preview("audio", "audio/ogg")
        if b"theora" in raw[:8192]:
            return Preview("video", "video/ogg")
        return Preview("unsupported", "application/ogg", notice="This Ogg container is not supported by the preview.")
    id3 = (
        len(raw) >= 10 and raw.startswith(b"ID3") and raw[3] in {2, 3, 4}
        and all(value < 128 for value in raw[6:10])
    )
    if id3 or (
        len(raw) >= 4 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0
        and raw[1] & 0x06 == 0x02 and raw[1] & 0x18 != 0x08
        and raw[2] & 0xF0 not in {0, 0xF0} and raw[2] & 0x0C != 0x0C
    ):
        return Preview("audio", "audio/mpeg")
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
        content = decoder.decode(raw[:MAX_TEXT_BYTES], final=len(raw) <= MAX_TEXT_BYTES)
    except UnicodeDecodeError:
        return Preview("unsupported", "application/octet-stream", notice="This binary or non-UTF-8 file has no inline preview.")
    if any(ord(character) < 32 and character not in "\t\r\n" or ord(character) == 127 for character in content):
        return Preview("unsupported", "application/octet-stream", notice="This file contains binary data and has no text preview.")
    lines = content.splitlines(keepends=True)
    truncated = len(raw) > MAX_TEXT_BYTES or len(lines) > MAX_TEXT_LINES
    content = "".join(lines[:MAX_TEXT_LINES])
    extension = PurePosixPath(name).suffix.lower()
    language = LANGUAGES.get(extension)
    media_type = TEXT_MEDIA.get(extension, "text/plain")
    # Active source formats remain inert even when their extension/type is misleading.
    prefix = content.lstrip()[:2048]
    if re.match(r"(?is)<!doctype\s+html\b|<html\b|<script\b", prefix):
        language, media_type = "html", "text/html"
    elif re.match(r"(?is)(?:<\?xml[^>]*>\s*)?<svg\b", prefix):
        language, media_type = "xml", "image/svg+xml"
    elif prefix.startswith("<?xml"):
        language, media_type = "xml", "application/xml"
    if PurePosixPath(name).name.lower() in {"dockerfile", "makefile", ".env", ".gitignore"}:
        language = "text"
    kind = "code" if language else "markdown" if extension in {".md", ".markdown"} else "text"
    notice = "Active content is shown as source only; it is not executed." if media_type in {
        "text/html", "image/svg+xml", "application/xml",
    } else None
    return Preview(kind, media_type, content, language, truncated, notice)


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """A single RFC byte range for media seeking; no multipart amplification."""
    if header is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()) or size == 0:
        raise HTTPException(416, "The requested byte range is unavailable.", headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    # Bound integer parsing even for arbitrarily long request headers.
    if len(left) > 20 or len(right) > 20:
        raise HTTPException(416, "The requested byte range is unavailable.", headers={"Content-Range": f"bytes */{size}"})
    start = int(left) if left else max(0, size - int(right))
    end = min(int(right), size - 1) if left and right else size - 1
    if start >= size or end < start or (not left and int(right) == 0):
        raise HTTPException(416, "The requested byte range is unavailable.", headers={"Content-Range": f"bytes */{size}"})
    return start, end


def disposition(mode: str, name: str) -> str:
    filename = PurePosixPath(name.replace("\\", "/")).name or "file"
    return f"{mode}; filename*=UTF-8''{quote(filename, safe='')}"


class FilePreviewService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.reads = asyncio.Semaphore(4)

    async def record(self, user: User, session_id: str, kind: ResourceKind, resource_id: str) -> FileRecord:
        current = await self.runtime.database.user(user.id)
        if current.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        async with self.runtime.database.sessions() as db:
            await self.runtime.database.allowed_session(db, session_id, user)
            row = await db.get(Artifact if kind == "artifact" else Upload, resource_id)
            if row is None:
                raise HTTPException(404, "File not found in this session.")
            if row.session_id != session_id and (
                kind != "artifact"
                or not (
                    await self.runtime.database.swarms.shared_artifact(db, user, session_id, row.session_id)
                    or await self.delegated_artifact(db, user, session_id, row.session_id)
                )
            ):
                # Ordinary allowed sessions and input uploads do not imply
                # cross-session access. Swarms explicitly share saved outputs.
                raise HTTPException(404, "File not found in this session.")
            return FileRecord(row.id, kind, session_id, row.name, row.media_type, row.size, row.digest)

    async def delegated_artifact(self, db, user: User, session_id: str, source_id: str | None) -> bool:
        from .delegation import DelegationService
        return await DelegationService(self.runtime.database).shared_artifact(db, user, session_id, source_id)

    async def load(
        self, user: User, session_id: str, kind: ResourceKind, resource_id: str,
    ) -> tuple[FileRecord, bytes, Preview]:
        record = await self.record(user, session_id, kind, resource_id)
        limit = MAX_ARTIFACT_BYTES if kind == "artifact" else MAX_UPLOAD_BYTES
        if record.size < 0 or record.size > limit:
            raise HTTPException(413, "This file exceeds the preview size limit. Download the original instead.")
        async with self.reads:
            # Objects are encrypted as a whole. Delivery/upload caps bound the
            # decryption read; the much smaller text prefix bounds the response.
            try:
                raw = await self.runtime.objects.get(record.digest)
            except (OSError, ValueError, InvalidToken, ClientError):
                await self.record(user, session_id, kind, resource_id)
                raise HTTPException(503, "The saved file could not be read. Retry or download the original.") from None
            preview = (
                await asyncio.to_thread(classify, raw, record.name, record.media_type)
                if len(raw) <= limit and len(raw) == record.size else None
            )
            current = await self.record(user, session_id, kind, resource_id)
            if current != record:
                raise HTTPException(409, "The file changed while loading. Refresh to retry.")
            if len(raw) > limit:
                raise HTTPException(413, "This file exceeds the preview size limit. Download the original instead.")
            if preview is None:
                raise HTTPException(409, "The saved file does not match its metadata. Refresh to retry.")
        return record, raw, preview


def routes(runtime: Runtime, actor, *, service: FilePreviewService | None = None) -> APIRouter:
    router = APIRouter()
    service = service or FilePreviewService(runtime)

    @router.get("/api/sessions/{session_id}/file-preview/{kind}/{resource_id}", operation_id="getSessionFilePreview")
    async def preview(
        session_id: str, kind: ResourceKind, resource_id: str, response: Response, user: User = Depends(actor),
    ) -> SessionFilePreview:
        record, _raw, view = await service.load(user, session_id, kind, resource_id)
        response.headers.update(HEADERS)
        return SessionFilePreview(
            id=record.id, kind=record.kind, name=record.name, size=record.size,
            media_type=view.media_type, preview_kind=view.kind, text=view.text,
            language=view.language, truncated=view.truncated, notice=view.notice,
            text_byte_limit=MAX_TEXT_BYTES, text_line_limit=MAX_TEXT_LINES,
            download_url=record.download_url,
            content_url=record.path + "/content" if view.inline else None,
        )

    @router.get("/api/sessions/{session_id}/file-preview/{kind}/{resource_id}/content")
    async def content(
        session_id: str, kind: ResourceKind, resource_id: str, request: Request, user: User = Depends(actor),
    ):
        record, raw, view = await service.load(user, session_id, kind, resource_id)
        if not view.inline:
            raise HTTPException(415, "This format is available as source or download only.")
        headers = {
            **HEADERS, "Content-Disposition": disposition("inline", record.name),
            "Accept-Ranges": "bytes", "X-Frame-Options": "DENY",
        }
        selected = byte_range(request.headers.get("range"), len(raw))
        if selected:
            start, end = selected
            headers["Content-Range"] = f"bytes {start}-{end}/{len(raw)}"
            return Response(raw[start:end + 1], status_code=206, media_type=view.media_type, headers=headers)
        return Response(raw, media_type=view.media_type, headers=headers)

    @router.get("/api/sessions/{session_id}/file-preview/{kind}/{resource_id}/download")
    async def download(
        session_id: str, kind: ResourceKind, resource_id: str, user: User = Depends(actor),
    ):
        record, raw, _view = await service.load(user, session_id, kind, resource_id)
        return Response(raw, media_type="application/octet-stream", headers={
            **HEADERS, "Content-Disposition": disposition("attachment", record.name),
        })

    return router
