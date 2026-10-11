"""Authenticated provenance for our Slack posts, including bot events without user IDs."""
import hashlib
import hmac
import json


def signature(secret, team, channel, text):
    payload = json.dumps(['moyai.slack.v1', team, channel, text], ensure_ascii=False, separators=(',', ':'))
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def metadata(connectors, channel, text):
    team = connectors.slack_installation().get('team_id')
    return {'event_type': 'moyai_message', 'event_payload': {
        'signature': signature(connectors.security.secret, team, channel, text)}}


def is_self(connectors, message, channel):
    bot = connectors.slack_installation()
    if message.get('user') and message['user'] == bot.get('user_id'):
        return True
    if message.get('bot_id') and message['bot_id'] == bot.get('bot_id'):
        return True
    author_app = message.get('app_id') or (message.get('bot_profile') or {}).get('app_id')
    if author_app and author_app == bot.get('app_id'):
        return True
    data = message.get('metadata') or {}
    received = (data.get('event_payload') or {}).get('signature')
    if data.get('event_type') != 'moyai_message' or not isinstance(received, str):
        return False
    return hmac.compare_digest(received, signature(connectors.security.secret,
        bot.get('team_id'), channel, message.get('text') or ''))
