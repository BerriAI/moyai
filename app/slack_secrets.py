"""Read-only Slack access using the active requester's encrypted personal secret."""
import json

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .connector_errors import ConnectorError
from .personal_slack import KEYS


class Access(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=128, description='Handle from credentials_request for provider=generic, name=slack-personal, format=env with SLACK_USER_TOKEN. Must be a personal secret.')


class Search(Access):
    query: str = Field(min_length=1, max_length=500)
    page: int = Field(default=1, ge=1, le=10000)


class Conversations(Access):
    cursor: str = Field(default='', max_length=2048)


class History(Conversations):
    channel: str = Field(pattern=r'^[CDG][A-Z0-9]{7,30}$')


class Thread(History):
    thread_ts: str = Field(pattern=r'^\d{10,16}\.\d{1,9}$')


GUIDANCE = (' Uses only the current requester\'s personal Slack user token; never falls back to shared or bot access. '
            'Save the personal token in Settings > Secrets first; private chats cannot open credential forms. First use credentials_list and credentials_request with provider=generic, name=slack-personal, format=env, '
            'input_fields=[{"name":"SLACK_USER_TOKEN","label":"Slack user token"}]. '
            'The user must choose Personal sharing. Never ask for tokens in chat. '
            'Only available in the current requester\'s private web chat; shared, Slack-mirrored, automation and delegated sessions are rejected.')
TOOLS = {
    'slack_personal_search': (Search, 'Search messages, including authorized private channels, DMs and group DMs. Returns 20 matches per page; use paging to continue.' + GUIDANCE),
    'slack_personal_conversations': (Conversations, 'List the user\'s conversations, including DMs and group DMs. Returns up to 100; follow response_metadata.next_cursor.' + GUIDANCE),
    'slack_personal_history': (History, 'Read a conversation\'s recent messages. Returns up to 15; follow response_metadata.next_cursor.' + GUIDANCE),
    'slack_personal_thread': (Thread, 'Read a Slack thread. Returns up to 15 messages; follow response_metadata.next_cursor.' + GUIDANCE),
}


def enabled(vault, run):
    policies = vault.store.rows("SELECT enabled FROM connection_policies WHERE provider='slack'")
    if 'slack' not in run.get('plugins', []) or (policies and not policies[0]['enabled']):
        return False
    try:
        private_context(vault, run)
    except (ConnectorError, HTTPException):
        return False
    return True


def private_context(vault, run):
    service = vault.personal_slack
    if service is None:
        raise HTTPException(403, 'Personal Slack is unavailable.')
    owner = service.selected_owner(run)
    if not owner or owner != run.get('active_user_id'):
        raise HTTPException(403, 'Personal Slack requires an individual requester in their private web chat.')
    try:
        service.eligible(run, owner)
    except ConnectorError as exc:
        raise HTTPException(403, str(exc)) from None


def token_for(vault, run, request_id, *, audit=True):
    private_context(vault, run)
    if not enabled(vault, run):
        raise HTTPException(403, 'Slack is disabled for this task or by organization policy.')
    with vault.store.connect() as conn:
        row, secret = vault.authorized_request(conn, run, request_id)
        if (row['status'] != 'provided' or not secret or secret['scope'] != 'personal'
                or not vault.permitted(secret, run, run['active_user_id'])
                or vault.status(secret) != 'active'):
            raise HTTPException(403, 'Request active personal Slack access for the current requester through the secure form.')
        if (secret['provider'], secret['name'], secret['format']) != ('generic', 'slack-personal', 'env'):
            raise HTTPException(422, 'Use a personal slack-personal environment secret with SLACK_USER_TOKEN.')
        value = json.loads(vault.security.decrypt(secret['encrypted']))
        token = value.get('SLACK_USER_TOKEN', '')
        if not isinstance(token, str) or not token.startswith('xoxp-') or any(c.isspace() for c in token):
            raise HTTPException(422, 'Provide a Slack user OAuth token (xoxp-) as SLACK_USER_TOKEN through the secure form.')
        if audit:
            vault.audit_in(conn, run['active_user_id'], secret['id'], 'personal Slack read', run['id'])
        return token


async def call(vault, run, name, args):
    token = token_for(vault, run, args.request_id)
    if name == 'slack_personal_search':
        endpoint, params = 'search.messages', {'query': args.query, 'count': 20, 'page': args.page, 'highlight': False}
    elif name == 'slack_personal_conversations':
        endpoint, params = 'users.conversations', {'types': 'public_channel,private_channel,im,mpim', 'limit': 100, 'cursor': args.cursor}
    else:
        endpoint = 'conversations.replies' if name == 'slack_personal_thread' else 'conversations.history'
        params = {'channel': args.channel, 'limit': 15, 'cursor': args.cursor}
        if name == 'slack_personal_thread':
            params['ts'] = args.thread_ts
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get('https://slack.com/api/' + endpoint,
                                        headers={'Authorization': 'Bearer ' + token}, params=params)
        # Authorization can change while Slack is responding. Do not release a
        # response after revocation, policy changes, or a turn switching users.
        current = vault.store.run(run['id'])
        if not current or any(current.get(k) != run.get(k) for k in KEYS) or token_for(vault, current, args.request_id, audit=False) != token:
            raise HTTPException(403, 'Personal Slack access changed. Request access again.')
        if response.status_code == 429:
            return {'error': 'Slack rate limit reached. Retry later.', 'status_code': 429}
        if response.status_code != 200:
            return {'error': 'Slack request failed. Check access and retry.', 'status_code': response.status_code}
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        if not data.get('ok'):
            error = data.get('error')
            if error in {'invalid_auth', 'token_revoked', 'account_inactive', 'token_expired'}:
                return {'error': 'Personal Slack token is invalid or expired. Replace it through Settings > Secrets.', 'status_code': 401}
            if error == 'missing_scope':
                return {'error': 'Personal Slack token lacks the required read scopes. Update the Slack app user scopes and reinstall.', 'status_code': 403}
            return {'error': 'Slack denied this read. Check conversation membership and token permissions.', 'status_code': 403}
        # Slack data is untrusted; never return an accidental exact token echo.
        return json.loads(json.dumps(data).replace(token, '[REDACTED]'))
    except (httpx.HTTPError, ValueError):
        return {'error': 'Could not read Slack. Check connectivity and retry.', 'status_code': 502}
