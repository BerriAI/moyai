import asyncio

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.lens_feedback import FeedbackNotConfigured, LensFeedback
from app.tracing import AgentTracing
from test_workspace import workspace


class Processor:
    def on_end(self, span):
        pass

    def shutdown(self):
        pass


def setup_feedback(tmp_path):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, litellm_trace_endpoint='https://gateway.example/v1/traces',
                        litellm_trace_api_key='trace-key')
    store.tracing = AgentTracing(store, settings, Processor())
    service = LensFeedback(store, settings)
    run = store.create_run('First question', '', 'modal', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    return store, settings, service, run, first


def use_client(service, handler):
    asyncio.run(service.client.aclose())
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_trace_resolution_ignores_injected_steering_and_uses_latest_turn(tmp_path):
    store, _, service, run, first = setup_feedback(tmp_path)
    first_trace = store.rows(
        'SELECT trace_id FROM trace_contexts WHERE run_id=? AND message_id=?',
        (run['id'], first['id']),
    )
    assert not first_trace
    steering, _ = store.enqueue_message(run['id'], 'Steer this answer', 'steering', user_id='actor', send_now=True)
    store.execute('UPDATE messages SET status=?,steering_parent_id=? WHERE id=?',
                  ('injected', first['id'], steering['id']))
    store.finish_message(run['id'], first['id'], 'First answer')
    first_assistant = next(m for m in store.messages(run['id']) if m['role'] == 'assistant')
    with store.connect() as conn:
        assert service.trace_for(conn, run['id'], first_assistant['id']) is not None
    first_trace_id = store.rows(
        'SELECT trace_id FROM trace_contexts WHERE run_id=? AND message_id=?',
        (run['id'], first['id']),
    )[0]['trace_id']
    with store.connect() as conn:
        assert service.trace_for(conn, run['id'], first_assistant['id']) == first_trace_id

    second, _ = store.enqueue_message(run['id'], 'Second question', 'second', user_id='actor')
    second_input = store.claim_message(run['id'])
    assert second_input['id'] == second['id']
    store.finish_message(run['id'], second['id'], 'Second answer')
    second_assistant = store.messages(run['id'])[-1]
    second_trace_id = store.rows(
        'SELECT trace_id FROM trace_contexts WHERE run_id=? AND message_id=?',
        (run['id'], second['id']),
    )[0]['trace_id']
    with store.connect() as conn:
        assert service.trace_for(conn, run['id'], second_assistant['id']) == second_trace_id
    assert second_trace_id != first_trace_id
    # Old saved assistants have no explicit source identity.
    store.execute('UPDATE messages SET response_to_id=NULL WHERE id=?', (second_assistant['id'],))
    with store.connect() as conn:
        assert service.trace_for(conn, run['id'], second_assistant['id']) == second_trace_id
    asyncio.run(service.close())
    store.close()


def test_early_feedback_uses_exact_turn_even_with_queued_and_inline_inputs(tmp_path):
    from app.runner import RunManager
    store, settings, service, run, first = setup_feedback(tmp_path)
    store.enqueue_message(run['id'], 'Next question', 'next')
    with store.connect() as conn:
        conn.begin_write()
        store.enqueue_message_in(conn, run['id'], 'Session ID?', 'inline', metadata_request=True, restore_archived=False)
    manager = RunManager(store, settings)
    result = {'message_id': first['id'], 'message': 'First answer', 'completed': True}
    manager.receive_result(run['id'], result)
    assistant = next(m for m in store.messages(run['id']) if m.get('response_to_id') == first['id'])
    assert assistant['status'] == 'saving'
    service.submit(run['id'], assistant['id'], 'person@example.com', 8, 'Useful', 'web')
    feedback = store.rows('SELECT trace_id FROM lens_feedback')[0]
    trace = store.rows('SELECT trace_id FROM trace_contexts WHERE message_id=?', (first['id'],))[0]
    assert feedback == trace
    store.finish_message(run['id'], first['id'], 'First answer')
    manager.receive_result(run['id'], {**result, 'checkpoint_saved': True})
    assert next(m for m in store.messages(run['id']) if m['id'] == assistant['id'])['status'] == 'completed'
    assert len([m for m in store.messages(run['id']) if m.get('response_to_id') == first['id']]) == 1
    asyncio.run(service.close())
    store.close()


def test_submit_validates_author_score_source_and_upserts_by_reply_and_author(tmp_path):
    store, _, service, run, first = setup_feedback(tmp_path)
    store.finish_message(run['id'], first['id'], 'Answer')
    assistant = store.messages(run['id'])[-1]

    for score in (True, -1, 11, 1.0):
        with pytest.raises(ValueError):
            service.submit(run['id'], assistant['id'], 'person@example.com', score, '', 'web')
    with pytest.raises(ValueError):
        service.submit(run['id'], assistant['id'], ' ', 5, '', 'web')
    with pytest.raises(ValueError):
        service.submit(run['id'], assistant['id'], 'author', 5, 'x' * 10001, 'web')
    with pytest.raises(ValueError):
        service.submit(run['id'], assistant['id'], 'author', 5, '', 'invalid')
    with pytest.raises(LookupError):
        service.submit(run['id'], first['id'], 'author', 5, '', 'web')

    result = service.submit(run['id'], assistant['id'], ' author ', 7, '  useful  ', 'web')
    assert result == {'score': 7, 'comment': 'useful', 'status': 'pending'}
    store.execute('UPDATE lens_feedback SET failed=1,attempts=4,last_error=? WHERE run_id=?',
                  ('HTTP 500', run['id']))
    updated = service.submit(run['id'], assistant['id'], 'author', 9, 'changed', 'slack')
    assert updated == {'score': 9, 'comment': 'changed', 'status': 'pending'}
    row = store.rows('SELECT * FROM lens_feedback WHERE run_id=?', (run['id'],))[0]
    assert row['attempts'] == 0 and row['failed'] == 0 and row['last_error'] == ''
    assert row['delivered_at'] is None and row['next_attempt_at'] == 0
    assert service.for_messages(run['id'], 'author') == {
        assistant['id']: {'score': 9, 'comment': 'changed', 'status': 'pending'}}
    asyncio.run(service.close())
    store.close()


def test_submit_is_disabled_without_a_feedback_target(tmp_path):
    store = Store(tmp_path)
    settings = Settings(_env_file=None)
    service = LensFeedback(store, settings)
    with pytest.raises(FeedbackNotConfigured, match='not configured'):
        service.submit('missing', 1, 'author', 5, '', 'web')
    asyncio.run(service.close())
    store.close()


def test_delivery_sends_lens_contract_and_marks_200_delivered(tmp_path):
    store, _, service, run, message = setup_feedback(tmp_path)
    store.finish_message(run['id'], message['id'], 'Answer')
    assistant = store.messages(run['id'])[-1]
    service.submit(run['id'], assistant['id'], 'person@example.com', 8, 'Helpful', 'web')
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'score': 8})

    use_client(service, respond)
    assert asyncio.run(service.deliver_once())
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://gateway.example/lens/feedback'
    assert request.headers['Authorization'] == 'Bearer trace-key'
    assert httpx.Request('PUT', 'https://gateway.example/lens/feedback',
                         json={'trace_id': store.rows(
                             'SELECT trace_id FROM lens_feedback WHERE run_id=?', (run['id'],))[0]['trace_id'],
                               'score': 8, 'comment': 'Helpful', 'user': 'person@example.com'}).read() == request.read()
    assert service.for_messages(run['id'], 'person@example.com')[assistant['id']]['status'] == 'delivered'
    asyncio.run(service.close())
    store.close()


def test_delivery_retries_404_then_delivers_and_422_fails_terminally(tmp_path):
    store, _, service, run, message = setup_feedback(tmp_path)
    store.finish_message(run['id'], message['id'], 'Answer')
    assistant = store.messages(run['id'])[-1]
    service.submit(run['id'], assistant['id'], 'author', 5, '', 'web')
    statuses = iter([404, 200])

    async def respond(_):
        return httpx.Response(next(statuses))

    use_client(service, respond)
    asyncio.run(service.deliver_once())
    assert store.rows('SELECT attempts,next_attempt_at FROM lens_feedback WHERE run_id=?',
                      (run['id'],))[0]['attempts'] == 1
    store.execute('UPDATE lens_feedback SET next_attempt_at=0 WHERE run_id=?', (run['id'],))
    asyncio.run(service.deliver_once())
    assert service.for_messages(run['id'], 'author')[assistant['id']]['status'] == 'delivered'

    service.submit(run['id'], assistant['id'], 'other', 4, '', 'web')
    use_client(service, lambda _: httpx.Response(422))
    asyncio.run(service.deliver_once())
    failed = store.rows('SELECT failed,last_error,attempts FROM lens_feedback WHERE author=?', ('other',))[0]
    assert (failed['failed'], failed['last_error'], failed['attempts']) == (1, 'HTTP 422', 0)
    asyncio.run(service.close())
    store.close()


def test_delivery_gives_up_after_24_hours(tmp_path):
    store, _, service, run, message = setup_feedback(tmp_path)
    store.finish_message(run['id'], message['id'], 'Answer')
    assistant = store.messages(run['id'])[-1]
    service.submit(run['id'], assistant['id'], 'author', 5, '', 'web')
    store.execute('UPDATE lens_feedback SET updated_at=updated_at-86401 WHERE run_id=?', (run['id'],))
    calls = []
    use_client(service, lambda request: (calls.append(request), httpx.Response(200))[1])
    assert not asyncio.run(service.deliver_once())
    assert calls == []
    assert service.for_messages(run['id'], 'author')[assistant['id']]['status'] == 'failed'
    asyncio.run(service.close())
    store.close()


def test_authenticated_web_feedback_api_persists_and_returns_current_actor_feedback(tmp_path):
    import time
    from fastapi.testclient import TestClient

    from app.main import create_app
    from test_spend import sign_in

    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
                       litellm_trace_endpoint='https://gateway.example/v1/traces',
                       litellm_trace_api_key='trace-key')
    app = create_app(settings)
    with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 50000)) as client:
        service = app.state.lens_feedback
        use_client(service, lambda _: httpx.Response(200, json={'ok': True}))
        actor = sign_in(app, client)
        run = app.state.store.create_run('Feedback test', '', 'modal', [], chat_enabled=True, user_id=actor)
        prompt = app.state.store.claim_message(run['id'])
        app.state.store.execute('INSERT INTO trace_contexts VALUES(?,?,?,?,?,?,?)',
                                (run['id'], prompt['id'], 'ab' * 16, 'cd' * 8, None, run['id'], 'moyai'))
        app.state.store.finish_message(run['id'], prompt['id'], 'A useful answer')
        assistant = app.state.store.messages(run['id'])[-1]
        endpoint = f"/api/runs/{run['id']}/messages/{assistant['id']}/feedback"
        assert client.post(endpoint, json={'score': True}).status_code == 422
        assert client.post(endpoint, json={'score': 8, 'comment': '  useful  '}).status_code == 200
        response = client.get(f"/api/runs/{run['id']}")
        assert response.status_code == 200
        payload = response.json()
        assert payload['feedback_enabled'] is True
        saved = next(message for message in payload['messages'] if message['id'] == assistant['id'])
        assert saved['feedback']['score'] == 8 and saved['feedback']['comment'] == 'useful'
        assert saved['feedback']['status'] in {'pending', 'delivered'}
        for _ in range(100):
            if service.for_messages(run['id'], 'alice@berri.ai')[assistant['id']]['status'] == 'delivered':
                break
            time.sleep(0.01)
        assert service.for_messages(run['id'], 'alice@berri.ai')[assistant['id']]['status'] == 'delivered'
        assert client.post(f"/api/runs/{run['id']}/messages/{prompt['id']}/feedback",
                           json={'score': 4}).status_code == 404
        app.state.store.execute('UPDATE runs SET deleted_at=? WHERE id=?', ('deleted', run['id']))
        assert client.post(endpoint, json={'score': 4}).status_code == 404
        client.cookies.clear()
        assert client.post(endpoint, json={'score': 4}).status_code == 401


def test_web_feedback_api_reports_disabled_feature_as_conflict(workspace):
    from test_spend import sign_in

    app, client = workspace
    actor = sign_in(app, client)
    run = app.state.store.create_run('Feedback test', '', 'modal', [], chat_enabled=True, user_id=actor)
    prompt = app.state.store.claim_message(run['id'])
    app.state.store.finish_message(run['id'], prompt['id'], 'An answer')
    assistant = app.state.store.messages(run['id'])[-1]
    response = client.post(f"/api/runs/{run['id']}/messages/{assistant['id']}/feedback",
                           json={'score': 4})
    assert response.status_code == 409
    assert response.json()['detail'] == 'Lens feedback is not configured.'
