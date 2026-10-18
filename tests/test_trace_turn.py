import json

import pytest

from scripts.trace_turn import build_report, chrome_trace, micros, write_report


def event(identity, offset, kind, **fields):
    return {'event_id': identity, 'at': f'2026-10-17T00:00:{offset:02d}Z',
            'type': 'EVENT_TYPE_' + kind, **fields}


def test_pair_by_ids_when_history_and_timestamps_are_out_of_order():
    history = {'events': [
        event(1, 0, 'WORKFLOW_EXECUTION_STARTED'),
        event(2, 1, 'ACTIVITY_TASK_SCHEDULED', activity_type='ensure', task_queue='tools'),
        event(3, 3, 'ACTIVITY_TASK_SCHEDULED', activity_type='catalog', task_queue='control'),
        event(4, 2, 'ACTIVITY_TASK_STARTED', scheduled_event_id=2),
        event(5, 6, 'ACTIVITY_TASK_COMPLETED', scheduled_event_id=2, started_event_id=4),
        event(6, 4, 'ACTIVITY_TASK_STARTED', scheduled_event_id=3),
        event(7, 7, 'ACTIVITY_TASK_COMPLETED', scheduled_event_id=3, started_event_id=6),
    ]}
    report = build_report(history)
    activities = [row for row in report['rows'] if row['category'] == 'activity']
    assert [(row['name'], row['start_ms'], row['duration_ms']) for row in activities] == [
        ('ensure', 2000, 4000), ('catalog', 4000, 3000),
    ]
    assert activities[0]['end_ms'] > activities[1]['start_ms']  # Preserve overlap.
    assert report['warnings'] == []
    spans = [row for row in chrome_trace(report)['traceEvents'] if row.get('cat') == 'activity']
    assert spans[0]['tid'] != spans[1]['tid']  # Concurrent operations need separate trace lanes.


def test_workflow_failure_and_replay_are_distinct_from_model_retry():
    history = {'events': [
        event(1, 0, 'WORKFLOW_TASK_SCHEDULED'),
        event(2, 1, 'WORKFLOW_TASK_STARTED', scheduled_event_id=1),
        event(3, 4, 'WORKFLOW_TASK_FAILED', scheduled_event_id=1, started_event_id=2),
        event(4, 4, 'WORKFLOW_TASK_SCHEDULED'),
        event(5, 5, 'WORKFLOW_TASK_STARTED', scheduled_event_id=4),
        event(6, 6, 'WORKFLOW_TASK_COMPLETED', scheduled_event_id=4, started_event_id=5),
    ]}
    rows = build_report(history)['rows']
    assert [(row['status'], row['duration_ms']) for row in rows if row['category'] == 'workflow'] == [
        ('failed', 3000), ('completed', 1000),
    ]
    assert not any(row['category'] == 'activity' for row in rows)


def test_missing_start_and_unfinished_activity_do_not_invent_duration():
    report = build_report({'events': [
        event(1, 0, 'ACTIVITY_TASK_SCHEDULED', activity_type='timeout'),
        event(2, 5, 'ACTIVITY_TASK_TIMED_OUT', scheduled_event_id=1),
        event(3, 6, 'ACTIVITY_TASK_SCHEDULED', activity_type='ongoing'),
        event(4, 7, 'ACTIVITY_TASK_STARTED', scheduled_event_id=3, attempt=2),
        event(5, 8, 'ACTIVITY_TASK_SCHEDULED', activity_type='pending'),
    ]})
    assert report['warnings']
    ongoing = next(row for row in report['rows'] if row['name'] == 'ongoing' and row['category'] == 'activity')
    assert ongoing['duration_ms'] is None
    assert ongoing['status'] == 'incomplete'
    assert ongoing['attempt'] == 2
    assert any(row['name'] == 'pending' and row['duration_ms'] is None for row in report['rows'])


def test_answer_completion_is_separate_from_workflow_cleanup_and_payloads_are_removed(tmp_path):
    history = {'turn_id': 'test', 'secret': 'do not copy', 'events': [
        event(1, 0, 'WORKFLOW_EXECUTION_STARTED', payload='secret prompt'),
        event(2, 10, 'WORKFLOW_EXECUTION_COMPLETED'),
    ]}
    report = build_report(history, {'label': '<script>bad()</script>', 'milestones': [
        {'name': 'Answer saved', 'at': '2026-10-17T00:00:05Z'},
    ]})
    assert [row['start_ms'] for row in report['rows']] == [0, 5000, 10000]
    write_report(report, tmp_path)
    saved = (tmp_path / 'timing.json').read_text()
    assert 'secret prompt' not in saved
    assert 'do not copy' not in saved
    page = (tmp_path / 'index.html').read_text()
    assert '<script>bad()' not in page
    trace = json.loads((tmp_path / 'trace.json').read_text())
    assert any(row.get('ts') == 5_000_000 for row in trace['traceEvents'])


def test_precision_and_clock_disagreement():
    assert micros('2026-10-17T00:00:00.123456789Z') - micros('2026-10-17T00:00:00Z') == 123456
    with pytest.raises(ValueError, match='timezone'):
        micros('2026-10-17T00:00:00')
    report = build_report({'events': []}, {'origin': '2026-10-17T00:00:00Z', 'intervals': [
        {'name': 'clock mismatch', 'start': '2026-10-17T00:00:02Z', 'end': '2026-10-17T00:00:01Z'},
    ]})
    assert report['rows'] == []
    assert report['warnings']
    assert chrome_trace(report)['traceEvents'] == []


def test_target_assumptions_are_explicit_and_whitelisted():
    report = build_report({'events': []}, {
        'origin': '2026-10-17T00:00:00Z',
        'intervals': [{'name': 'catalog', 'start': '2026-10-17T00:00:00Z', 'end': '2026-10-17T00:00:10Z'}],
        'targets': {'catalog': {'label': '100–500 ms', 'basis': 'Unbenchmarked hypothesis', 'secret': 'omit'}},
    })
    assert report['rows'][0]['target'] == {'label': '100–500 ms', 'basis': 'Unbenchmarked hypothesis'}
