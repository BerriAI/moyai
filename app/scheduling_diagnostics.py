"""Body-free startup and stall timings. No SQL, prompts, tokens or locals."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import time
import traceback

log = logging.getLogger('uvicorn.error.moyai.scheduling')
_database_warning_at = 0.0
_warning_lock = threading.Lock()


def record(event, **fields):
    log.info(json.dumps({'event': event, 'version': 1, **fields}))


def elapsed_ms(started):
    return round(max(0, time.monotonic() - started) * 1000, 2)


def age_ms(stamp):
    try:
        return round(max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()) * 1000, 2)
    except (ValueError, TypeError):
        return None


def slow_database(started):
    global _database_warning_at
    duration = elapsed_ms(started)
    if duration < 100:
        return
    # Bound log volume across concurrent request threads. Stack summaries
    # contain code locations only; never format source lines or query values.
    with _warning_lock:
        current = time.monotonic()
        if current < _database_warning_at:
            return
        _database_warning_at = current + 10
    try:
        asyncio.get_running_loop()
        on_event_loop = True
    except RuntimeError:
        on_event_loop = False
    stack = [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
             for frame in traceback.extract_stack(limit=8)[:-1]]
    log.warning(json.dumps({'event': 'slow_database', 'version': 1, 'duration_ms': duration,
                           'on_event_loop': on_event_loop, 'stack': stack}))


async def watch_event_loop(*, interval=1.0, threshold=0.5):
    while True:
        started, cpu = time.monotonic(), time.process_time()
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - started
        if elapsed - interval >= threshold:
            log.warning(json.dumps({'event': 'event_loop_lag', 'version': 1,
                'lag_ms': round((elapsed - interval) * 1000, 2),
                'interval_ms': round(elapsed * 1000, 2),
                'process_cpu_ms': round((time.process_time() - cpu) * 1000, 2)}))
