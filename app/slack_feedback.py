"""Signed Slack actions for rating assistant replies."""
import json
import re
from urllib.parse import parse_qs

from fastapi import HTTPException

from .lens_feedback import FeedbackNotConfigured

RUN_ID = re.compile(r'[0-9a-f]{32}')
USER_ID = re.compile(r'[UW][A-Z0-9]{7,30}')
CHANNEL_ID = re.compile(r'[CDG][A-Z0-9]{7,30}')
STAMP = re.compile(r'\d{10,16}\.\d{1,9}')


class SlackFeedback:
    def __init__(self, owner, lens_feedback):
        self.owner, self.store = owner, owner.store
        self.lens_feedback = lens_feedback

    async def receive(self, request):
        body = await request.body()
        self.owner.verify_signature(body, request.headers)
        return await self.receive_payload(self.parse_payload(body))

    @staticmethod
    def parse_payload(body):
        try:
            if len(body) > 65536:
                raise ValueError()
            form = parse_qs(body.decode(), strict_parsing=True, max_num_fields=4)
            if set(form) != {'payload'} or len(form['payload']) != 1:
                raise ValueError()
            payload = json.loads(form['payload'][0])
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack access action.') from None
        return payload

    async def receive_payload(self, payload):
        if payload.get('type') == 'block_actions' and self.feedback_action(payload):
            return await self.open(payload)
        if payload.get('type') == 'view_submission':
            view = payload.get('view')
            if isinstance(view, dict) and view.get('callback_id') == 'moyai_feedback':
                return await self.submit(payload)
        return await self.owner.access.receive_payload(payload)

    @staticmethod
    def feedback_action(payload):
        actions = payload.get('actions')
        return isinstance(actions, list) and any(
            isinstance(action, dict) and action.get('action_id') == 'feedback_open'
            for action in actions
        )

    @staticmethod
    def parse_value(action):
        try:
            value = json.loads(action['value'])
            if (not isinstance(value, dict) or set(value) != {'run_id', 'message_id'}
                    or not isinstance(value['run_id'], str) or not RUN_ID.fullmatch(value['run_id'])
                    or type(value['message_id']) is not int or value['message_id'] < 1):
                raise ValueError()
            return value['run_id'], value['message_id']
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack feedback action.') from None

    def action_identity(self, payload):
        try:
            action, = [item for item in payload['actions'] if item['action_id'] == 'feedback_open']
            run_id, message_id = self.parse_value(action)
            team = payload['team']['id']
            user = payload['user']['id']
            channel = payload['channel']['id']
            container = payload['container']
            message = payload['message']
            clicked_ts = container['message_ts']
            if (action.get('type') != 'button' or container.get('type') != 'message'
                    or container.get('channel_id') != channel
                    or message.get('ts') != clicked_ts
                    or message.get('thread_ts') is None
                    or not isinstance(team, str) or not isinstance(user, str)
                    or not isinstance(channel, str) or not isinstance(clicked_ts, str)
                    or not USER_ID.fullmatch(user) or not CHANNEL_ID.fullmatch(channel)
                    or not STAMP.fullmatch(clicked_ts)
                    or payload.get('is_ext_shared_channel')
                    or payload['channel'].get('is_ext_shared_channel')):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack feedback action.') from None
        return team, user, channel, run_id, message_id, clicked_ts, message['thread_ts']

    def binding(self, team, user, channel, run_id, message_id, clicked_ts=None, thread_ts=None):
        bot = self.owner.connectors.slack_installation()
        allowed = {item.strip() for item in self.owner.settings.slack_session_users.split(',')}
        if (not self.owner.status()['enabled'] or not self.owner.settings.slack_thread_chat_enabled
                or team != bot.get('team_id') or user == bot.get('user_id')
                or ('*' not in allowed and user not in allowed)):
            raise HTTPException(403, 'This Slack feedback action is unavailable.')
        if not self.lens_feedback or not self.lens_feedback.enabled:
            raise HTTPException(409, 'Lens feedback is not configured.')
        rows = self.store.rows('''
            SELECT t.* FROM slack_threads AS t JOIN runs AS r ON r.id=t.run_id
            WHERE t.run_id=? AND t.team_id=? AND t.channel=?
              AND r.deleted_at='' AND r.deletion_requested_at=''
        ''', (run_id, team, channel))
        if not rows or (thread_ts is not None and rows[0]['thread_ts'] != thread_ts):
            raise HTTPException(409, 'This Slack reply is no longer available.')
        if not STAMP.fullmatch(rows[0]['thread_ts']):
            raise HTTPException(409, 'This Slack reply is no longer available.')
        if clicked_ts is None:
            outbox = self.store.rows('''
                SELECT id FROM slack_outbox
                WHERE run_id=? AND kind='answer' AND status='sent'
                  AND json_number(metadata,'feedback_message_id')=?
            ''', (run_id, message_id))
        else:
            outbox = self.store.rows('''
                SELECT id FROM slack_outbox
                WHERE run_id=? AND kind='answer' AND status='sent'
                  AND json_number(metadata,'feedback_message_id')=? AND slack_ts=?
            ''', (run_id, message_id, clicked_ts))
        if not outbox:
            raise HTTPException(409, 'This Slack reply is no longer available.')
        return rows[0]

    async def open(self, payload):
        team, user, channel, run_id, message_id, clicked_ts, thread_ts = self.action_identity(payload)
        binding = self.binding(team, user, channel, run_id, message_id, clicked_ts, thread_ts)
        try:
            trigger_id = payload['trigger_id']
            if not isinstance(trigger_id, str) or not trigger_id:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, 'Invalid Slack feedback action.') from None
        options = [{
            'text': {'type': 'plain_text', 'text': str(score)},
            'value': str(score),
        } for score in range(11)]
        token = await self.owner.connectors.slack_bot_token()
        response = await self.owner.connectors.request(
            'POST',
            'https://slack.com/api/views.open',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'trigger_id': trigger_id,
                'view': {
                    'type': 'modal',
                    'callback_id': 'moyai_feedback',
                    'private_metadata': json.dumps({
                        'run_id': run_id,
                        'message_id': message_id,
                        'channel': channel,
                    }),
                    'title': {'type': 'plain_text', 'text': 'Reply feedback'},
                    'submit': {'type': 'plain_text', 'text': 'Send'},
                    'close': {'type': 'plain_text', 'text': 'Cancel'},
                    'blocks': [
                        {
                            'type': 'input',
                            'block_id': 'score',
                            'label': {'type': 'plain_text', 'text': 'Score from 0 to 10'},
                            'element': {
                                'type': 'static_select',
                                'action_id': 'value',
                                'placeholder': {'type': 'plain_text', 'text': 'Choose a score'},
                                'options': options,
                            },
                        },
                        {
                            'type': 'input',
                            'block_id': 'comment',
                            'optional': True,
                            'label': {'type': 'plain_text', 'text': 'Comment (optional)'},
                            'element': {
                                'type': 'plain_text_input',
                                'action_id': 'value',
                                'multiline': True,
                                'max_length': 3000,
                            },
                        },
                    ],
                },
            },
        )
        if response.get('ok') is not True:
            raise HTTPException(502, 'Slack could not open the feedback form.')
        return {'ok': True}

    @staticmethod
    def private_metadata(view):
        try:
            data = json.loads(view['private_metadata'])
            if (not isinstance(data, dict) or set(data) != {'run_id', 'message_id', 'channel'}
                    or not isinstance(data['run_id'], str) or not RUN_ID.fullmatch(data['run_id'])
                    or type(data['message_id']) is not int or data['message_id'] < 1
                    or not isinstance(data['channel'], str) or not CHANNEL_ID.fullmatch(data['channel'])):
                raise ValueError()
            return data
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack feedback submission.') from None

    @staticmethod
    def state_values(view):
        try:
            values = view['state']['values']
            score_value = values['score']['value']['selected_option']['value']
            comment_value = values.get('comment', {}).get('value', {}).get('value', '')
            if (not isinstance(score_value, str) or not re.fullmatch(r'(?:[0-9]|10)', score_value)
                    or (comment_value is not None and not isinstance(comment_value, str))):
                raise ValueError()
            return int(score_value), comment_value or ''
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack feedback submission.') from None

    async def submit(self, payload):
        view = payload['view']
        data = self.private_metadata(view)
        try:
            team = payload['team']['id']
            user = payload['user']['id']
            score, comment = self.state_values(view)
            if (not isinstance(team, str) or not isinstance(user, str) or not USER_ID.fullmatch(user)
                    or payload.get('is_ext_shared_channel')):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, 'Invalid Slack feedback submission.') from None
        binding = self.binding(team, user, data['channel'], data['run_id'], data['message_id'])
        actor = f'slack:{team}:{user}'
        identity = self.store.rows('SELECT email FROM users WHERE id=?', (actor,))
        author = identity[0]['email'] or actor if identity else actor
        try:
            self.lens_feedback.submit(data['run_id'], data['message_id'], author, score, comment, 'slack')
        except FeedbackNotConfigured:
            return {'response_action': 'errors', 'errors': {'score': 'Lens feedback is not configured.'}}
        except LookupError:
            return {'response_action': 'errors', 'errors': {'score': 'This reply is no longer available.'}}
        except ValueError as exc:
            field = 'comment' if 'comment' in str(exc).lower() else 'score'
            return {'response_action': 'errors', 'errors': {field: str(exc)}}
        await self.owner.checkpoints.flush()
        try:
            token = await self.owner.connectors.slack_bot_token()
            await self.owner.connectors.request(
                'POST',
                'https://slack.com/api/chat.postEphemeral',
                headers={'Authorization': f'Bearer {token}'},
                json={
                    'channel': data['channel'],
                    'user': user,
                    'thread_ts': binding['thread_ts'],
                    'text': "Thanks, your feedback was saved and will appear on this reply's Lens trace",
                },
            )
        except Exception:
            pass
        return {}
