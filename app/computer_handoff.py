"""A durable, requester-scoped instruction to reveal the session computer."""
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .db import now


class Handoff(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=500,
                        description='Briefly explain what the user needs to do, without credentials.')


TOOL = {
    'name': 'computer_handoff',
    'description': 'Show the current session Computer panel to the requester and automatically wake '
        'its desktop if asleep. Use before asking the user to sign in with SSO, complete authentication, '
        'or interact with the computer, and when asked to open/show the Computer panel. Navigate to the '
        'relevant page first using browser tools. This requests presentation in the Moyai web app; it '
        'does not sign in, take control, or confirm that a user is viewing it. After calling, stop '
        'browser actions and wait for the user to finish. Never ask for passwords or codes in chat.',
    'inputSchema': Handoff.model_json_schema(),
    'annotations': {'readOnlyHint': False, 'idempotentHint': False},
}


def request_handoff(store, run, arguments):
    args = Handoff.model_validate(arguments)
    with store.connect(write_scope=run['id']) as conn:
        conn.begin_write()
        fresh = conn.execute('SELECT * FROM runs WHERE id=?', (run['id'],)).fetchone()
        if (not fresh or fresh['mode'] != 'modal' or not fresh['chat_enabled']
                or not fresh['active_message_id'] or not fresh['active_user_id']
                or fresh['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or fresh['deleted_at'] or fresh['deletion_requested_at']):
            raise HTTPException(409, 'A computer handoff requires an active cloud conversation.')
        if (fresh['active_message_id'] != run['active_message_id']
                or fresh['active_user_id'] != run['active_user_id']
                or fresh['token_hash'] != run['token_hash']):
            raise HTTPException(409, 'The conversation changed. Request the handoff in the current turn.')
        previous = conn.execute("SELECT data FROM events WHERE run_id=? AND kind='computer' ORDER BY id DESC LIMIT 1",
                                (run['id'],)).fetchone()
        replayed = bool(previous and json.loads(previous['data']).get('turn_id') == fresh['active_message_id'])
        if not replayed:
            data = {'phase': 'handoff', 'version': 1, 'turn_id': fresh['active_message_id'],
                    'user_id': fresh['active_user_id'], 'reason': args.reason}
            conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'computer',?,?,?)",
                         (run['id'], 'Computer input requested', json.dumps(data), now()))
    return {'status': 'requested', 'replayed': replayed,
            'instruction': 'The requester’s Moyai web app will open Computer and wake it if needed. '
                'Ask them to complete the step there, then wait. This is not confirmation of sign-in.'}
