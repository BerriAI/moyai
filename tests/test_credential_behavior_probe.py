"""Verify the live probe against real activity serialization, not a hand-shaped receipt."""
import pytest

from agent.activity import ActivityReporter
from agent.skills import catalog, read
from scripts.credential_behavior_probe import access_skill_reads


def receipts(change=None):
    entry = next(item for item in catalog() if item['name'] == 'access-discovery')
    result = read(entry['name'], entry['revision'])
    result.update(change or {})
    events = []
    reporter = ActivityReporter(lambda kind, message, data: events.append({
        'kind': kind, 'data': data}))
    args = {'name': entry['name'], 'revision': entry['revision']}
    reporter.start('skill-call', 'bundled_skill_read', args)
    reporter.complete('skill-call', 'bundled_skill_read', args, result)
    return entry, events


def test_live_probe_accepts_full_skill_receipt_from_actual_activity_formatter():
    entry, events = receipts()
    assert isinstance(events[-1]['data']['output'], str)
    assert access_skill_reads(events) == [{'name': entry['name'], 'revision': entry['revision']}]


@pytest.mark.parametrize('change', [
    {'name': 'coding'}, {'path': 'references/partial.md'}, {'offset': 10},
    {'next_offset': 100}, {'content': ''}, {'error': 'Rejected revision'},
])
def test_live_probe_does_not_count_unrelated_partial_or_failed_reads(change):
    _, events = receipts(change)
    assert access_skill_reads(events) == []
