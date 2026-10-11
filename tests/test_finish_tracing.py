"""Optional Slack tracing must not strand a completed turn in its finish phase."""
import json

import pytest

from app.config import Settings
from app.main import create_app
from test_trace_outbox import spans


@pytest.fixture
async def finish_app(tmp_path):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, temporal_enabled=True, session_secret='test-session-secret',
                  litellm_trace_endpoint='https://traces.example/v1/traces', litellm_trace_api_key='test-trace-key')
    app = create_app(Settings(_env_file=None, **values))
    try:
        yield app
    finally:
        await app.state.tracing.close()
        await app.state.manager.modal_clients.close()
        app.state.store.close()


def finishing_slack_turn(app, messages):
    store, manager = app.state.store, app.state.manager
    run = store.create_slack_run('event-1', 'Summarize the result', [], 'C0123ABCD', '1759870000.000100',
                                 'U0AAAAAAA', team_id='T0AAAAAAA')
    store.execute('UPDATE slack_events SET context_status=?,context_json=? WHERE run_id=?', ('ready', json.dumps({
        'permalink': 'https://example.slack.com/archives/C0123ABCD/p1759870000000100',
        'messages': messages}), run['id']))
    message = store.claim_message(run['id'])
    manager.submit(run)
    state = {'phase': 'finish', 'message_id': message['id'], 'response': 'The saved answer.',
             'outcome': 'completed', 'keep_warm': False}
    manager.save(run['id'], state)
    store.update_run(run['id'], status='saving')
    return run['id'], message['id'], state


@pytest.mark.parametrize('messages,title', [
    ([{'ts': '1759870000.000100', 'text': 'Please summarize\n the result.'}], 'Please summarize the result.'),
    ([], ''),
    ([{'ts': '1759870000.000200', 'text': 'A later reply.'}], ''),
    ([{'ts': '1759870000.000100', 'text': '<@U0BBBBBBB> please summarize.'}], '@someone please summarize.'),
])
async def test_slack_finish_saves_answer_and_trace_without_requiring_mentions(finish_app, messages, title):
    app = finish_app
    store, manager = app.state.store, app.state.manager
    run_id, message_id, state = finishing_slack_turn(app, messages)

    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert store.run(run_id)['status'] == 'idle'
    saved = store.messages(run_id)
    assert [(m['role'], m['status']) for m in saved] == [('user', 'completed'), ('assistant', 'completed')]
    assert saved[-1]['content'] == 'The saved answer.'
    outbox = store.rows('SELECT * FROM trace_outbox')
    emitted = [span for row in outbox for span in spans(row['payload'])]
    assert sorted(span.name for span in emitted) == ['moyai', 'runtime.finish']
    root = next(span for span in emitted if span.name == 'moyai')
    attributes = {item.key: item.value.string_value for item in root.attributes}
    assert attributes['agent.source.type'] == 'slack'
    assert attributes['agent.source.title'] == title

    # Retry a finish whose acknowledgement was lost; neither answer nor trace
    # may duplicate, and the input must stay completed.
    manager.save(run_id, state)
    assert await manager.advance(run_id) is False
    assert len(store.messages(run_id)) == 2
    emitted = [span for row in store.rows('SELECT * FROM trace_outbox') for span in spans(row['payload'])]
    assert [span.name for span in emitted].count('moyai') == 1
    assert [span.name for span in emitted].count('runtime.finish') == 2
    assert store.rows('SELECT status FROM messages WHERE id=?', (message_id,))[0]['status'] == 'completed'


async def test_failed_trace_sql_does_not_rollback_answer_or_poison_memory_review(finish_app, monkeypatch, caplog):
    app = finish_app
    store, manager, tracing = app.state.store, app.state.manager, app.state.tracing
    run_id, message_id, _ = finishing_slack_turn(app, [])
    enqueue = tracing.outboxes[0].enqueue

    def broken_capture(span, connection=None):
        enqueue(span, connection)
        # A real SQL error after writing trace context and payload must roll
        # back those partial writes without aborting the answer transaction.
        connection.execute('SELECT FROM')

    monkeypatch.setattr(tracing.outboxes[0], 'enqueue', broken_capture)
    assert await manager.advance(run_id) is False
    assert manager.state(run_id)['phase'] == 'idle'
    assert store.run(run_id)['status'] == 'idle'
    assert [(m['role'], m['status']) for m in store.messages(run_id)] == [
        ('user', 'completed'), ('assistant', 'completed')]
    assert store.messages(run_id)[-1]['content'] == 'The saved answer.'
    assert store.rows('SELECT * FROM trace_outbox') == []
    assert store.rows('SELECT * FROM trace_contexts') == []
    assert 'Agent trace capture failed' in caplog.text
    assert 'SELECT FROM' not in caplog.text
    assert 'The saved answer.' not in caplog.text
    store.finish_message(run_id, message_id, 'The saved answer.')
    assert len(store.messages(run_id)) == 2
