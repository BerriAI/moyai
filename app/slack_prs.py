"""Durable PR reactions, independent of agent execution and answer delivery.

Publication receipts establish that a PR exists; only a fresh GitHub read can
establish a merge. Reactions are idempotent (already_reacted is success), so a
lost Slack response can be retried without duplicating messages or agent work.
"""
import asyncio
import logging
import time

from pydantic import Field

from .connector_errors import ConnectorError
from .pr_delivery import PullRequest


logger = logging.getLogger(__name__)


class Publication(PullRequest):
    repository_id: int = Field(gt=0, strict=True)
    branch: str = Field(min_length=1)


def track_publication(conn, publication_id, installation):
    """Freeze the original thread destination atomically with the PR receipt.

    A child agent uses its parent's binding. Web-only sessions have no target;
    neither model output nor PR contents can supply a Slack destination.
    """
    if not installation.get('installed') or not installation.get('user_id'):
        return
    conn.execute('''INSERT OR IGNORE INTO slack_pr_reactions
        (publication_id,run_id,team_id,channel,thread_ts,bot_user_id)
        SELECT p.id,t.run_id,t.team_id,t.channel,t.thread_ts,?
        FROM github_publications p JOIN runs r ON r.id=p.run_id
        JOIN slack_threads t ON t.run_id=COALESCE(NULLIF(r.parent_run_id,''),r.id)
        WHERE p.id=? AND p.result!='' AND t.team_id=?''',
        (installation['user_id'], publication_id, installation.get('team_id')))


class SlackPullRequests:
    def __init__(self, owner):
        self.owner = owner
        self.store = owner.store
        self.connectors = owner.connectors
        self.github = owner.connectors.github
        self.watcher = None

    def enabled(self):
        return (self.owner.settings.slack_thread_chat_enabled
                and self.owner.status()['enabled']
                and self.connectors.allowed('slack_send')
                and self.connectors.allowed('github_pull_request')
                and 'reactions:write' in self.connectors.slack_installation().get('scopes', []))

    def guard(self, row):
        installation = self.connectors.slack_installation()
        bindings = self.store.rows('SELECT * FROM slack_threads WHERE run_id=?', (row['run_id'],))
        if (not self.enabled() or not bindings or bindings[0]['paused']
                or (installation.get('team_id'), installation.get('user_id')) != (row['team_id'], row['bot_user_id'])
                or any(bindings[0][key] != row[key] for key in ('team_id', 'channel', 'thread_ts'))
                or self.github.connection_version() != row['connection_version']):
            raise ConnectorError('The PR reaction destination or connection changed.')

    async def react(self, row, name, *, fallback=False):
        self.guard(row)
        token = await self.connectors.slack_bot_token()
        # Token refresh, GitHub reads and checkpointing can yield to disconnect,
        # sleep or a replacement installation. Recheck immediately before sends.
        for emoji in (name, 'link') if fallback and name != 'link' else (name,):
            self.guard(row)
            result = await self.connectors.request('POST', 'https://slack.com/api/reactions.add',
                headers={'Authorization': f'Bearer {token}'},
                allowed_errors={'already_reacted', 'invalid_name'},
                json={'channel': row['channel'], 'timestamp': row['thread_ts'], 'name': emoji})
            if result.get('ok') is True or result.get('error') == 'already_reacted':
                return emoji
            if result.get('error') != 'invalid_name':
                break
        raise ConnectorError('Slack did not confirm the PR reaction.')

    async def reconcile(self, row):
        self.guard(row)
        pr = Publication.model_validate_json(row['result'])
        if pr.branch != row['branch']:
            raise ConnectorError('The PR receipt does not match its publication.')
        # Issuance rechecks the selected repository and uses read-only access.
        token = await self.github.installation_token(repository=pr.repository_id)
        self.guard(row)
        if not row['opened_reaction']:
            emoji = await self.react(row, self.owner.settings.slack_pr_reaction, fallback=True)
            self.store.execute('UPDATE slack_pr_reactions SET opened_reaction=? WHERE publication_id=?',
                               (emoji, row['publication_id']))
            await self.owner.checkpoints.flush()
        data = await self.github.request('GET', f'/repositories/{pr.repository_id}/pulls/{pr.number}', token=token)
        self.guard(row)
        if (not isinstance(data, dict) or type(data.get('number')) is not int or data['number'] != pr.number
                or data.get('state') not in {'open', 'closed'} or type(data.get('merged')) is not bool
                or ((data.get('base') or {}).get('repo') or {}).get('id') != pr.repository_id
                or ((data.get('head') or {}).get('repo') or {}).get('id') != pr.repository_id
                or (data.get('head') or {}).get('ref') != pr.branch):
            raise ConnectorError('GitHub did not confirm the tracked PR identity and state.')
        if data['merged'] and data['state'] == 'closed':
            await self.react(row, 'white_check_mark')
            self.store.execute('UPDATE slack_pr_reactions SET finished=1 WHERE publication_id=?', (row['publication_id'],))
        elif data['state'] == 'closed':
            # Closed is NOT merged. Keep checking slowly in case it is reopened.
            self.store.execute('UPDATE slack_pr_reactions SET next_check=? WHERE publication_id=?',
                               (time.time() + 3600, row['publication_id']))

    async def check(self, row):
        # Persist a retry deadline before I/O, including ambiguous sends. Each
        # failed PR backs off independently and cannot starve other sessions.
        self.store.execute('UPDATE slack_pr_reactions SET next_check=? WHERE publication_id=?',
                           (time.time() + 60, row['publication_id']))
        await self.owner.checkpoints.flush()
        try:
            async with asyncio.timeout(15):
                await self.reconcile(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Slack PR reaction will retry after %s', type(exc).__name__)
        finally:
            await self.owner.checkpoints.flush()

    async def sync(self):
        if not self.enabled():
            return
        rows = self.store.rows('''SELECT s.*,p.result,p.branch,p.connection_version
            FROM slack_pr_reactions s JOIN github_publications p ON p.id=s.publication_id
            JOIN slack_threads t ON t.run_id=s.run_id
            WHERE s.finished=0 AND s.next_check<=? AND t.paused=0
            ORDER BY s.next_check,s.publication_id LIMIT 5''', (time.time(),))
        await asyncio.gather(*(self.check(row) for row in rows))

    async def watch(self):
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Slack PR watcher will retry after %s', type(exc).__name__)
            await asyncio.sleep(5)

    def recover(self):
        if not self.watcher or self.watcher.done():
            self.watcher = asyncio.create_task(self.watch())

    async def shutdown(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
