import pytest
from app.db import Store
from app.planning import message_prompt


def test_planning_intent_persists_and_deduplicates(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Design this feature', '', 'demo', [], chat_enabled=True,
                           plan_mode=True, client_id='planning-initial')
    first = store.messages(run['id'])[0]
    assert first['plan_mode'] == 1
    assert first['content'] == 'Design this feature'
    assert '/workspace/plans' in message_prompt(first)
    assert 'Do not implement' in message_prompt(first)
    with pytest.raises(ValueError, match='different content'):
        store.create_run('Design this feature', '', 'demo', [], chat_enabled=True,
                         plan_mode=False, client_id='planning-initial')
    item, created = store.enqueue_message(run['id'], 'Review the plan', 'planning-next', plan_mode=True)
    assert created
    assert store.enqueue_message(run['id'], 'Review the plan', 'planning-next', plan_mode=True)[1] is False
    with pytest.raises(ValueError):
        store.enqueue_message(run['id'], 'Review the plan', 'planning-next', plan_mode=False)
    store.enqueue_message(run['id'], 'Implement it', 'work-next')
    last = store.messages(run['id'])[-1]
    assert last['plan_mode'] == 0
    assert message_prompt(last) == 'Implement it'


from test_message_queue import queue  # noqa: F401
from test_workspace import workspace  # noqa: F401
from app.message_queue import MessageQueue


def test_plan_mode_api_and_live_steering(queue):
    app, client, run_id, first, earlier, user = queue
    response = client.post(f'/api/runs/{run_id}/messages', json={
        'content': 'Plan the rollout', 'client_id': 'planning-steer',
        'plan_mode': True, 'send_now': True})
    assert response.status_code == 202
    control = MessageQueue(app.state.store).live_control(run_id, first, [])
    assert control['input']['content'].startswith('PLAN MODE')
    assert control['input']['content'].endswith('Plan the rollout')
    message = app.state.store.messages(run_id)[-1]
    assert message['plan_mode'] == 1
    assert message['content'] == 'Plan the rollout'
    created = client.post('/api/runs', json={
        'prompt': 'Plan a feature', 'plan_mode': True, 'chat_enabled': False})
    assert created.status_code == 201
    initial = app.state.store.messages(created.json()['id'])[0]
    assert initial['plan_mode'] == 1
