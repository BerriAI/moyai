import time

from app.db import Store
from app.feedback import Feedback
from test_slack import slack_app, signed, event  # noqa: F401
from test_slack_chat import ROOT, finish, start
from test_tracing import setup as traced_setup
from test_workspace import workspace  # noqa: F401


class Security:
    pass


class Checkpoints:
    async def flush(self):
        pass


def feedback_spans(processor):
    return [s for s in processor.spans if s.name == 'human_feedback']


def test_feedback_is_attached_to_the_answers_trace_and_latest_choice_wins(tmp_path):
    store, tracing, processor, run, message = traced_setup(tmp_path)
    feedback = Feedback(store, Security(), Checkpoints())
    store.finish_message(run['id'], message['id'], 'It says hello')
    root = processor.spans[-1]
    answer = next(m for m in store.messages(run['id']) if m['role'] == 'assistant')

    feedback.record(run['id'], answer['id'], 'google:alice', 'down', 'Wrong file')
    feedback.record(run['id'], answer['id'], 'google:alice', 'down', 'Wrong file')  # unchanged: no new span
    feedback.record(run['id'], answer['id'], 'google:bob', 'up')
    feedback.record(run['id'], answer['id'], 'google:alice', 'up')

    spans = feedback_spans(processor)
    assert len(spans) == 3
    assert all(s.context.trace_id == root.context.trace_id and s.parent == root.context for s in spans)
    assert len({s.context.span_id for s in spans}) == 3
    first = spans[0].attributes
    assert first['feedback.rating'] == 'down' and first['feedback.score'] == 0
    assert first['feedback.comment'] == 'Wrong file' and first['feedback.source'] == 'web'
    assert 'not helpful' in first['output.value'] and first['input.value'] == 'It says hello'
    assert first['moyai.turn_id'] == str(message['id'])
    assert feedback.for_run(run['id'], 'google:alice') == {answer['id']: {'up': 2, 'down': 0, 'mine': 'up', 'comment': ''}}

    feedback.record(run['id'], answer['id'], 'google:alice', None)
    assert feedback_spans(processor)[-1].attributes['feedback.rating'] == 'cleared'
    assert feedback.for_run(run['id'], 'google:alice')[answer['id']]['mine'] is None


def test_feedback_rejects_user_messages_and_other_sessions(tmp_path):
    store, tracing, processor, run, message = traced_setup(tmp_path)
    feedback = Feedback(store, Security(), Checkpoints())
    other = store.create_run('Other', '', 'modal', [], chat_enabled=True)
    store.finish_message(run['id'], message['id'], 'Done')
    answer = next(m for m in store.messages(run['id']) if m['role'] == 'assistant')
    for run_id, message_id in ((run['id'], message['id']), (other['id'], answer['id'])):
        try:
            feedback.record(run_id, message_id, 'google:alice', 'up')
        except ValueError:
            continue
        raise AssertionError('feedback accepted for a non-answer')
    assert not feedback_spans(processor)


def test_feedback_without_tracing_is_still_saved(tmp_path):
    store = Store(tmp_path)
    feedback = Feedback(store, Security(), Checkpoints())
    run = store.create_run('Task', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'Answer')
    answer = store.messages(run['id'])[-1]
    assert answer['role'] == 'assistant'
    assert feedback.record(run['id'], answer['id'], 'u', 'up')['rating'] == 'up'


def test_web_feedback_endpoint_and_run_view(workspace):  # noqa: F811
    app, client = workspace
    store = app.state.store
    run = store.create_run('Task', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'Answer')
    answer = store.messages(run['id'])[-1]
    url = f"/api/runs/{run['id']}/messages/{answer['id']}/feedback"
    response = client.post(url, json={'rating': 'down', 'comment': 'Missed the PR'})
    assert response.status_code == 200
    assert response.json()['summary'] == {'up': 0, 'down': 1, 'mine': 'down', 'comment': 'Missed the PR'}
    view = client.get(f"/api/runs/{run['id']}").json()
    assert next(m for m in view['messages'] if m['id'] == answer['id'])['feedback']['mine'] == 'down'
    assert 'feedback' not in next(m for m in view['messages'] if m['role'] == 'user')
    assert client.post(f"/api/runs/{run['id']}/messages/{message['id']}/feedback", json={'rating': 'up'}).status_code == 404
    assert client.post(url, json={'rating': 'meh'}).status_code == 422
    assert client.post(url, json={'rating': None}).json()['summary'] is None
    headers = {k: v for k, v in client.headers.items() if k.lower() != 'x-csrf-token'}
    client.headers.pop('x-csrf-token')
    assert client.post(url, json={'rating': 'up'}, headers=headers).status_code in {401, 403}


def reaction(event_id, ts, kind='reaction_added', name='+1', user='U12345678', channel='C12345678'):
    return {'type': 'event_callback', 'team_id': 'T12345678', 'event_id': event_id,
            'event': {'type': kind, 'user': user, 'reaction': name,
                      'item': {'type': 'message', 'channel': channel, 'ts': ts}, 'event_ts': str(time.time())}}


def test_slack_thumbs_reactions_on_answers_become_feedback(slack_app):  # noqa: F811
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Here is the answer.')
    answer_ts = '1790719500.000100'
    app.state.store.execute("UPDATE slack_outbox SET status='sent',slack_ts=? WHERE kind='answer'", (answer_ts,))
    answer = next(m for m in app.state.store.messages(run_id) if m['role'] == 'assistant')

    def mine():
        return app.state.feedback.for_run(run_id, 'slack:T12345678:U12345678').get(answer['id'], {}).get('mine')

    assert client.post('/hooks/slack/events', **signed(reaction('EvR1', answer_ts, name='thumbsdown'))).status_code == 200
    assert mine() == 'down'
    client.post('/hooks/slack/events', **signed(reaction('EvR2', answer_ts, name='+1::skin-tone-3')))
    assert mine() == 'up'
    # Removing a different reaction leaves the current vote alone; removing it clears.
    client.post('/hooks/slack/events', **signed(reaction('EvR3', answer_ts, 'reaction_removed', 'thumbsdown')))
    assert mine() == 'up'
    client.post('/hooks/slack/events', **signed(reaction('EvR4', answer_ts, 'reaction_removed', '+1')))
    assert mine() is None
    # Other emoji, other messages and other channels are ignored and never start sessions.
    for index, payload in enumerate((reaction('EvR5', answer_ts, name='eyes'), reaction('EvR6', ROOT),
                                     reaction('EvR7', answer_ts, channel='C99999999'))):
        assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert mine() is None
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
