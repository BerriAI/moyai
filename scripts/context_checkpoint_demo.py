"""Actual context-store execution with synthetic data and deterministic inference.

Run: uv run python -m scripts.context_checkpoint_demo --checkpoints 30
No provider calls, external writes, Temporal service or Modal credentials needed.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time

from sandbox.context_store import ContextStore, ContextUnavailable, read_records
from sandbox.harness_agent import TurnJournal


FACTS = ['Keep Escape support', 'Do not deploy', 'PR #127 already created']


def demonstrate(checkpoints=30, delay=0):
    print('Persistent context: real SQLite writes, summaries and cold restores', flush=True)
    print('Synthetic receipts + deterministic summary fixture; no live provider calls', flush=True)
    batches, sizes = [], []
    def summarize(previous, entries):
        batches.append([entry['seq'] for entry in entries])
        source = previous + json.dumps(entries)
        return '\n'.join(fact for fact in FACTS if fact in source) + f'\nCovered through {entries[-1]["seq"]}'
    with tempfile.TemporaryDirectory(prefix='moyai-context-demo-') as temporary:
        root = Path(temporary)
        path = root / 'active.sqlite3'
        store = ContextStore(path, 'demo-run')
        store.initialize([{'role': 'user', 'content': '. '.join(FACTS)}])
        for checkpoint in range(checkpoints):
            for index in range(12):
                number = checkpoint * 12 + index
                journal = TurnJournal([], f'Continue synthetic step {number}', store)
                journal.tool_started(f'echo-{number}', 'echo', {'number': number})
                journal.tool_finished(f'echo-{number}', f'receipt-{number}: ' + 'diagnostic output\n' * 1800)
            store.compact(summarize)
            before = store.state()['cursor']
            store.close()
            cold = root / ('cold.sqlite3' if checkpoint % 2 == 0 else 'active.sqlite3')
            shutil.copy2(path, cold)
            path = cold
            store = ContextStore(path, 'demo-run')
            assert store.state()['cursor'] == before
            prompt = store.history()[0]['content']
            assert all(fact in prompt for fact in FACTS)
            sizes.append(len(prompt.encode()))
            assert sizes[-1] < 48_000
            rows = store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
            print(f'Checkpoint {checkpoint + 1:02}: {rows:4} records | resume {sizes[-1]:5,} bytes | constraints preserved', flush=True)
            if delay: time.sleep(delay)
        covered = [seq for batch in batches for seq in batch]
        assert covered == list(range(1, store.state()['cursor'] + 1))
        print(f'PASS: {len(covered):,} entries summarized once; no old journal rescans', flush=True)
        assert 'receipt-0' in read_records(path, after=3, limit=1)[0]['text']
        print('PASS: first completed-action receipt is still retrievable', flush=True)

        # Demonstrate failure with enough new entries to require maintenance.
        for number in range(50):
            store.append({'role': 'assistant', 'content': f'new completed step {number}: ' + 'log ' * 800})
        saved = store.state()
        def unavailable(*args): raise TimeoutError('synthetic outage')
        try:
            store.compact(unavailable)
        except ContextUnavailable:
            assert store.state() == saved
        else:
            raise AssertionError('Expected summary failure')
        print('PASS: summary outage preserves both the last summary and cursor', flush=True)
        store.compact(summarize)
        assert store.state()['cursor'] > saved['cursor']
        print('PASS: retry processes the remaining delta; no tools are replayed', flush=True)
        report = {'cold_checkpoints': checkpoints, 'journal_records': rows,
                  'database_bytes': path.stat().st_size, 'maximum_resume_bytes': max(sizes),
                  'summary_requests': len(batches), 'constraints_preserved': True,
                  'old_receipt_retrievable': True, 'failure_recovery': True,
                  'inference': 'deterministic local fixture; not live provider validation'}
        store.close()
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoints', type=int, default=30)
    parser.add_argument('--delay', type=float, default=0, help='Pause between actual restores for a readable recording')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.checkpoints <= 200 or not 0 <= args.delay <= 2:
        parser.error('Use checkpoints 1..200 and delay 0..2')
    report = demonstrate(args.checkpoints, args.delay)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
