from app.db import Store
from app.goals import Goals


def test_durable_goal_is_read_independently_of_event_page(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Keep verifying the UI', '', 'demo', [], chat_enabled=True, user_id='owner')
    source = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='token')
    store.goals = Goals(store)
    state = store.goals.exchange(store.run(run['id']), {'action': 'create', 'source_input_id': source['id'], 'objective': 'Verify the UI'})['goal']
    store.event(run['id'], 'status', 'Old stale goal event', {'phase': 'goal', 'goal_version': 1, 'goal': None})
    assert store.goal(run['id']) == state
    store.close()
