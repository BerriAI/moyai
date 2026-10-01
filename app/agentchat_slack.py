"""AgentChat channel for Moyai's signed HTTP events and durable session store.

The upstream Slack transport uses Socket Mode. This public Channel/State adapter
keeps our existing webhook and rotating OAuth credentials; no app-level token is
needed. AgentChat owns normalized dispatch, conversation locks and reply routing.
SQLite receipts, sessions and the outbox remain the authoritative durable state.
"""
import asyncio
from types import MappingProxyType
from weakref import WeakValueDictionary

from agentchat import AgentChat
from agentchat.channels.slack_mirror import mirror_payload
from agentchat.models import Message, Sender

from .slack_activity import threaded


class SessionState:
    def __init__(self, store):
        self.store = store
        self._locks = WeakValueDictionary()

    async def claim(self, message_id):
        # Do not persist a claim before the handler's atomic receipt + enqueue.
        # Otherwise a temporary failure could permanently swallow Slack's retry.
        event_id = message_id.split(':', 2)[-1]
        return not self.store.rows('SELECT 1 FROM slack_receipts WHERE event_id=?', (event_id,))

    def lock(self, conversation_id):
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    async def append(self, message):
        # Incoming turns are saved with their Slack receipt by SlackChat.accept;
        # outgoing messages already exist in the durable outbox. Avoid a second
        # transcript that could disagree after cancellation or a failed send.
        pass

    async def history(self, conversation_id, *, limit=None):
        parts = conversation_id.split(':')
        if len(parts) not in {3, 4} or parts[0] != 'slack':
            return ()
        _, team, channel, *thread = parts
        bindings = self.store.rows('SELECT run_id FROM slack_threads WHERE team_id=? AND channel=?' +
                                   (' AND thread_ts=?' if thread else ' AND thread_ts=started_ts') + ' ORDER BY started_ts LIMIT 1',
                                   (team, channel, *thread))
        if not bindings:
            return ()
        rows = self.store.messages(bindings[0]['run_id'])
        if limit is not None:
            rows = rows[-max(0, limit):] if limit > 0 else []
        return tuple(Message(id=f"moyai:{bindings[0]['run_id']}:{row['id']}", conversation_id=conversation_id,
                             channel='slack', sender=Sender(id=row['user_id'] or 'moyai'),
                             text=row['content'], role=row['role']) for row in rows)


class SlackWebhookChannel:
    name = 'slack'

    def __init__(self, owner):
        self.owner = owner
        self._receiver = None
        self._closed = asyncio.Event()

    def bind(self, receiver):
        self._receiver = receiver

    async def run(self):
        # FastAPI receives signed events; there is no socket listener to start.
        await self._closed.wait()

    async def close(self):
        self._closed.set()

    async def handle_validated_event(self, *, team, event_id, channel, ts, root, user,
                                     prompt, mentioned, direct_message, missing_cloud):
        conversation = f'slack:{team}:{channel}' + ('' if direct_message and root == ts else ':' + root)
        message = Message(id=f'slack:{team}:{event_id}', conversation_id=conversation, channel=self.name,
                          sender=Sender(id=user), text=prompt, role='user',
                          metadata=MappingProxyType({'team': team, 'event_id': event_id, 'channel': channel,
                              'ts': ts, 'root': root, 'mentioned': mentioned, 'direct_message': direct_message,
                              'missing_cloud': tuple(missing_cloud)}))
        await self._receiver(self, message)

    def source_for_run(self, run_id):
        binding = self.owner.store.rows('SELECT * FROM slack_threads WHERE run_id=?', (run_id,))[0]
        conversation = f"slack:{binding['team_id']}:{binding['channel']}"
        if not binding['channel'].startswith('D') or binding['thread_ts'] != binding['started_ts']:
            conversation += ':' + binding['thread_ts']
        return Message(id='moyai:' + run_id, conversation_id=conversation, channel=self.name,
                       sender=Sender(id='moyai'), text='', role='user', metadata=MappingProxyType({'run_id': run_id}))

    async def reply(self, source, content):
        # Resolve the destination from our saved binding, never model output.
        binding = self.owner.store.rows('SELECT * FROM slack_threads WHERE run_id=?', (source.metadata['run_id'],))[0]
        if not self.owner.status()['enabled'] or binding['team_id'] != self.owner.connectors.slack_installation().get('team_id'):
            raise RuntimeError('Slack connection changed before delivery.')
        token = await self.owner.connectors.slack_bot_token()
        link = self.owner.chat.link(source.metadata['run_id'])
        body = content.removesuffix('\n\n' + link)
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body, 'verbatim': True}}]
        if body != content:
            blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': link, 'verbatim': True}]})
        response = await self.owner.connectors.request('POST', 'https://slack.com/api/chat.postMessage',
            headers={'Authorization': f'Bearer {token}'}, json={
                'channel': binding['channel'],
                'thread_ts': None if binding['channel'].startswith('D') and binding['thread_ts'] == binding['started_ts'] else binding['thread_ts'],
                'text': content,
                'blocks': blocks if len(body) <= 3000 else None,
                'unfurl_links': False, 'unfurl_media': False, 'parse': 'none', 'link_names': False})
        return Message(id=f"slack:{binding['channel']}:{response.get('ts', '')}",
                       conversation_id=source.conversation_id, channel=self.name,
                       sender=Sender(id='moyai'), text=content, role='assistant',
                       metadata=MappingProxyType({'slack_ts': response.get('ts', '')}))

    async def set_status(self, source, status):
        run_id = source.metadata['run_id']
        token = await self.owner.connectors.slack_bot_token()
        rows = self.owner.store.rows('SELECT * FROM slack_threads WHERE run_id=?', (run_id,))
        # Resolve and recheck the immutable destination after token refresh.
        if (not rows or not self.owner.settings.slack_thread_chat_enabled
                or not self.owner.status()['enabled']
                or rows[0]['team_id'] != self.owner.connectors.slack_installation().get('team_id')
                or not threaded(rows[0]) or (status and rows[0]['paused'])):
            raise RuntimeError('Slack destination changed before working status.')
        await self.owner.connectors.request('POST', 'https://slack.com/api/assistant.threads.setStatus',
            headers={'Authorization': f'Bearer {token}'},
            json={'channel_id': rows[0]['channel'], 'thread_ts': rows[0]['thread_ts'], 'status': status})
        return True

    async def mirror(self, source, message, *, origin='Moyai web'):
        payload = mirror_payload(message, origin=origin)
        token = await self.owner.connectors.slack_bot_token()
        # Recheck after token refresh, immediately before the external send.
        binding = self.owner.store.rows('SELECT * FROM slack_threads WHERE run_id=?', (source.metadata['run_id'],))[0]
        if (binding['paused'] or not self.owner.settings.slack_thread_chat_enabled
                or not self.owner.status()['enabled']
                or binding['team_id'] != self.owner.connectors.slack_installation().get('team_id')):
            raise RuntimeError('Slack destination changed before mirroring.')
        response = await self.owner.connectors.request('POST', 'https://slack.com/api/chat.postMessage',
            headers={'Authorization': f'Bearer {token}'}, json={
                **payload, 'channel': binding['channel'],
                'thread_ts': None if binding['channel'].startswith('D') and binding['thread_ts'] == binding['started_ts'] else binding['thread_ts'],
            })
        if not response.get('ts'):
            raise RuntimeError('Slack mirror delivery could not be confirmed.')
        return Message(id=f"slack:{binding['channel']}:{response['ts']}",
                       conversation_id=source.conversation_id, channel=self.name,
                       sender=message.sender, text=message.text, role='user',
                       metadata=MappingProxyType({'slack_ts': response['ts'], 'mirrored_message_id': message.id}))

    async def acknowledge(self, run_id, message_ts):
        """React only to an accepted message in the immutable session binding."""
        rows = self.owner.store.rows('SELECT t.team_id,t.channel FROM slack_threads t JOIN slack_receipts r '
            'ON r.run_id=t.run_id AND r.team_id=t.team_id AND r.channel=t.channel '
            'WHERE t.run_id=? AND r.message_ts=? AND r.message_id IS NOT NULL', (run_id, message_ts))
        if not rows or not self.owner.status()['enabled'] or rows[0]['team_id'] != self.owner.connectors.slack_installation().get('team_id'):
            raise RuntimeError('Slack destination changed before acknowledgment.')
        token = await self.owner.connectors.slack_bot_token()
        await self.owner.connectors.request('POST', 'https://slack.com/api/reactions.add',
            headers={'Authorization': f'Bearer {token}'}, allowed_errors={'already_reacted'},
            json={'channel': rows[0]['channel'], 'timestamp': message_ts, 'name': 'eyes'})


def connect_agentchat(owner):
    channel = SlackWebhookChannel(owner)
    app = AgentChat(channels=[channel], state=SessionState(owner.store))

    @app.on_message
    async def respond(context):
        data = dict(context.message.metadata)
        await owner.accept_message(user=context.sender.id, prompt=context.message.text, **data)
        # Responses are posted later via AgentChat by the durable outbox worker.
        return None

    return app, channel
