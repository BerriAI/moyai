"""One-time, offline repair from complete LiteLLM per-request spend exports."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.spend import money

type Json = None | bool | int | Decimal | str | list[Json] | dict[str, Json]
TERMINAL = {'completed', 'failed', 'interrupted'}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamps must include a timezone')
    return parsed.astimezone(timezone.utc)


def load_logs(paths: list[Path]) -> tuple[list[dict[str, Json]], list[str]]:
    if not paths:
        raise ValueError('At least one spend export is required')
    documents: list[Json] = []
    hashes = []
    for path in paths:
        raw = path.read_bytes()
        documents.append(json.loads(raw, parse_float=Decimal, parse_constant=Decimal))
        hashes.append(hashlib.sha256(raw).hexdigest())
    rows: list[Json] = []
    if all(isinstance(document, list) for document in documents):
        for document in documents:
            rows.extend(document)
    elif all(isinstance(document, dict) for document in documents):
        pages: dict[int, list[Json]] = {}
        counts: set[tuple[int, int, int]] = set()
        for document in documents:
            page, total, size, last = (document.get(key) for key in ('page', 'total', 'page_size', 'total_pages'))
            data = document.get('data')
            if (not all(type(value) is int for value in (page, total, size, last))
                    or page < 1 or total < 0 or size < 1 or last != (total + size - 1) // size
                    or document.get('total_is_capped') is not False or not isinstance(data, list)
                    or page in pages):
                raise ValueError('Require a complete, uncapped set of /spend/logs/v2 pages')
            pages[page] = data
            counts.add((total, size, last))
        if len(counts) != 1:
            raise ValueError('Spend pages must belong to one consistent export')
        total, _, last = counts.pop()
        if set(pages) != set(range(1, max(last, 1) + 1)):
            raise ValueError('Missing spend-log pages')
        rows = [row for page in sorted(pages) for row in pages[page]]
        if len(rows) != total:
            raise ValueError('Spend export count does not match its rows')
    else:
        raise ValueError('Use JSON row arrays or a complete set of v2 page envelopes')
    if any(not isinstance(row, dict) or not isinstance(row.get('request_id'), str)
           or not row['request_id'] for row in rows):
        raise ValueError('Require per-request spend records, not aggregates')
    if isinstance(documents[0], dict) and len({row['request_id'] for row in rows}) != len(rows):
        raise ValueError('Overlapping spend pages; obtain a stable complete export')
    return rows, sorted(hashes)


@dataclass(frozen=True)
class Receipt:
    request_id: str
    key: str
    calls: tuple[str, ...]
    moyai_ids: tuple[str, ...]
    cost: str | None
    error: str
    status: str


def receipt(row: dict[str, Json]) -> Receipt:
    metadata = row.get('metadata', {})
    if metadata is None:
        metadata = {}
    error = ''
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = None
    if not isinstance(metadata, dict):
        metadata, error = {}, 'invalid_metadata'
    nested = metadata.get('spend_logs_metadata', {})
    if not isinstance(nested, dict):
        nested, error = {}, 'invalid_metadata'
    calls = (row.get('litellm_call_id'), metadata.get('litellm_call_id'))
    ids = (metadata.get('moyai_request_id'), nested.get('moyai_request_id'))
    if any(value is not None and (not isinstance(value, str) or not value) for value in (*calls, *ids)):
        error = 'invalid_identifiers'
    cost = money(row.get('spend'))
    if cost is None:
        error = 'invalid_cost'
    elif Decimal(cost) == 0:
        # LiteLLM defaults unknown spend to zero; this is not a priced receipt.
        error = 'zero_cost_needs_review'
    status = row.get('status')
    if not isinstance(status, str) or status not in {'success', 'failure'}:
        error = 'unknown_receipt_status'
    return Receipt(row['request_id'], row.get('api_key') if isinstance(row.get('api_key'), str) else '',
                   tuple(sorted({value for value in calls if isinstance(value, str) and value})),
                   tuple(sorted({value for value in ids if isinstance(value, str) and value})), cost, error, status if isinstance(status, str) else '')


def plan(conn: sqlite3.Connection, records: list[dict[str, Json]], before: datetime,
         source_hashes: list[str], database: Path) -> dict[str, object]:
    rows = [dict(row) for row in conn.execute('SELECT * FROM model_requests ORDER BY id')]
    by_id = {row['id']: row for row in rows}
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        for identifier in (row['id'], row['gateway_id']):
            if row['key_hash'] and identifier:
                index[row['key_hash'], identifier].add(row['id'])
    grouped: dict[tuple[str, str], set[Receipt]] = defaultdict(set)
    for record in records:
        item = receipt(record)
        grouped[item.key, item.request_id].add(item)
    matches: dict[str, set[Receipt]] = defaultdict(set)
    blocked: dict[str, str] = {}
    unmatched = 0
    for variants in grouped.values():
        targets: set[str] = set()
        for item in variants:
            identifiers = (*item.calls, *item.moyai_ids) or (item.request_id,)
            for identifier in identifiers:
                targets.update(index.get((item.key, identifier), set()))
        if not targets:
            unmatched += 1
            continue
        if len(variants) != 1 or len(targets) != 1:
            blocked.update((target, 'ambiguous_receipts') for target in targets)
            continue
        item = next(iter(variants))
        target = next(iter(targets))
        row = by_id[target]
        if (any(value not in {row['id'], row['gateway_id']} for value in item.calls)
                or any(value != row['id'] for value in item.moyai_ids)):
            blocked[target] = 'conflicting_identifiers'
        matches[target].add(item)
    changes, skipped = [], []
    for row in rows:
        items = matches[row['id']]
        reason = ''
        if row['cost'] is not None:
            reason = 'already_priced'
        elif row['status'] not in TERMINAL or not row['finished_at']:
            reason = 'not_finalized'
        elif timestamp(row['created_at']) >= before:
            reason = 'outside_cutoff'
        elif row['id'] in blocked:
            reason = blocked[row['id']]
        elif not items:
            reason = 'no_matching_receipt'
        elif len(items) != 1:
            reason = 'multiple_billed_attempts'
        else:
            item = next(iter(items))
            reason = item.error
        if reason:
            skipped.append({'id': row['id'], 'reason': reason})
            continue
        changes.append({'id': row['id'], 'run_id': row['run_id'], 'gateway_id': row['gateway_id'],
                        'key_hash': row['key_hash'], 'status': row['status'],
                        'created_at': row['created_at'], 'finished_at': row['finished_at'],
                        'cost_source': row['cost_source'], 'cost': item.cost,
                        'receipt_id': item.request_id,
                        'matched_by': 'moyai_metadata' if item.moyai_ids else 'call_id' if item.calls else 'request_id'})
    scope = {'database': str(database.resolve()), 'before': before.isoformat(),
             'source_sha256': source_hashes, 'changes': changes}
    fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    amounts = [Decimal(change['cost']) for change in changes]
    with localcontext() as context:
        if amounts:
            context.prec = max(28, max(value.adjusted() for value in amounts)
                               - min(value.as_tuple().exponent for value in amounts) + len(str(len(amounts))) + 2)
        total = str(sum(amounts, Decimal(0)))
    # Keep historical credential fingerprints out of the operator report.
    public = [{key: value for key, value in change.items() if key != 'key_hash'} for change in changes]
    return {'plan_sha256': fingerprint, 'before': before.isoformat(), 'source_sha256': source_hashes,
            'source_rows': len(records), 'unmatched_receipts': unmatched, 'recoverable': len(changes),
            'recovered_spend': total, 'changes': public, 'skipped': skipped,
            'skip_counts': dict(Counter(item['reason'] for item in skipped))}


def connect(path: Path, *, apply: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def backfill(database: Path, logs: list[Path], before: datetime, *, apply: bool = False,
             expected_plan: str = '', backup: Path | None = None) -> dict[str, object]:
    records, hashes = load_logs(logs)
    if apply and (not expected_plan or backup is None):
        raise ValueError('--apply requires --expect-plan from the dry run and a new --backup path')
    with closing(connect(database, apply=apply)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        report = plan(conn, records, before, hashes, database)
        report.update(mode='apply' if apply else 'dry_run', applied=0)
        if not apply:
            return report
        if expected_plan != report['plan_sha256']:
            raise ValueError('Plan changed; run a fresh dry run and review it')
        if not report['changes']:
            return report
        # A second reader can snapshot under our write reservation, before edits.
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(connect(database)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        for change in report['changes']:
            cursor = conn.execute("UPDATE model_requests SET cost=?,cost_source='gateway_backfill' "
                                  'WHERE id=? AND cost IS NULL AND status=? AND finished_at=?',
                                  (change['cost'], change['id'], change['status'], change['finished_at']))
            if cursor.rowcount != 1:
                raise ValueError('Request changed; no backfill changes were committed')
        report.update(applied=len(report['changes']), backup=str(backup.resolve()))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Existing Moyai workspace.db')
    parser.add_argument('--logs', type=Path, action='append', required=True, help='Complete JSON export; repeat for v2 pages')
    parser.add_argument('--before', type=timestamp, required=True, help='Exclusive historical cutoff, with timezone')
    parser.add_argument('--apply', action='store_true', help='Write reviewed changes; default is read-only')
    parser.add_argument('--expect-plan', default='', help='plan_sha256 printed by the dry run')
    parser.add_argument('--backup', type=Path, help='New, private SQLite backup file required for apply')
    args = parser.parse_args()
    try:
        report = backfill(args.db, args.logs, args.before, apply=args.apply,
                          expected_plan=args.expect_plan, backup=args.backup)
    except (OSError, ValueError, sqlite3.Error):
        # Input paths, SQL errors and JSON decoder messages can contain private data.
        print('Backfill failed; no changes committed. Check export completeness, DB schema, '
              'cutoff, plan hash and a writable new backup path.', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
