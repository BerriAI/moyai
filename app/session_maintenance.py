"""Keep Access sign-in accounting off the browser's bootstrap response path."""
import asyncio
import logging

from .db import database, now

log = logging.getLogger(__name__)
MAX_PENDING_ACCOUNTS = 256
SHUTDOWN_GRACE_SECONDS = 2


class SessionMaintenance:
    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.worker = None
        self.closing = False

    def schedule(self, identity, actor):
        # Called on the request loop, only with its freshly verified mapping.
        # One worker limits database/thread consumption during writer waits;
        # repeated page loads retain at most one queued update per account.
        if self.closing:
            return
        key = (identity['issuer'], identity['sub'])
        if key not in self.pending and len(self.pending) >= MAX_PENDING_ACCOUNTS:
            log.warning('Session identity maintenance is full; the next session read will retry.')
            return
        self.pending[key] = (dict(identity), actor, now())
        if self.worker is None:
            self.worker = asyncio.create_task(self.flush())

    async def flush(self):
        try:
            while self.pending:
                key = next(iter(self.pending))
                identity, actor, seen_at = self.pending.pop(key)
                try:
                    await database(self.store.touch_access_identity, identity, actor, seen_at)
                except Exception:
                    # Accounting is best effort after the response. A later
                    # bootstrap retries it; do not log identity/claim contents.
                    log.warning('Session identity maintenance failed; the next session read will retry.')
        finally:
            self.worker = None

    async def close(self):
        self.closing = True
        if self.worker:
            worker = self.worker
            try:
                await asyncio.wait_for(asyncio.shield(worker), SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                # Do not serialize a full queue of database lock timeouts during
                # shutdown. Remaining accounting retries on the next bootstrap.
                # The one in-flight transaction still drains safely, bounded by
                # the database's pool, lock and statement timeouts.
                self.pending.clear()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
