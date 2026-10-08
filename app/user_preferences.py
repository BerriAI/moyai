"""Account preferences, resolved from the verified session or active turn author."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict


class ChatPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    send_immediately: bool = False
    omit_private_tool_payloads: bool = False


class UserPreferences:
    def __init__(self, store, security, checkpoints):
        self.store, self.security, self.checkpoints = store, security, checkpoints
        with store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                send_immediately INTEGER NOT NULL DEFAULT 0 CHECK(send_immediately IN (0,1))
            )''')
            if 'omit_private_tool_payloads' not in {row[1] for row in conn.execute('PRAGMA table_info(user_preferences)')}:
                conn.execute('''ALTER TABLE user_preferences ADD COLUMN omit_private_tool_payloads
                    INTEGER NOT NULL DEFAULT 0 CHECK(omit_private_tool_payloads IN (0,1))''')

    def get(self, user_id):
        rows = self.store.rows('SELECT * FROM user_preferences WHERE user_id=?', (user_id,))
        return {name: bool(rows and rows[0][name]) for name in ChatPreferences.model_fields}

    def for_run(self, run):
        message_id = run.get('active_message_id') or run.get('message_id')
        messages = self.store.rows("SELECT user_id FROM messages WHERE run_id=? AND id=? AND role='user'",
                                   (run['id'], message_id)) if message_id else []
        actor = messages[0]['user_id'] if messages else run.get('active_user_id') or run.get('owner_id')
        # Linked Slack turns use the preference saved by the same Google account.
        linked = self.store.rows('''SELECT linked.id FROM users u JOIN users linked
            ON linked.id=u.linked_user_id AND linked.kind='google' WHERE u.id=? AND u.kind='slack' ''', (actor,))
        return self.get(linked[0]['id'] if linked else actor)

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
            changes = body.model_dump(exclude_unset=True)
            if changes:
                # Only validated field names enter SQL. Updating just supplied fields
                # keeps independent switches from overwriting each other.
                columns = ','.join(changes)
                updates = ','.join(f'{key}=excluded.{key}' for key in changes)
                self.store.execute(f'''INSERT INTO user_preferences(user_id,{columns})
                    VALUES(?,{','.join('?' for _ in changes)})
                    ON CONFLICT(user_id) DO UPDATE SET {updates}''', (user_id, *changes.values()))
            await self.checkpoints.flush()
            return self.get(user_id)

        return router
