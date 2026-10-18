"""Read a Temporal history and export payload-free timing evidence. Never run a worker."""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

EVENT_FIELDS = (
    'event_id', 'at', 'type', 'scheduled_event_id', 'started_event_id',
    'activity_id', 'activity_type', 'task_queue', 'attempt', 'worker_build',
)


def micros(stamp: str) -> int:
    """Retain microseconds without float epoch rounding; reject timezone ambiguity."""
    date = datetime.fromisoformat(stamp)
    if date.tzinfo is None:
        raise ValueError('Timestamps must include a timezone')
    delta = date - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def project_history(history) -> list[dict]:
    from temporalio.api.enums.v1 import EventType

    rows = []
    for event in history.events:
        row = {'event_id': event.event_id, 'at': event.event_time.ToJsonString(),
               'type': EventType.Name(event.event_type)}
        for field, attrs in event.ListFields():
            if not field.name.endswith('_event_attributes'):
                continue
            for key in ('scheduled_event_id', 'started_event_id', 'activity_id', 'attempt'):
                if value := getattr(attrs, key, None):
                    row[key] = value
            for key in ('activity_type', 'task_queue'):
                if value := getattr(getattr(attrs, key, None), 'name', None):
                    row[key] = value
            for key in ('worker_version_stamp', 'worker_version'):
                if value := getattr(getattr(attrs, key, None), 'build_id', None):
                    row['worker_build'] = value
        rows.append(row)
    return rows


async def fetch_history(turn_id: str) -> dict:
    from temporalio.client import Client

    client = await Client.connect(
        os.environ['TEMPORAL_ADDRESS'], namespace=os.environ['TEMPORAL_NAMESPACE'],
        tls=os.getenv('TEMPORAL_TLS', 'false').lower() in ('true', '1'),
        api_key=os.getenv('TEMPORAL_API_KEY'),
    )
    history = await client.get_workflow_handle('moyai-turn-' + turn_id).fetch_history()
    return {'turn_id': turn_id, 'events': project_history(history)}


def build_report(history: dict, evidence: dict | None = None, origin: str | None = None) -> dict:
    evidence = evidence or {}
    events = [{key: event[key] for key in EVENT_FIELDS if key in event} for event in history['events']]
    events.sort(key=lambda event: (micros(event['at']), event['event_id']))
    by_id = {event['event_id']: event for event in events}
    if len(by_id) != len(events):
        raise ValueError('Duplicate history event IDs')
    origin = origin or evidence.get('origin') or (events[0]['at'] if events else None)
    if origin is None:
        raise ValueError('An empty history requires an explicit origin')
    origin_us = micros(origin)
    rows, warnings = [], []

    def add(name, category, start, end=None, **details):
        start_ms = (micros(start) - origin_us) / 1000
        end_ms = (micros(end) - origin_us) / 1000 if end else None
        if end_ms is not None and end_ms < start_ms:
            warnings.append(f'Negative interval excluded: {name}. Check clocks or history completeness.')
            return
        rows.append({'name': name, 'category': category, 'start': start, 'end': end,
                     'start_ms': start_ms, 'end_ms': end_ms,
                     'duration_ms': round(end_ms - start_ms, 3) if end_ms is not None else None,
                     **details})

    for family, label in (('ACTIVITY_TASK', 'activity'), ('WORKFLOW_TASK', 'workflow')):
        prefix = 'EVENT_TYPE_' + family + '_'
        starts = [event for event in events if event['type'] == prefix + 'STARTED']
        terminals = [event for event in events if event['type'] in {
            prefix + suffix for suffix in ('COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELED')
        }]
        matched = set()
        for start in starts:
            schedule = by_id.get(start.get('scheduled_event_id'))
            # Event order is not chronological. Join IDs rather than neighboring rows.
            end = next((event for event in terminals
                        if event.get('started_event_id') == start['event_id']), None)
            name = (schedule or {}).get('activity_type', 'Workflow task' if label == 'workflow' else 'Activity')
            ids = [event['event_id'] for event in (schedule, start, end) if event]
            details = {'event_ids': ids, 'source': 'Temporal history',
                       'queue': (schedule or {}).get('task_queue'),
                       'attempt': start.get('attempt', (schedule or {}).get('attempt')),
                       'worker_build': start.get('worker_build')}
            if schedule:
                add(name, label + '_wait', schedule['at'], start['at'],
                    note='Schedule → recorded start. Retried activities may include earlier attempts/backoff.',
                    **details)
            else:
                warnings.append(f'Missing schedule for event {start["event_id"]}.')
            add(name, label, start['at'], end['at'] if end else None,
                status=end['type'].removeprefix(prefix).lower() if end else 'incomplete', **details)
            if end:
                matched.add(end['event_id'])
        for end in terminals:
            if end['event_id'] not in matched:
                add('Unpaired ' + end['type'].removeprefix('EVENT_TYPE_'), 'event', end['at'],
                    event_ids=[end['event_id']], source='Temporal history', status='unpaired')
                warnings.append(f'No recorded start for terminal event {end["event_id"]}; duration unknown.')
        started_schedules = {event.get('scheduled_event_id') for event in starts}
        terminal_schedules = {event.get('scheduled_event_id') for event in terminals}
        for event in events:
            if event['type'] == prefix + 'SCHEDULED' and event['event_id'] not in started_schedules | terminal_schedules:
                add(event.get('activity_type', 'Workflow task'), label + '_wait', event['at'],
                    event_ids=[event['event_id']], source='Temporal history', status='incomplete')

    for event in events:
        if event['type'].startswith('EVENT_TYPE_WORKFLOW_EXECUTION_'):
            add(event['type'].removeprefix('EVENT_TYPE_'), 'event', event['at'],
                event_ids=[event['event_id']], source='Temporal history')
    for item in evidence.get('intervals', []):
        add(item['name'], 'evidence', item['start'], item['end'],
            source=item.get('source', 'Supplied evidence'), note=item.get('note', ''))
    for item in evidence.get('milestones', []):
        add(item['name'], 'milestone', item['at'], source=item.get('source', 'Supplied evidence'),
            note=item.get('note', ''), uncertainty_ms=item.get('uncertainty_ms'))
    for row in rows:
        target = evidence.get('targets', {}).get(row['name'])
        if target and row['category'] in ('activity', 'workflow', 'evidence'):
            row['target'] = {key: target[key] for key in ('label', 'basis', 'change', 'owner', 'url') if key in target}
    rows.sort(key=lambda row: (row['start_ms'], row['category']))
    return {'turn_id': history.get('turn_id'), 'label': evidence.get('label', history.get('turn_id', 'Turn')),
            'origin': origin, 'summary': evidence.get('summary', []), 'warnings': warnings,
            'rows': rows, 'events': events,
            'limitations': [
                'Durations are elapsed wall time, not CPU time. Overlapping rows must not be added.',
                'Activity internals and omitted retry attempts are not reconstructed from missing evidence.',
                'Server clocks are not proven synchronized. Decimal precision is not measurement accuracy.',
                'Workflow completion can follow answer delivery. Client paint and provider TTFT need their own evidence.',
            ]}


def chrome_trace(report: dict) -> dict:
    trace = []
    def lane_name(row):
        if row['category'].startswith('activity'):
            return f'Activity {row.get("event_ids", ["?"])[0]}: {row["name"]}'
        if row['category'].startswith('workflow'):
            return 'Workflow tasks'
        return row['category'] + ': ' + row['name']

    lanes = {name: index + 1 for index, name in enumerate(sorted({lane_name(row) for row in report['rows']}))}
    for name, lane in lanes.items():
        trace.append({'name': 'thread_name', 'ph': 'M', 'pid': 1, 'tid': lane, 'args': {'name': name}})
    for row in report['rows']:
        item = {'name': row['name'], 'cat': row['category'], 'pid': 1, 'tid': lanes[lane_name(row)],
                'ts': round(row['start_ms'] * 1000), 'args': row}
        if row['duration_ms'] is None:
            item.update(ph='i', s='t')
        else:
            item.update(ph='X', dur=round(row['duration_ms'] * 1000))
        trace.append(item)
    return {'traceEvents': trace, 'displayTimeUnit': 'ms', 'metadata': {'origin': report['origin']}}


def write_report(report: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / 'timing.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'history.json').write_text(json.dumps({'turn_id': report['turn_id'], 'events': report['events']}, indent=2) + '\n')
    (output / 'trace.json').write_text(json.dumps(chrome_trace(report)) + '\n')
    with (output / 'timing.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['name', 'category', 'start', 'end', 'start_ms', 'duration_ms',
                                                 'status', 'event_ids', 'source', 'note'], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['rows'])
    template = Path(__file__).with_name('trace_turn.html').read_text()
    # Escape script closers, including from user-supplied labels and notes.
    data = json.dumps(report).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    (output / 'index.html').write_text(template.replace('__TITLE__', html.escape(str(report['label'])))
                                    .replace('__DATA__', data))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--history', type=Path, help='Payload-free projected history from this script')
    source.add_argument('--turn-id', type=lambda value: str(UUID(value)), help='Read one history from Temporal')
    parser.add_argument('--evidence', type=Path, help='Optional origin, summary, milestones and intervals JSON')
    parser.add_argument('--origin', help='Timezone-qualified timestamp used for t=0')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    history = json.loads(args.history.read_text()) if args.history else asyncio.run(fetch_history(args.turn_id))
    evidence = json.loads(args.evidence.read_text()) if args.evidence else None
    report = build_report(history, evidence, args.origin)
    write_report(report, args.output)
    print(f'{len(report["rows"])} timing rows → {args.output / "index.html"}')


if __name__ == '__main__':
    main()
