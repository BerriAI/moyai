from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NotRequired, TypedDict, TypeGuard

import httpx
from agentchat.channels.slack import files as slack_files
from agentchat.models import UploadFile
from sqlalchemy import select

from .models import Artifact, Delivery

if TYPE_CHECKING:
    from .runtime import Runtime
    from .storage import ObjectStore


class _UploadContent(TypedDict):
    filename: str
    title: str | None
    content: NotRequired[str]
    digest: NotRequired[str]


class SlackFileContent(TypedDict):
    upload: _UploadContent
    after: str


class SlackFileDelivery:
    @staticmethod
    def content(file: UploadFile, *, after: str) -> dict[str, object]:
        return {
            "upload": {
                "filename": file.filename,
                "title": file.title,
                "content": base64.b64encode(file.content).decode("ascii"),
            },
            "after": after,
        }

    @staticmethod
    def artifact(artifact: Artifact, *, after: str) -> dict[str, object]:
        filename = PurePosixPath(artifact.name).name
        return {
            "upload": {
                "filename": filename,
                "title": filename,
                "digest": artifact.digest,
            },
            "after": after,
        }

    @staticmethod
    def strip_links(text: str, artifact_ids: Iterable[str], public_url: str) -> str:
        public_url_prefix = re.escape(public_url.rstrip("/"))
        for artifact_id in artifact_ids:
            text = re.sub(
                rf"!?\[[^\]\n]*\]\((?:{public_url_prefix})?/api/files/{re.escape(artifact_id)}\)",
                "",
                text,
            )
        return re.sub(r"\n{3,}", "\n\n", text).rstrip()

    @staticmethod
    def is_content(content: object) -> TypeGuard[SlackFileContent]:
        if not isinstance(content, dict):
            return False
        upload = content.get("upload")
        return (
            isinstance(content.get("after"), str)
            and isinstance(upload, dict)
            and isinstance(upload.get("filename"), str)
            and "title" in upload
            and (upload["title"] is None or isinstance(upload["title"], str))
            and ("content" not in upload or isinstance(upload["content"], str))
            and ("digest" not in upload or isinstance(upload["digest"], str))
            and (isinstance(upload.get("content"), str) != isinstance(upload.get("digest"), str))
        )

    @staticmethod
    async def ready(runtime: Runtime, content: SlackFileContent) -> bool:
        async with runtime.database.sessions() as db:
            status = await db.scalar(select(Delivery.status).where(
                Delivery.identity == content["after"],
            ))
        return status == "sent"

    @staticmethod
    async def send(
        client: httpx.AsyncClient,
        content: SlackFileContent,
        destination: dict[str, str],
        *,
        objects: ObjectStore,
        headers: dict[str, str] | None,
        before_send: Callable[[], Awaitable[None]],
    ) -> dict[str, object]:
        upload = content["upload"]
        digest = upload.get("digest")
        if digest is not None:
            file_content = await objects.get(digest)
        else:
            encoded_content = upload.get("content")
            if encoded_content is None:
                raise ValueError("Slack file upload content is missing.")
            file_content = base64.b64decode(encoded_content, validate=True)

        async def request(api_method: str, payload: dict[str, object]) -> dict[str, object]:
            response = await client.post(api_method, headers=headers, json=payload)
            response.raise_for_status()
            result: object = response.json()
            if not isinstance(result, dict):
                raise TypeError("Slack file upload API returned a non-object response.")
            normalized: dict[str, object] = {}
            for key, value in result.items():
                if not isinstance(key, str):
                    raise TypeError("Slack file upload API returned a non-string key.")
                normalized[key] = value
            return normalized

        uploaded = await slack_files.upload_slack_files(
            (UploadFile(
                filename=upload["filename"],
                content=file_content,
                title=upload["title"],
            ),),
            request=request,
            channel_id=destination["channel"],
            thread_ts=destination["thread_ts"],
            before_send=before_send,
        )
        return {"files": [{"id": file.id, "permalink": file.permalink} for file in uploaded]}
