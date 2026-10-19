"""Personal sidebar organization; no agent state or run recovery lives here."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update

from .models import Message, SessionFolder, SessionPreference, SessionRead, User, new_id


class SessionFolderView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: str
    name: str


class SessionFolderRemoved(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    removed: Literal[True]


class SessionReadResult(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    read: Literal[True]


class FolderName(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a folder name.")
        return value


class ReadThrough(BaseModel):
    message_id: int = Field(ge=0)


async def lock_sidebar(db, user_id: str) -> None:
    # Serialize preference insert/update and folder rename/move across workers,
    # including SQLite, which does not implement SELECT FOR UPDATE.
    await db.execute(update(User).where(User.id == user_id).values(preferences=User.preferences))


async def folders_for(db, user_id: str) -> list[SessionFolder]:
    return list(await db.scalars(select(SessionFolder).where(
        SessionFolder.user_id == user_id,
    ).order_by(SessionFolder.name, SessionFolder.id)))


def routes(runtime, actor) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session-folders", operation_id="listSessionFolders")
    async def folders(user: User = Depends(actor)) -> list[SessionFolderView]:
        async with runtime.database.sessions() as db:
            return [SessionFolderView(id=row.id, name=row.name) for row in await folders_for(db, user.id)]

    @router.post("/api/session-folders", status_code=201, operation_id="createSessionFolder")
    async def create_folder(body: FolderName, user: User = Depends(actor)) -> SessionFolderView:
        async with runtime.database.sessions.begin() as db:
            await lock_sidebar(db, user.id)
            if any(row.name.casefold() == body.name.casefold() for row in await folders_for(db, user.id)):
                raise HTTPException(409, "You already have a folder with this name.")
            row = SessionFolder(id=new_id(), user_id=user.id, name=body.name)
            db.add(row)
        return SessionFolderView(id=row.id, name=row.name)

    @router.patch("/api/session-folders/{folder_id}", operation_id="renameSessionFolder")
    async def rename_folder(folder_id: str, body: FolderName, user: User = Depends(actor)) -> SessionFolderView:
        async with runtime.database.sessions.begin() as db:
            await lock_sidebar(db, user.id)
            row = await db.get(SessionFolder, folder_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(404, "Folder not found.")
            if any(item.id != row.id and item.name.casefold() == body.name.casefold()
                   for item in await folders_for(db, user.id)):
                raise HTTPException(409, "You already have a folder with this name.")
            # Existing preferences stored names. Keep those memberships intact.
            await db.execute(update(SessionPreference).where(
                SessionPreference.user_id == user.id, SessionPreference.folder == row.name,
            ).values(folder=body.name))
            row.name = body.name
        return SessionFolderView(id=row.id, name=row.name)

    @router.delete("/api/session-folders/{folder_id}", operation_id="deleteSessionFolder")
    async def remove_folder(folder_id: str, user: User = Depends(actor)) -> SessionFolderRemoved:
        async with runtime.database.sessions.begin() as db:
            await lock_sidebar(db, user.id)
            row = await db.get(SessionFolder, folder_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(404, "Folder not found.")
            await db.execute(update(SessionPreference).where(
                SessionPreference.user_id == user.id, SessionPreference.folder == row.name,
            ).values(folder=""))
            await db.execute(delete(SessionFolder).where(SessionFolder.id == row.id))
        return SessionFolderRemoved(removed=True)

    @router.post("/api/sessions/{session_id}/read", operation_id="markSessionRead")
    async def read_session(session_id: str, body: ReadThrough, user: User = Depends(actor)) -> SessionReadResult:
        async with runtime.database.sessions.begin() as db:
            await lock_sidebar(db, user.id)
            await runtime.database.allowed_session(db, session_id, user)
            latest = await db.scalar(select(func.max(Message.id)).where(Message.session_id == session_id)) or 0
            row = await db.get(SessionRead, (user.id, session_id))
            if row is None:
                row = SessionRead(user_id=user.id, session_id=session_id, message_id=0)
                db.add(row)
            row.message_id = max(row.message_id, min(body.message_id, latest))
            row.unread = False
        return SessionReadResult(read=True)

    return router
