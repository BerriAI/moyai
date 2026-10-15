"""Private image uploads and conversion to Pydantic's native multimodal input."""
from __future__ import annotations

import asyncio
import base64
import io
import warnings
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

from .contracts import InputImage, PreparedTurn, SteeringInput
from .models import Upload, User

if TYPE_CHECKING:
    from .runtime import Runtime

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


def image_type(raw: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                media_type = MEDIA_TYPES.get(image.format or "")
                if media_type is None or image.width * image.height > 20_000_000:
                    raise ValueError("Use a PNG, JPEG, GIF or WebP image up to 20 megapixels.")
                image.verify()
                return media_type
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("The attachment is not a supported image.") from error


def prompt_content(prompt: str, images: list[InputImage]) -> str | list[str | BinaryContent] | None:
    if not images:
        return prompt or None
    return [
        prompt or "Please inspect the attached images.",
        *(BinaryContent(data=base64.b64decode(image.data), media_type=image.media_type) for image in images),
    ]


async def hydrate(runtime: Runtime, value: PreparedTurn | SteeringInput) -> None:
    async def load(images: list[InputImage]) -> None:
        for image in images:
            image.data = base64.b64encode(await runtime.objects.get(image.digest)).decode()
    await load(value.images)
    if isinstance(value, PreparedTurn) and value.conversation:
        conversation = ConversationTypeAdapter.validate_json(value.conversation)
        changed = False
        for message in conversation.messages:
            identity = (message.metadata or {}).get("moyai_public_turn")
            if isinstance(message, ModelRequest) and identity:
                images = await runtime.database.input_images(identity, session_id=value.scope.session_id)
                if images:
                    await load(images)
                    for part in message.parts:
                        if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                            part.content = prompt_content(part.content, images) or ""
                            changed = True
        if changed:
            value.conversation = ConversationTypeAdapter.dump_json(conversation).decode()


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()

    @router.post("/api/uploads", status_code=201)
    async def upload(request: Request, name: str = "clipboard.png", user: User = Depends(actor)):
        if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise HTTPException(422, "Use an image filename up to 200 characters.")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Images must be 8 MB or smaller.")
            raw.extend(chunk)
        try:
            media_type = await asyncio.to_thread(image_type, bytes(raw))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        digest = await runtime.objects.put(bytes(raw))
        async with runtime.database.sessions.begin() as db:
            row = Upload(owner_id=user.id, name=name, digest=digest, media_type=media_type, size=len(raw))
            db.add(row)
            await db.flush()
            return {"id": row.id, "name": row.name, "media_type": row.media_type, "size": row.size}

    @router.get("/api/uploads/{upload_id}")
    async def content(upload_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            row = await db.get(Upload, upload_id)
            if row is None:
                raise HTTPException(404, "Image not found.")
            if row.session_id:
                await runtime.database.allowed_session(db, row.session_id, user)
            elif row.owner_id != user.id:
                raise HTTPException(404, "Image not found.")
        return Response(await runtime.objects.get(row.digest), media_type=row.media_type)

    return router
