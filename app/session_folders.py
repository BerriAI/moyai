"""Personal sidebar organization; session visibility and execution are unchanged."""
import sqlite3
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import now

FolderId = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]


class FolderName(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)

    @field_validator('name')
    @classmethod
    def readable_name(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Use a single line for the folder name.')
        return value


class FolderEdit(FolderName):
    revision: int = Field(ge=1)


class FolderRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


class FolderMove(BaseModel):
    model_config = ConfigDict(extra='forbid')
    folder_id: FolderId | None


class SessionFolders:
    def __init__(self, store, security, checkpoints):
        self.store, self.security, self.checkpoints = store, security, checkpoints
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS session_folders (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL, name_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, id), UNIQUE(owner_id, name_key));
                CREATE TABLE IF NOT EXISTS session_folder_memberships (
                    owner_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    folder_id TEXT NOT NULL, PRIMARY KEY(owner_id, run_id),
                    FOREIGN KEY(owner_id, folder_id) REFERENCES session_folders(owner_id, id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS idx_session_folder_members
                    ON session_folder_memberships(owner_id, folder_id);
            ''')

    def actor(self, request, mutation=False):
        self.security.require(request, mutation=mutation)
        return self.store.identity(self.security.session_info(request))

    def memberships(self, owner):
        return {row['run_id']: row['folder_id'] for row in self.store.rows('''
            SELECT m.run_id,m.folder_id FROM session_folder_memberships m
            JOIN runs r ON r.id=m.run_id WHERE m.owner_id=?
            ORDER BY r.updated_at DESC,r.created_at DESC,r.id DESC''', (owner,))}

    def listing(self, owner):
        return self.store.rows('''SELECT f.id,f.name,f.revision,COUNT(m.run_id) AS session_count
            FROM session_folders f LEFT JOIN session_folder_memberships m
            ON m.owner_id=f.owner_id AND m.folder_id=f.id WHERE f.owner_id=?
            GROUP BY f.id ORDER BY f.name_key,f.id''', (owner,))

    def require_folder(self, conn, owner, folder_id, revision=None):
        folder = conn.execute('SELECT * FROM session_folders WHERE id=? AND owner_id=?',
                              (folder_id, owner)).fetchone()
        if not folder:
            raise HTTPException(404, 'Folder not found. Refresh your sidebar and try again.')
        if revision is not None and revision != folder['revision']:
            raise HTTPException(409, 'This folder changed. Close this dialog and try again.')
        return folder

    def save(self, owner, body, folder_id=None):
        stamp = now()
        try:
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if folder_id:
                    old = self.require_folder(conn, owner, folder_id, body.revision)
                    revision = old['revision'] + 1
                    conn.execute('''UPDATE session_folders SET name=?,name_key=?,revision=?,updated_at=?
                        WHERE id=? AND owner_id=?''',
                                 (body.name, body.name.casefold(), revision, stamp, folder_id, owner))
                else:
                    count = conn.execute('SELECT COUNT(*) FROM session_folders WHERE owner_id=?', (owner,)).fetchone()[0]
                    if count >= 100:
                        raise HTTPException(422, 'You can create up to 100 folders.')
                    folder_id, revision = uuid4().hex, 1
                    conn.execute('INSERT INTO session_folders VALUES(?,?,?,?,?,?,?)',
                                 (folder_id, owner, body.name, body.name.casefold(), revision, stamp, stamp))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, 'You already have a folder with that name.') from exc
        return {'id': folder_id, 'name': body.name, 'revision': revision}

    def move(self, owner, run_id, folder_id):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT parent_run_id FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, 'Session not found.')
            if run['parent_run_id']:
                raise HTTPException(422, 'Move the parent session to keep its agents together.')
            if folder_id:
                self.require_folder(conn, owner, folder_id)
                conn.execute('''INSERT INTO session_folder_memberships VALUES(?,?,?)
                    ON CONFLICT(owner_id,run_id) DO UPDATE SET folder_id=excluded.folder_id''',
                             (owner, run_id, folder_id))
            else:
                conn.execute('DELETE FROM session_folder_memberships WHERE owner_id=? AND run_id=?', (owner, run_id))
        return {'run_id': run_id, 'folder_id': folder_id}

    def routes(self):
        router = APIRouter()

        @router.get('/api/session-folders')
        async def listing(request: Request):
            return {'folders': self.listing(self.actor(request))}

        @router.post('/api/session-folders', status_code=201)
        async def create(body: FolderName, request: Request):
            result = self.save(self.actor(request, True), body)
            await self.checkpoints.flush()
            return result

        @router.patch('/api/session-folders/{folder_id}')
        async def rename(folder_id: str, body: FolderEdit, request: Request):
            result = self.save(self.actor(request, True), body, folder_id)
            await self.checkpoints.flush()
            return result

        @router.delete('/api/session-folders/{folder_id}')
        async def delete(folder_id: str, body: FolderRevision, request: Request):
            owner = self.actor(request, True)
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                self.require_folder(conn, owner, folder_id, body.revision)
                conn.execute('DELETE FROM session_folders WHERE id=? AND owner_id=?', (folder_id, owner))
            await self.checkpoints.flush()
            return {'ok': True}

        @router.put('/api/runs/{run_id}/folder')
        async def move(run_id: str, body: FolderMove, request: Request):
            result = self.move(self.actor(request, True), run_id, body.folder_id)
            await self.checkpoints.flush()
            return result

        return router
