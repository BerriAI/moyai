"""Measure real admission against disposable SQLite state, without cloud calls.

Run: uv run python -m scripts.admission_benchmark --history 1000 --occupied 100
The occupied machines are synthetic; the admission code and database are real.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from app.config import Settings
from app.db import Store, now
from app.durable_runner import DurableRunner


def populate(store, history, occupied):
    stamp = now()
    with store.connect() as conn:
        for index in range(history + occupied):
            run_id = f'fixture-{index}'
            conn.execute("""INSERT INTO runs
                (id,prompt,repo_url,mode,status,plugins,created_at,updated_at)
                VALUES(?,'Synthetic capacity benchmark','','modal',?,'[]',?,?)""",
                (run_id, 'idle' if index < history else 'running', stamp, stamp))
            conn.execute('INSERT INTO durable_sessions(run_id,state) VALUES(?,?)',
                (run_id, json.dumps({'phase': 'idle' if index < history else 'monitor'})))


async def measure(history, occupied):
    with tempfile.TemporaryDirectory(prefix='moyai-admission-') as directory:
        settings = Settings(_env_file=None, data_dir=Path(directory), max_concurrent_runs=100)
        store = Store(settings.data_dir)
        manager = DurableRunner(store, settings)
        populate(store, history, occupied)
        queries = 0
        rows_read = 0
        original = store.rows

        def counted(sql, params=(), **kwargs):
            nonlocal queries, rows_read
            rows = original(sql, params, **kwargs)
            queries += 1
            rows_read += len(rows)
            return rows

        store.rows = counted
        started = time.perf_counter()
        # The same lock/entry point used by actual new sessions.
        async with manager.admission('new-fixture') as admitted:
            pass
        elapsed = time.perf_counter() - started
        return {'history': history, 'occupied': occupied, 'limit': 100,
                'admitted': admitted, 'elapsed_ms': round(elapsed * 1000, 3),
                'queries': queries, 'rows_returned_to_python': rows_read}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=int, default=1000)
    parser.add_argument('--occupied', type=int, default=100)
    args = parser.parse_args()
    if args.history < 0 or not 0 <= args.occupied <= 100:
        parser.error('history must be nonnegative and occupied must be between 0 and 100')
    print(json.dumps(asyncio.run(measure(args.history, args.occupied)), indent=2))


if __name__ == '__main__':
    main()
