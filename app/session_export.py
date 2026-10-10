"""Versioned session debugging exports from one consistent read snapshot."""
import json

from fastapi import HTTPException

from .db import now


def session_export(store, run_id):
    with store.connect() as connection:
        connection.begin_read()
        run = store.run(run_id, connection=connection)
        if not run or run['deleted_at']:
            raise HTTPException(404, 'Session not found.')
        # Allowlist metadata: run rows also contain internal authentication and
        # pending execution state that must never be included in a download.
        session = {key: run.get(key) for key in (
            'id', 'display_title', 'prompt', 'status', 'model', 'created_at',
            'updated_at', 'parent_run_id', 'agent_label', 'repo_url')}
        messages = store.messages(run_id, connection=connection)
        events = []
        after = 0
        while True:
            page = store.events(run_id, after=after, limit=500, connection=connection)
            if not page:
                break
            events.extend(page)
            after = page[-1]['id']
        spans = [json.loads(row['payload']) for row in connection.execute(
            'SELECT payload FROM session_trace_spans WHERE run_id=? ORDER BY span_id', (run_id,))]
        spans.sort(key=lambda span: (int(span['start_ns']), span['span_id']))
    return {
        'format': 'moyai.session', 'version': 1, 'exported_at': now(),
        'session': session, 'messages': messages, 'events': events, 'traces': spans,
        'coverage': {
            'scope': 'This session only; child agents and side chats have separate exports.',
            'traces': 'Locally retained spans only. Older executions cannot be reconstructed.',
            'content': 'Recorded model responses, tool calls/results, and public progress. '
                       'Existing trace redaction, private-tool preferences and size limits apply.',
            'reasoning': 'Hidden reasoning and system prompts are not exported. '
                         'Public progress explanations are included in events.',
            'attachments': 'Attachment metadata only; binary files are not embedded.',
            'snapshot': 'Activity after this snapshot is not included.',
        },
    }
