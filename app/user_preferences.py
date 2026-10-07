"""Account-scoped web chat preferences, resolved from the verified session."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict


class ChatPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    send_immediately: bool


class UserPreferences:
    def __init__(self, store, security, checkpoints):
        self.store, self.security, self.checkpoints = store, security, checkpoints
        with store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                send_immediately INTEGER NOT NULL DEFAULT 0 CHECK(send_immediately IN (0,1))
            )''')

    def get(self, user_id):
        rows = self.store.rows('SELECT send_immediately FROM user_preferences WHERE user_id=?', (user_id,))
        return {'send_immediately': bool(rows and rows[0]['send_immediately'])}

    def routes(self):
        router = APIRouter()

        @router.get('/api/settings/preferences')
        async def read(request: Request):
            self.security.require(request)
            return self.get(self.store.identity(self.security.session_info(request)))

        @router.put('/api/settings/preferences')
        async def save(body: ChatPreferences, request: Request):
            self.security.require(request, mutation=True)
            user_id = self.store.identity(self.security.session_info(request))
            self.store.execute('''INSERT INTO user_preferences(user_id,send_immediately) VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET send_immediately=excluded.send_immediately''',
                               (user_id, body.send_immediately))
            await self.checkpoints.flush()
            return self.get(user_id)

        return router
