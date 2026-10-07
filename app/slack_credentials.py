"""Signed scope choices and one durable Slack card per secure access request."""
import html
import json
import re
from decimal import Decimal
from urllib.parse import parse_qs

from fastapi import HTTPException


SCOPES = {'organization': 'Organization', 'personal': 'Personal', 'session': 'Session only'}
STAMP = re.compile(r'^\d{10,16}\.\d{1,9}$')


class SlackCredentials:
    def __init__(self, owner):
        self.owner, self.store = owner, owner.store

    def collect_in(self, conn, binding):
        vault = self.owner.manager.credentials
        pending = {row['id']: row for row in vault.pending_rows(conn, binding['run_id'], include_children=True)}
        for row in pending.values():
            self.queue_in(conn, binding, row, 'pending')
        for card in conn.execute("SELECT * FROM slack_outbox WHERE run_id=? AND json_extract(metadata,'$.credential_request_id') IS NOT NULL", (binding['run_id'],)).fetchall():
            request_id = json.loads(card['metadata'])['credential_request_id']
            if request_id not in pending:
                row = conn.execute('SELECT * FROM credential_requests WHERE id=?', (request_id,)).fetchone()
                if row:
                    self.queue_in(conn, binding, row, row['status'] if row['status'] != 'pending' else 'unavailable')

    def queue_in(self, conn, binding, request, state):
        key = 'credential:' + request['id']
        card = conn.execute('SELECT * FROM slack_outbox WHERE run_id=? AND dedupe_key=?', (binding['run_id'], key)).fetchone()
        previous = json.loads(card['metadata']) if card else {}
        bot = self.owner.connectors.slack_installation()
        destination = {'team_id': binding['team_id'], 'bot_user_id': bot.get('user_id'),
                       'channel': binding['channel'], 'thread_ts': binding['thread_ts']}
        if previous.get('credential_request_id') and any(previous.get(key) != value for key, value in destination.items()):
            return  # A reconnected app cannot adopt another installation's card.
        metadata = {**destination, 'credential_request_id': request['id'], 'generation': request['generation'],
                    'scope_revision': request['scope_revision'], 'preferred_scope': request['preferred_scope'], 'state': state,
                    'action_ts': previous.get('action_ts', '') if previous.get('generation') == request['generation'] else ''}
        text = 'Credentials requested: ' + self.owner.manager.credentials.request_label(request)
        retry = card and ((card['status'] == 'uncertain' and card['slack_ts'])
                          or (card['status'] == 'skipped' and (state == 'pending' or card['slack_ts'])))
        if not card:
            self.owner.chat.queue(conn, binding['run_id'], key, 'approval', text, metadata)
        elif metadata != previous or retry:
            conn.execute("""UPDATE slack_outbox SET text=?,metadata=?,status=CASE
                WHEN status='sending' OR (status='uncertain' AND slack_ts='') THEN status
                WHEN slack_ts='' AND ?!='pending' THEN 'skipped' ELSE 'pending' END WHERE id=?""",
                (text, json.dumps(metadata), state, card['id']))

    def current_in(self, conn, card_id, binding, identity):
        card = conn.execute('SELECT * FROM slack_outbox WHERE id=? AND run_id=?', (card_id, binding['run_id'])).fetchone()
        data = json.loads(card['metadata']) if card else {}
        destination = {'team_id': identity[0], 'bot_user_id': identity[1], 'channel': binding['channel'], 'thread_ts': binding['thread_ts']}
        root = conn.execute("SELECT 1 FROM runs WHERE id=? AND deleted_at=''", (binding['run_id'],)).fetchone()
        if not root or not data.get('credential_request_id') or any(data.get(key) != value for key, value in destination.items()):
            raise HTTPException(409, 'This Slack access card is no longer available.')
        row = conn.execute('SELECT q.* FROM credential_requests q JOIN runs r ON r.id=q.run_id WHERE q.id=? AND (r.id=? OR r.parent_run_id=?)',
                           (data['credential_request_id'], binding['run_id'], binding['run_id'])).fetchone()
        if not row:
            raise HTTPException(409, 'This Slack access card is no longer available.')
        pending = any(item['id'] == row['id'] for item in self.owner.manager.credentials.pending_rows(conn, binding['run_id'], include_children=True))
        self.queue_in(conn, binding, row, 'pending' if pending else row['status'] if row['status'] != 'pending' else 'unavailable')
        return conn.execute('SELECT * FROM slack_outbox WHERE id=?', (card_id,)).fetchone(), row

    def payload(self, card_id, binding, identity):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            card, request = self.current_in(conn, card_id, binding, identity)
            data = json.loads(card['metadata'])
        label = self.owner.manager.credentials.request_label(request)
        selected = data['preferred_scope']
        text = 'Credentials requested: ' + label
        blocks = [{'type': 'section', 'text': {'type': 'plain_text', 'text': text}}]
        if data['state'] != 'pending':
            blocks.append({'type': 'section', 'text': {'type': 'plain_text', 'text': 'This access request is ' + data['state'] + '.'}})
        else:
            description = ('Selected: ' + SCOPES[selected] + '. ' if selected else '') + 'Choose where this secret may be used. Enter its value only in the secure form.'
            blocks.append({'type': 'section', 'text': {'type': 'plain_text', 'text': description}})
            value = json.dumps({'request_id': request['id'], 'generation': request['generation']}, separators=(',', ':'))
            blocks.append({'type': 'actions', 'elements': [
                {'type': 'button', 'action_id': 'credential_scope_' + scope, 'text': {'type': 'plain_text', 'text': name}, 'value': value}
                for scope, name in SCOPES.items()]})
            if selected:
                url = (self.owner.settings.public_url.rstrip('/') + '/#run=' + request['run_id']
                       + '&credential=' + request['id'] + '&generation=' + str(request['generation']))
                blocks.append({'type': 'actions', 'elements': [{'type': 'button', 'action_id': 'credential_open',
                    'style': 'primary', 'text': {'type': 'plain_text', 'text': 'Provide Secret'}, 'url': url}]})
        return {'text': html.escape(text, quote=False), 'blocks': blocks, 'parse': 'none', 'link_names': False,
                'unfurl_links': False, 'unfurl_media': False}, card['slack_ts'], card['metadata']

    async def receive(self, request):
        body = await request.body()
        self.owner.verify_signature(body, request.headers)
        try:
            if len(body) > 65536:
                raise ValueError()
            form = parse_qs(body.decode(), strict_parsing=True, max_num_fields=4)
            if set(form) != {'payload'} or len(form['payload']) != 1:
                raise ValueError()
            payload = json.loads(form['payload'][0])
            action, = payload['actions']
            if payload['type'] != 'block_actions' or action['type'] != 'button':
                raise ValueError()
            if action['action_id'] == 'credential_open':
                return {'ok': True}  # URL navigation has no server-side effect.
            scope = action['action_id'].removeprefix('credential_scope_')
            value = json.loads(action['value'])
            team, user, channel = payload['team']['id'], payload['user']['id'], payload['channel']['id']
            timestamp, clicked = payload['container']['message_ts'], action['action_ts']
            if (action['action_id'] != 'credential_scope_' + scope or scope not in SCOPES
                    or payload['container']['type'] != 'message'
                    or set(value) != {'request_id', 'generation'} or type(value['generation']) is not int or value['generation'] < 0
                    or not re.fullmatch(r'[0-9a-f]{32}', value['request_id'])
                    or not re.fullmatch(r'[UW][A-Z0-9]{7,30}', user)
                    or not STAMP.fullmatch(timestamp) or not STAMP.fullmatch(clicked)
                    or payload['container']['channel_id'] != channel or payload['message']['ts'] != timestamp
                    or payload.get('is_ext_shared_channel') or payload['channel'].get('is_ext_shared_channel')):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack access action.') from None
        bot = self.owner.connectors.slack_installation()
        users = {item.strip() for item in self.owner.settings.slack_session_users.split(',')}
        if (not self.owner.status()['enabled'] or not self.owner.settings.slack_thread_chat_enabled
                or team != bot.get('team_id') or user == bot.get('user_id') or ('*' not in users and user not in users)):
            raise HTTPException(403, 'This Slack access action is unavailable.')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            card = conn.execute("""SELECT o.* FROM slack_outbox o JOIN slack_threads t ON t.run_id=o.run_id
                WHERE o.dedupe_key=? AND o.slack_ts=? AND t.team_id=? AND t.channel=? AND t.paused=0""",
                ('credential:' + value['request_id'], timestamp, team, channel)).fetchone()
            if not card:
                raise HTTPException(409, 'Reopen the current access request in Moyai.')
            binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (card['run_id'],)).fetchone()
            if payload['message'].get('thread_ts', binding['thread_ts']) != binding['thread_ts']:
                raise HTTPException(409, 'This Slack access card belongs to another thread.')
            card, row = self.current_in(conn, card['id'], binding, (team, bot.get('user_id')))
            data = json.loads(card['metadata'])
            if data['state'] != 'pending' or data['generation'] != value['generation']:
                raise HTTPException(409, 'This access request changed. Reopen it in Moyai.')
            actor = 'slack:' + team + ':' + user
            vault = self.owner.manager.credentials
            if not (vault.same_requester(actor, row['actor_id']) or vault.same_requester(row['actor_id'], actor)):
                raise HTTPException(403, 'Only the original requester can choose this access scope.')
            if data.get('action_ts') and Decimal(clicked) <= Decimal(data['action_ts']):
                return {'ok': True}  # The POST boundary still flushes replayed state.
            row = vault.select_scope_in(conn, row['id'], value['generation'], scope, actor)
            self.queue_in(conn, binding, row, 'pending')
            latest = json.loads(conn.execute('SELECT metadata FROM slack_outbox WHERE id=?', (card['id'],)).fetchone()[0])
            latest['action_ts'] = clicked
            conn.execute('UPDATE slack_outbox SET metadata=? WHERE id=?', (json.dumps(latest), card['id']))
        await self.owner.checkpoints.flush()
        self.owner.chat.wake.set()
        return {'ok': True}
