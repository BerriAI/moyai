"""Fast, receipt-backed sidebar projections of current GitHub PR state."""
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from cryptography.fernet import InvalidToken
from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

from .connector_errors import ConnectorError
from .github_repositories import positive_id
from .pr_delivery import PullRequest


class Receipt(PullRequest):
    repository_id: int | None = Field(default=None, gt=0, strict=True)


class Snapshot(PullRequest):
    state: Literal['open', 'closed', 'merged']
    draft: StrictBool
    review_requested: StrictBool
    created_at: str | None = None
    merged_at: str | None = None

    @field_validator('created_at', 'merged_at', mode='before')
    @classmethod
    def timestamp(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError('GitHub timestamps must be timezone-aware strings.')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('GitHub timestamps must include a timezone.')
        try:
            return parsed.astimezone(timezone.utc).isoformat()
        except OverflowError:
            raise ValueError('GitHub timestamp is outside the supported date range.') from None

    @model_validator(mode='after')
    def merge_date(self):
        if self.merged_at is not None and (self.state != 'merged'
                or (self.created_at is not None and self.merged_at < self.created_at)):
            raise ValueError('GitHub merge time does not match the PR state.')
        return self


@dataclass
class Cached:
    value: Snapshot | None = None
    fresh_until: float = 0
    retry_at: float = 0
    failed: bool = False


class SessionPullRequests:
    TTL = 60
    RETRY = 15
    MAX_PENDING = 32
    MAX_CACHE = 512

    def __init__(self, github):
        self.github, self.store = github, github.store
        self.cache = OrderedDict()
        self.pending = {}
        self.slots = asyncio.Semaphore(4)
        self.closed = False
        self.cursor = 0
        self.store.execute('''CREATE TABLE IF NOT EXISTS github_pr_snapshots (
            connection_version TEXT NOT NULL, repository_id INTEGER NOT NULL, number INTEGER NOT NULL,
            snapshot TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(connection_version,repository_id,number))''')

    def context(self):
        if not self.github.connectors.allowed('github_pull_request'):
            return None
        try:
            credentials = self.github.saved_credentials()
            if (not isinstance(credentials, dict) or credentials.get('kind') != 'github_app'
                    or not positive_id(credentials.get('installation_id'))):
                return None
            return self.github.connection_version(), credentials
        except (ConnectorError, InvalidToken, ValueError, TypeError, AttributeError):
            return None

    def _cached(self, key):
        cached = self.cache.get(key)
        if cached:
            self.cache.move_to_end(key)
            return cached
        rows = self.store.rows('''SELECT snapshot FROM github_pr_snapshots
            WHERE connection_version=? AND repository_id=? AND number=?''', key)
        if rows:
            try:
                value = Snapshot.model_validate_json(rows[0]['snapshot']) if rows[0]['snapshot'] else None
                if value and value.number != key[2]:
                    return None
                # Restored observations never claim fresh status. They do not
                # consume an in-memory slot until a refresh can be scheduled.
                return Cached(value)
            except ValidationError:
                pass
        return None

    def status(self, receipt, context):
        """Resolve a receipt through current access before reading its status."""
        if not context:
            return None, None, None
        version, credentials = context
        try:
            target = self.github.target(receipt.repository_id or receipt.repository, credentials)
        except (ConnectorError, KeyError, TypeError, ValueError):
            # Legacy migration remains owned by the normal GitHub connection.
            key = (version, 0, 0) if 'repository_ids' not in credentials else None
            return None, key, None
        key = (version, target, receipt.number)
        return target, key, self._cached(key)

    def summaries(self, run_ids):
        result = {identity: {'open': 0, 'merged': 0, 'closed': 0, 'unknown': 0,
                             'stale': False, 'label': '', 'pull_requests': []} for identity in run_ids}
        if not result:
            return result
        rows = []
        identities = list(result)
        for offset in range(0, len(identities), 400):
            batch = identities[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            rows.extend(self.store.rows(f'''SELECT p.result,r.id,r.parent_run_id FROM github_publications p
                JOIN runs r ON r.id=p.run_id LEFT JOIN runs parent ON parent.id=r.parent_run_id
                WHERE r.deleted_at='' AND (r.parent_run_id='' OR parent.deleted_at='')
                AND (r.id IN ({marks}) OR r.parent_run_id IN ({marks})) ORDER BY p.created_at,p.id''',
                [*batch, *batch]))
        context = self.context()
        parsed, receipt_ids = [], {}
        for row in rows:
            try:
                receipt = Receipt.model_validate_json(row['result'])
            except ValidationError:
                continue
            parsed.append((row, receipt))
            if receipt.repository_id:
                for run_id in {row['id'], row['parent_run_id']} & result.keys():
                    receipt_ids.setdefault((run_id, receipt.url.casefold()), set()).add(receipt.repository_id)
        seen = {identity: set() for identity in result}
        clock = time.monotonic()
        wanted = []
        # Prefer permanent receipts before considering their legacy duplicates.
        # This affects presentation only; target() still owns every access check.
        for row, receipt in sorted(parsed, key=lambda entry: entry[1].repository_id is None):
            target, key, cached = self.status(receipt, context)
            if key:
                wanted.append(key)
            snapshot = cached.value if cached else None
            stale = bool(cached and (cached.failed or (snapshot and cached.fresh_until <= clock)))
            item = {**receipt.model_dump(exclude={'repository_id'}),
                    'state': 'unknown', 'draft': None, 'review_requested': False, 'stale': stale}
            if snapshot:
                item.update(snapshot.model_dump())
            for run_id in {row['id'], row['parent_run_id']} & result.keys():
                candidates = receipt_ids.get((run_id, receipt.url.casefold()), set())
                legacy_id = next(iter(candidates)) if len(candidates) == 1 else None
                identity = (target or receipt.repository_id or legacy_id or receipt.repository.casefold(), receipt.number)
                if identity in seen[run_id]:
                    continue
                seen[run_id].add(identity)
                summary = result[run_id]
                summary[item['state']] += 1
                summary['stale'] |= stale
                summary['pull_requests'].append(item)
        self.queue_refreshes(wanted)
        for summary in result.values():
            ready = [pr for pr in summary['pull_requests'] if pr['state'] == 'open' and not pr['draft']]
            if ready and not summary['stale'] and not summary['unknown']:
                summary['label'] = 'Review PR' if any(pr['review_requested'] for pr in ready) else 'PR is ready'
        return result

    def queue_refreshes(self, keys):
        keys = list(dict.fromkeys(keys))
        if keys and len(self.pending) < self.MAX_PENDING:
            start = self.cursor % len(keys)
            for offset in range(len(keys)):
                position = (start + offset) % len(keys)
                self.schedule(keys[position])
                self.cursor = position + 1
                if len(self.pending) >= self.MAX_PENDING:
                    break

    def schedule(self, key):
        cached = self._cached(key)
        if (self.closed or key in self.pending or len(self.pending) >= self.MAX_PENDING
                or (cached and cached.retry_at > time.monotonic())):
            return
        if key not in self.cache:
            if len(self.cache) >= self.MAX_CACHE:
                expired = next((entry for entry, value in self.cache.items()
                                if entry not in self.pending and value.retry_at <= time.monotonic()), None)
                if expired is None:
                    return  # Do not evict cooldowns and defeat the poll rate limit.
                self.cache.pop(expired)
            self.cache[key] = cached or Cached()
        task = asyncio.create_task(self.refresh(key))
        self.pending[key] = task
        task.add_done_callback(lambda finished: self.finished(key, finished))

    def finished(self, key, task):
        if self.pending.get(key) is task:
            self.pending.pop(key)
        cached = self.cache.get(key)
        if cached and not cached.retry_at:  # Includes cancellation before the first coroutine step.
            self.cache.pop(key)

    async def refresh(self, key):
        version, target, number = key
        previous = self.cache.get(key, Cached())
        try:
            async with self.slots, asyncio.timeout(10):
                current = self.context()
                if not current or current[0] != version:
                    return
                credentials = await self.github.ensure_connection()
                if not target:  # Legacy connection migration; next poll uses its new identity.
                    value = None
                else:
                    self.github.target(target, credentials)
                    token = await self.github.installation_token(credentials, repository=target)
                    current = self.context()
                    if not current or current[0] != version:
                        return
                    data = await self.github.request('GET', f'/repositories/{target}/pulls/{number}', token=token, missing=True)
                    value = self.snapshot(data, target, number) if data is not None else None
                    if value:
                        self.github.remember_repository(data['base']['repo'], credentials)
                current = self.context()
                if not current or current[0] != version:
                    return
                if target:
                    # Persist a missing observation too, so a restart cannot
                    # resurrect a previously visible PR after a verified 404.
                    self.store.execute('''INSERT INTO github_pr_snapshots VALUES(?,?,?,?,?)
                        ON CONFLICT(connection_version,repository_id,number) DO UPDATE SET
                        snapshot=excluded.snapshot,observed_at=excluded.observed_at''',
                        (*key, value.model_dump_json() if value else '', datetime.now(timezone.utc).isoformat()))
                clock = time.monotonic()
                self.cache[key] = Cached(value, clock + self.TTL, clock + self.TTL)
        except (ConnectorError, InvalidToken, ValidationError, KeyError, TypeError, ValueError, AttributeError, TimeoutError):
            self.cache[key] = Cached(previous.value, previous.fresh_until, time.monotonic() + self.RETRY, True)
        finally:
            self.finished(key, asyncio.current_task())

    @staticmethod
    def snapshot(data, target, number):
        repo = data['base']['repo']
        reviewers, teams, merged = data['requested_reviewers'], data['requested_teams'], data['merged']
        if (repo['id'] != target or type(repo['id']) is not int or data['number'] != number
                or type(merged) is not bool or data['state'] not in {'open', 'closed'}
                or (merged and data['state'] != 'closed')
                or not all(isinstance(items, list) and all(isinstance(item, dict) and positive_id(item.get('id'))
                           for item in items) for items in (reviewers, teams))):
            raise ValueError('GitHub did not confirm the expected PR state.')
        return Snapshot(number=data['number'], repository=repo['full_name'], url=data['html_url'],
                        title=data['title'], state='merged' if merged else data['state'], draft=data['draft'],
                        review_requested=bool(reviewers or teams),
                        created_at=data.get('created_at'), merged_at=data.get('merged_at'))

    async def close(self):
        self.closed = True
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
