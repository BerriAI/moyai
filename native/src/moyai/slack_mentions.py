"""Display-only names for receipt-backed native Slack messages.

Reuse the product's workspace-scoped name cache, without enrolling mentioned
people as users or changing authored text, turn prompts, or personal access.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app.db import database as source_database
from app.slack_mentions import MAX_MENTIONS, MENTION, profile_name

logger = logging.getLogger(__name__)


class SlackMentionNames:
    def __init__(self, slack):
        self.slack = slack
        self.lock = asyncio.Lock()

    def cached(self, team: str, users: list[str]) -> dict:
        targets = ','.join('(?)' for _ in users)
        rows = self.slack.store.rows(f'''WITH targets(user_id) AS (VALUES {targets})
            SELECT targets.user_id,n.name,n.next_check,u.name AS sender_name
            FROM targets LEFT JOIN slack_mention_names n ON n.team_id=? AND n.user_id=targets.user_id
            LEFT JOIN users u ON u.id='slack:'||?||':'||targets.user_id''', (*users, team, team))
        return {row['user_id']: row for row in rows}

    async def names(self, team: str, users: list[str]) -> dict[str, str]:
        rows = await source_database(self.cached, team, users)
        names = {user: name for user, row in rows.items()
                 if (name := profile_name(row['sender_name'], user) or profile_name(row['name'], user))}
        due = [user for user, row in rows.items() if not profile_name(row['sender_name'], user)
               and (row['next_check'] or 0) <= time.time()]
        if not due:
            return names
        try:
            # One bounded lookup window per page, including concurrent readers.
            # Cached names remain usable during provider failures/rate limits.
            async with asyncio.timeout(8), self.lock:
                installed = await source_database(self.slack.installation)
                if installed['team_id'] != team or 'users:read' not in installed.get('scopes', []):
                    return names
                rows = await source_database(self.cached, team, users)
                slots = asyncio.Semaphore(5)

                async def resolve(user, row):
                    async with slots:
                        name, retry = profile_name(row['name'], user), 300
                        # Reserve the retry window before I/O, including timeouts.
                        await source_database(self.slack.store.execute, '''
                            INSERT INTO slack_mention_names(team_id,user_id,name,next_check) VALUES(?,?,?,?)
                            ON CONFLICT(team_id,user_id) DO UPDATE SET next_check=excluded.next_check
                        ''', (team, user, name, time.time() + retry))
                        try:
                            token = await self.slack.connectors.slack_bot_token()
                            result = await self.slack.connectors.request(
                                'GET', 'https://slack.com/api/users.info',
                                headers={'Authorization': 'Bearer ' + token}, params={'user': user},
                                allowed_errors=('user_not_found', 'missing_scope', 'ratelimited'),
                            )
                            profile = result.get('user')
                            if (not result.get('ok') or not isinstance(profile, dict)
                                    or profile.get('id') != user or profile.get('team_id') != team):
                                raise ValueError('Mention profile did not match')
                            details = profile.get('profile') or {}
                            name = (profile_name(details.get('real_name'), user)
                                    or profile_name(details.get('display_name'), user)
                                    or profile_name(profile.get('real_name'), user))
                            if not name:
                                raise ValueError('Mention profile has no name')
                            retry = 3600
                        except Exception:  # noqa: BLE001 - display failures never block the transcript
                            name = profile_name(row['name'], user)
                        current = await source_database(self.slack.installation)
                        if current['authority'] != installed['authority']:
                            return
                        await source_database(self.slack.store.execute, '''
                            INSERT INTO slack_mention_names(team_id,user_id,name,next_check) VALUES(?,?,?,?)
                            ON CONFLICT(team_id,user_id) DO UPDATE SET name=excluded.name,next_check=excluded.next_check
                        ''', (team, user, name, time.time() + retry))
                        if name:
                            names[user] = name

                # Bound cold provider work, not the displayed cached names.
                # Subsequent reads can resolve the next batch after this one.
                pending = [user for user, row in rows.items() if not profile_name(row['sender_name'], user)
                           and (row['next_check'] or 0) <= time.time()][:MAX_MENTIONS]
                names.update({user: name for user, row in rows.items()
                              if (name := profile_name(row['sender_name'], user) or profile_name(row['name'], user))})
                async with asyncio.TaskGroup() as tasks:
                    for user in pending:
                        tasks.create_task(resolve(user, rows[user]))
        except Exception as error:  # noqa: BLE001 - keep cached names when the provider is unavailable
            logger.debug('Slack mention lookup unavailable (%s)', type(error).__name__)
        return names

    async def decorate(self, messages: list[dict], sources: dict[int, dict | None]) -> bool:
        groups: dict[str, list[tuple[dict, list[str]]]] = {}
        for message in messages:
            source = sources.get(message['id']) or {}
            team = source.get('team_id', '')
            if (message['role'] != 'user' or source.get('kind') != 'slack' or source.get('deleted')
                    or not re.fullmatch(r'T[A-Z0-9]{7,30}', team)):
                continue
            users = list(dict.fromkeys(MENTION.findall(message['content'])))[:MAX_MENTIONS]
            if users:
                groups.setdefault(team, []).append((message, users))
        for team, entries in groups.items():
            users = list(dict.fromkeys(user for _, ids in entries for user in ids))
            try:
                names = await self.names(team, users)
            except Exception as error:  # noqa: BLE001 - optional display cache must not hide messages
                logger.debug('Slack mention cache unavailable (%s)', type(error).__name__)
                continue
            for message, ids in entries:
                message['slack_mentions'] = {user: names[user] for user in ids if user in names}
        return bool(groups)
