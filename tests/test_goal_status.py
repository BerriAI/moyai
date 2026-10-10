from agent.goals import GoalLoop
from app.db import Store


def test_public_goal_lifecycle_and_timer(tmp_path):
    timestamp = [100.0]
    emitted = []
    goal = GoalLoop(tmp_path / 'goal.json', 'test', clock=lambda: timestamp[0], on_change=emitted.append)
    goal.accept('/goal verify the UI')
    assert emitted[-1]['status'] == 'active'
    assert emitted[-1]['active_since'] == 100
    timestamp[0] = 112
    goal.accept('/goal pause')
    assert emitted[-1]['elapsed_seconds'] == 12
    assert emitted[-1]['active_since'] is None
    timestamp[0] = 150
    goal.accept('/goal resume')
    assert emitted[-1]['elapsed_seconds'] == 12
    timestamp[0] = 155
    goal.tool_complete('test', 'terminal', {}, {'exit_code': 0})
    goal.after_response({'completed': True, 'final_response':
        f"[goal:evidence:test] Tests passed.\n[goal:complete:{goal.state['id']}]"})
    assert emitted[-1]['status'] == 'completed'
    assert emitted[-1]['elapsed_seconds'] == 17
    assert 'evidence' not in emitted[-1]
    goal.save()
    restored = GoalLoop(goal.path, 'test', on_change=emitted.append)
    restored.publish()
    assert emitted[-1]['elapsed_seconds'] == 17
    goal.accept('/goal clear')
    assert emitted[-1] is None


def test_latest_goal_is_read_independently_of_event_page(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Check goals', '', 'demo', [])
    def publish(value):
        store.event(run['id'], 'status', 'Goal status', {'phase':'goal', 'goal_version':1, 'goal':value})
    goal = GoalLoop(tmp_path / 'goal.json', run['id'], on_change=publish)
    goal.accept('/goal verify the UI')
    assert store.goal(run['id'])['objective'] == 'verify the UI'
    store.event(run['id'], 'status', 'Other activity')
    assert store.goal(run['id'])['status'] == 'active'
    goal.accept('/goal pause')
    assert store.goal(run['id'])['status'] == 'paused'
    goal.accept('/goal clear')
    assert store.goal(run['id']) is None
