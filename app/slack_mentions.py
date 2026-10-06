"""Display-only Slack mentions. Names never create users or grant personal access."""
import asyncio
import re
import time


MENTION = re.compile(r'<@([UW][A-Z0-9]{7,30})(?:\|[^<>\n]*)?>')
MAX_MENTIONS = 50


def profile_name(value, user):
    if not isinstance(value, str):
        return ''
    name = ' '.join(value.split())[:160]
    return '' if name in {user, 'Slack ' + user} else name


class SlackMentions:
    def __init__(self, store):
        self.store = store
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS slack_mention_names (
                    team_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', next_check REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(team_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS slack_message_mentions (
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    team_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    PRIMARY KEY(message_id,team_id,user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_slack_mention_due ON slack_mention_names(team_id,next_check);
                CREATE INDEX IF NOT EXISTS idx_slack_mention_messages ON slack_message_mentions(team_id,user_id);
            ''')

    @staticmethod
    def queue_in(conn, message_id, team, content):
        if not re.fullmatch(r'T[A-Z0-9]{7,30}', team or ''):
            return
        users = list(dict.fromkeys(MENTION.findall(content)))[:MAX_MENTIONS]
        conn.executemany('INSERT OR IGNORE INTO slack_mention_names(team_id,user_id) VALUES(?,?)',
                         [(team, user) for user in users])
        conn.executemany('INSERT OR IGNORE INTO slack_message_mentions VALUES(?,?,?)',
                         [(message_id, team, user) for user in users])

    def decorate(self, messages):
        # Receipt-backed provenance only: never reinterpret web text or assistant
        # examples as Slack markup. This also discovers pre-upgrade history.
        sources = [(message, message.pop('slack_message_team')) for message in messages]
        sources = [(message, team) for message, team in sources
                   if team and message['role'] == 'user' and MENTION.search(message['content'])]
        if not sources:
            return
        names = {}
        with self.store.connect() as conn:
            for message, team in sources:
                self.queue_in(conn, message['id'], team, message['content'])
                for row in conn.execute('''SELECT n.*,u.name AS sender_name,u.email AS sender_email
                    FROM slack_message_mentions r JOIN slack_mention_names n USING(team_id,user_id)
                    LEFT JOIN users u ON u.id='slack:'||n.team_id||':'||n.user_id
                    WHERE r.message_id=? AND r.team_id=?''', (message['id'], team)):
                    names[team, row['user_id']] = (profile_name(row['sender_name'], row['user_id'])
                        or row['name'] or profile_name(row['sender_email'], row['user_id']))
        for message, team in sources:
            content = message.get('display_content', message['content'])
            displayed = MENTION.sub(lambda match: '@' + names[team, match[1]]
                if names.get((team, match[1])) else match[0], content)
            if displayed != content:
                message['display_content'] = displayed

    @staticmethod
    def enabled(connectors, team):
        bot = connectors.slack_installation()
        return (bot.get('installed') and bot.get('team_id') == team
                and 'users:read' in bot.get('scopes', []) and connectors.policy('slack')['enabled'])

    async def sync_due(self, connectors):
        team = connectors.slack_installation().get('team_id')
        if not self.enabled(connectors, team):
            return False
        rows = self.store.rows('''SELECT n.* FROM slack_mention_names n WHERE team_id=? AND next_check<=?
            AND EXISTS(SELECT 1 FROM slack_message_mentions r JOIN messages m ON m.id=r.message_id
                WHERE r.team_id=n.team_id AND r.user_id=n.user_id AND m.status!='deleted')
            ORDER BY next_check,user_id LIMIT 10''', (team, time.time()))
        for row in rows:
            name = row['name']
            retry_after = 300
            try:
                async with asyncio.timeout(8):
                    token = await connectors.slack_bot_token()
                    result = await connectors.request('GET', 'https://slack.com/api/users.info',
                        headers={'Authorization': f'Bearer {token}'}, params={'user': row['user_id']},
                        allowed_errors=('user_not_found', 'missing_scope', 'ratelimited'))
                if not self.enabled(connectors, team):
                    return bool(rows)
                user = result.get('user')
                if (not result.get('ok') or not isinstance(user, dict)
                        or user.get('id') != row['user_id'] or user.get('team_id') != team):
                    raise ValueError('Mention profile did not match')
                profile = user.get('profile') or {}
                resolved = (profile_name(profile.get('real_name'), row['user_id'])
                        or profile_name(profile.get('display_name'), row['user_id'])
                        or profile_name(user.get('real_name'), row['user_id']))
                if not resolved:
                    raise ValueError('Mention profile has no name')
                name = resolved
                retry_after = 3600
            except Exception:
                # Keep the last known name (or the original ID); do not guess.
                pass
            self.store.execute('UPDATE slack_mention_names SET name=?,next_check=? WHERE team_id=? AND user_id=?',
                               (name, time.time() + retry_after, team, row['user_id']))
            if name != row['name']:
                runs = self.store.rows('''SELECT DISTINCT m.run_id FROM slack_message_mentions r
                    JOIN messages m ON m.id=r.message_id
                    WHERE r.team_id=? AND r.user_id=? AND m.status!='deleted' ''', (team, row['user_id']))
                for run in runs:
                    self.store.event(run['run_id'], 'chat', 'Slack mention names updated')
        return bool(rows)
