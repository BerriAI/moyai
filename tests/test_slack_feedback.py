import asyncio
import json
import time

import httpx
import pytest

from app.lens_feedback import LensFeedback
from test_slack import event, signed, slack_app, wait_for
from test_slack_chat import finish, receive, start


def wait_for_reply(predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Slack feedback reply was not delivered.')


def enable_feedback(app):
    app.state.settings.slack_thread_chat_enabled = True
    app.state.settings.lens_feedback_endpoint = 'https://lens.example/lens/feedback'
    app.state.settings.litellm_trace_api_key = 'trace-key'
    service = LensFeedback(
        app.state.store,
        app.state.settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    app.state.lens_feedback = service
    app.state.slack.lens_feedback = service
    app.state.slack.feedback.lens_feedback = service
    return service


def test_slack_feedback_button_is_on_the_last_chunk_and_survives_long_replies(slack_app):
    app, client, _, posted = slack_app
    service = enable_feedback(app)
    assert service.enabled
    _, _, run_id = start(slack_app)
    answer = ('A long answer with details. ' * 400)
    finish(app, run_id, answer)
    wait_for(lambda: app.state.store.rows(
        "SELECT metadata FROM slack_outbox WHERE run_id=? AND kind='answer'",
        (run_id,),
    ))
    queued = app.state.store.rows(
        "SELECT metadata FROM slack_outbox WHERE run_id=? AND kind='answer' ORDER BY id",
        (run_id,),
    )
    assistant = app.state.store.messages(run_id)[-1]
    assert json.loads(queued[-1]['metadata']).get('feedback_message_id') == assistant['id'], [
        json.loads(row['metadata']) for row in queued
    ]
    wait_for_reply(lambda: any(
        any(block.get('block_id') == 'moyai_feedback' for block in message.get('blocks', []))
        for message in posted
    ))

    answer_rows = app.state.store.rows(
        "SELECT metadata FROM slack_outbox WHERE run_id=? AND kind='answer' ORDER BY id",
        (run_id,),
    )
    metadata = [json.loads(row['metadata']) for row in answer_rows]
    assert len(metadata) > 1
    assert all('feedback_message_id' not in item for item in metadata[:-1])
    assistant = app.state.store.messages(run_id)[-1]
    assert metadata[-1]['feedback_message_id'] == assistant['id']
    rich = next(message for message in reversed(posted) if any(
        block.get('block_id') == 'moyai_feedback' for block in message.get('blocks', [])))
    action = next(block for block in rich['blocks'] if block.get('block_id') == 'moyai_feedback')
    assert json.loads(action['elements'][0]['value']) == {'run_id': run_id, 'message_id': assistant['id']}
    assert all(len(block['text']['text']) <= 3000 for block in rich['blocks'] if block['type'] == 'section')
    assert len(rich['blocks']) <= 49
    asyncio.run(service.close())


@pytest.mark.parametrize('saving', [False, True])
def test_slack_feedback_interactions_open_submit_and_preserve_credentials(slack_app, saving):
    app, client, _, posted = slack_app
    service = enable_feedback(app)
    _, _, run_id = start(slack_app)
    (receive if saving else finish)(app, run_id, 'A completed answer.')
    wait_for(lambda: any(
        any(block.get('block_id') == 'moyai_feedback' for block in message.get('blocks', []))
        for message in posted
    ))
    rich = next(message for message in reversed(posted) if any(
        block.get('block_id') == 'moyai_feedback' for block in message.get('blocks', [])))
    action = next(block['elements'][0] for block in rich['blocks'] if block.get('block_id') == 'moyai_feedback')
    assistant = app.state.store.messages(run_id)[-1]
    source = app.state.store.messages(run_id)[0]
    if saving:
        assert source['status'] == 'running'
        assert app.state.store.rows('SELECT 1 FROM trace_contexts WHERE run_id=? AND message_id=?', (run_id, source['id']))
    else:
        app.state.store.execute('INSERT INTO trace_contexts VALUES(?,?,?,?,?,?,?)', (
            run_id, source['id'], 'ab' * 16, 'cd' * 8, None, run_id, 'moyai'))
    message_ts = app.state.store.rows(
        "SELECT slack_ts FROM slack_outbox WHERE run_id=? AND kind='answer' ORDER BY id DESC LIMIT 1",
        (run_id,),
    )[0]['slack_ts']
    thread_ts = '1790719000.123456'
    action_payload = {
        'type': 'block_actions',
        'team': {'id': 'T12345678'},
        'user': {'id': 'U12345678'},
        'channel': {'id': 'C12345678'},
        'container': {'type': 'message', 'channel_id': 'C12345678', 'message_ts': message_ts},
        'message': {'ts': message_ts, 'thread_ts': thread_ts},
        'trigger_id': 'trigger-123',
        'actions': [action],
    }
    opened = client.post('/hooks/slack/interactions', **signed(action_payload, form=True))
    assert opened.status_code == 200 and opened.json() == {'ok': True}
    modal_request = next(request for request in reversed(posted)
                         if request.get('trigger_id') == 'trigger-123')
    view = modal_request['view']
    assert view['callback_id'] == 'moyai_feedback'
    assert view['title']['text'] == 'Reply feedback'
    assert len(view['blocks'][0]['element']['options']) == 11
    assert view['blocks'][1]['element']['max_length'] == 3000
    metadata = json.loads(view['private_metadata'])
    assert metadata == {'run_id': run_id, 'message_id': assistant['id'], 'channel': 'C12345678'}

    submission = {
        'type': 'view_submission',
        'team': {'id': 'T12345678'},
        'user': {'id': 'U12345678'},
        'view': {
            'callback_id': 'moyai_feedback',
            'private_metadata': view['private_metadata'],
            'state': {'values': {
                'score': {'value': {'selected_option': {'value': '9'}}},
                'comment': {'value': {'value': 'Useful answer'}},
            }},
        },
    }
    response = client.post('/hooks/slack/interactions', **signed(submission, form=True))
    assert response.status_code == 200 and response.json() == {}
    row = app.state.store.rows('SELECT * FROM lens_feedback WHERE run_id=?', (run_id,))[0]
    assert (row['message_id'], row['author'], row['score'], row['comment'], row['source']) == (
        assistant['id'], 'slack:T12345678:U12345678', 9, 'Useful answer', 'slack')
    assert any(request.get('text', '').startswith('Thanks, your feedback was saved')
               for request in posted)

    unauthorized = {**action_payload, 'team': {'id': 'TOTHER999'}}
    assert client.post('/hooks/slack/interactions', **signed(unauthorized, form=True)).status_code == 403
    app.state.settings.slack_session_users = 'U87654321'
    assert client.post('/hooks/slack/interactions', **signed(action_payload, form=True)).status_code == 403
    app.state.settings.slack_session_users = '*'
    invalid_signature = signed(action_payload, form=True)
    invalid_signature['content'] += b' '
    assert client.post('/hooks/slack/interactions', **invalid_signature).status_code == 401

    credential_action = {'type': 'block_actions', 'actions': [
        {'type': 'button', 'action_id': 'credential_open'}]}
    preserved = client.post('/hooks/slack/interactions', **signed(credential_action, form=True))
    assert preserved.status_code == 200 and preserved.json() == {'ok': True}
    asyncio.run(service.close())
