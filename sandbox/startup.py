"""Retry only immutable reads while the control plane is restarting."""
import http.client
import time
import urllib.error
import urllib.request

try:
    from .broker_failure import TRANSIENT_STATUSES
except ImportError:  # Loaded by the sandbox script, outside a Python package.
    from broker_failure import TRANSIENT_STATUSES


# The local caller must outlive the relay's complete metadata reconnect window.
REPOSITORY_METADATA_BUDGET = 180


class StartupUnavailable(RuntimeError):
    def __init__(self, stage, reason):
        self.stage, self.reason = stage, reason
        super().__init__('Workspace services are temporarily unavailable. Reconnecting before starting work.')


def read_with_reconnect(request, reader, **options):
    if request.get_method() != 'GET':
        raise ValueError('Only read-only startup requests may be retried')
    return _read_with_reconnect(request, reader, **options)


def _read_with_reconnect(request, reader, *, stage, notify=None, budget=45, attempt_timeout=10,
                         opener=None, clock=None, sleep=None):
    # Callers must establish read-only semantics before entering this loop.
    opener, clock, sleep = opener or urllib.request.urlopen, clock or time.monotonic, sleep or time.sleep
    deadline, attempt = clock() + budget, 0
    while True:
        if attempt and clock() >= deadline:
            raise StartupUnavailable(stage, reason) from None
        try:
            with opener(request, timeout=min(attempt_timeout, max(1, deadline - clock()))) as response:
                result = reader(response)
            if attempt and notify:
                notify('Workspace services reconnected. Preparing the agent.')
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_STATUSES:
                raise
            reason = f'HTTP {exc.code}'
            exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead,
                http.client.RemoteDisconnected):
            reason = 'network'
        if clock() >= deadline:
            raise StartupUnavailable(stage, reason) from None
        if not attempt and notify:
            notify('Workspace services are reconnecting after an interruption. Your request will start automatically.')
        sleep(max(0, min(2 ** min(attempt, 4), deadline - clock())))
        attempt += 1
