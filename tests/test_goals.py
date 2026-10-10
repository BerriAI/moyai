"""Real goal controller and production loop with a deterministic agent boundary."""
from types import SimpleNamespace

import pytest

from app.skills import requested_skills
from agent.continuation import ActiveTurnSteering
from agent.goals import GoalLoop, command, run_goal_conversation


@pytest.fixture
def goal(tmp_path):
    value = GoalLoop(tmp_path / 'goal.json', 'session-one')
    value.accept('/goal fix the tests and verify them')
    return value


def response(text='Next I will run the tests.', **flags):
    return dict(completed=True, final_response=text,
                messages=[{'role': 'assistant', 'content': text}], **flags)


def complete(goal, call='check'):
    return response(f'Tests passed.\n[goal:evidence:{call}] Test command exited zero.\n'
                    f"[goal:complete:{goal.state['id']}]")


@pytest.mark.parametrize('text', ['please /goal fix', '`/goal fix`', '```\n/goal fix\n```',
                                 '/goals fix', '/goal/file', '/goal:fix', 'https://x/goal'])
def test_only_exact_leading_user_command(text):
    assert command(text) is None


def test_multiline_and_help(goal):
    assert command(' /goal fix tests\nand docs ') == 'fix tests\nand docs'
    assert 'Commands:' in goal.accept('/goal')
    assert 'active' in goal.accept('/goal status')
    assert requested_skills('/goal fix tests', [{'name': 'goal'}]) == []
    assert requested_skills('/personal:goal', [{'name': 'goal'}]) == ['personal:goal']


def test_actual_loop_does_not_stop_at_normal_final_and_carries_history(goal):
    calls = []
    def run(prompt, conversation_history, system_message):
        calls.append((prompt, conversation_history, system_message))
        if len(calls) == 1:
            goal.tool_complete('edit', 'patch', {}, {'success': True})
            return response('The fix is written. I will verify next.')
        goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
        return complete(goal)
    result = run_goal_conversation(SimpleNamespace(run_conversation=run), '/goal fix', [], 'system', goal)
    assert len(calls) == 2
    assert calls[1][1][0]['content'] == 'The fix is written. I will verify next.'
    assert 'GOAL MODE' in calls[1][2]
    assert goal.state['status'] == 'completed'
    assert '[goal:complete:' not in result['final_response']


@pytest.mark.parametrize('flags', [{'interrupted': True}, {'failed': True}, {'partial': True}])
def test_interrupt_failure_and_partial_never_continue(goal, flags):
    assert goal.after_response(response(**flags)) is None
    assert goal.state['status'] == 'paused'


def test_no_goal_leaves_normal_conversation_unchanged(tmp_path):
    g = GoalLoop(tmp_path / 'goal.json', 'plain')
    result = response()
    assert run_goal_conversation(SimpleNamespace(run_conversation=lambda *a, **k: result),
                                 'hi', [], 'system', g) is result


def test_partial_with_pending_steer_does_not_restart(goal):
    result = response(partial=True, pending_steer='continue')
    run_goal_conversation(SimpleNamespace(run_conversation=lambda *a, **k: result),
                          'work', [], '', goal)
    assert not goal.active


@pytest.mark.parametrize('flags', [
    {'completed': False}, {'failed': True}, {'partial': True}, {'interrupted': True},
])
def test_stopped_turn_saves_unapplied_correction_without_restarting(goal, flags):
    result = response(pending_steer='Also check the copy button.')
    result.update(flags)
    calls = []
    def run(*args, **kwargs):
        calls.append(args[0])
        assert len(calls) == 1, 'A pending correction must not bypass a stop guard'
        return result
    returned = run_goal_conversation(SimpleNamespace(run_conversation=run), 'work', [], '', goal)
    assert calls == ['work']
    assert returned['messages'][-1] == {'role': 'user', 'content': 'Also check the copy button.'}
    assert 'pending_steer' not in returned
    assert not goal.active


def test_reused_tool_id_with_failure_cannot_use_stale_evidence(goal):
    goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
    goal.tool_complete('check', 'terminal', {}, {'exit_code': 1})
    assert goal.after_response(complete(goal))
    assert goal.active


def test_completion_requires_real_successful_tool_id_and_current_goal(goal):
    assert goal.after_response(complete(goal))
    goal.tool_complete('check', 'terminal', {}, {'exit_code': 1})
    assert goal.after_response(complete(goal))
    old = complete(goal)
    goal.accept('/goal verify a different feature')
    goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
    assert goal.after_response(old)
    assert goal.after_response(complete(goal)) is None
    assert goal.state['status'] == 'completed'


def test_blocker_requires_reason_and_is_not_completion(goal):
    marker = f"[goal:blocked:{goal.state['id']}]"
    assert goal.after_response(response(marker))
    result = response('Which staging environment should I use?\n' + marker)
    assert goal.after_response(result) is None
    assert goal.state['status'] == 'blocked'
    assert 'resume' in result['final_response']


def test_no_tool_and_repeat_guards(goal):
    for n in range(3):
        next_prompt = goal.after_response(response(f'I will do step {n}'))
    assert next_prompt is None and goal.state['status'] == 'paused'
    goal.accept('/goal resume')
    for _ in range(3):
        goal.tool_complete('read', 'read_file', {}, 'same file')
        next_prompt = goal.after_response(response('Same answer'))
    assert next_prompt is None and 'Repeated' in goal.state['reason']


def test_limits_survive_checkpoint_and_resume_resets_window(goal):
    goal.state.update(continues=25, started=0)
    goal.save()
    restored = GoalLoop(goal.path, goal.run_id, continuation=True)
    assert restored.active
    assert restored.after_response(response()) is None
    assert 'limit' in restored.state['reason']
    restored.accept('/goal resume')
    assert restored.active and restored.state['continues'] == 0
    restored.clock = lambda: restored.state['started'] + 3601
    assert restored.after_response(response()) is None
    assert 'time limit' in restored.state['reason']


def test_rotation_and_waits_preserve_active_goal_without_reentry(goal):
    assert goal.after_response(response(interrupted=True), suspended=True) is None
    goal.save()
    restored = GoalLoop(goal.path, goal.run_id, continuation=True)
    assert restored.active and restored.state['id'] == goal.state['id']
    assert not GoalLoop(goal.path, goal.run_id).active  # No silent restart after Stop.
    assert GoalLoop(goal.path, 'child', continuation=True).state is None
    assert GoalLoop(goal.path, goal.run_id, restore=False).state is None


def test_control_commands_do_not_call_model(goal):
    def forbidden(*a, **k):
        pytest.fail('Control commands must not call the model')
    for text, state in [('/goal pause', 'paused'), ('/goal status', 'paused'), ('/goal clear', None)]:
        goal.accept(text)
        result = run_goal_conversation(SimpleNamespace(run_conversation=forbidden), text, [], '', goal)
        assert result['completed']
        assert (goal.state['status'] if goal.state else None) == state


def test_missing_history_does_not_replay_original_request(goal):
    result = response()
    result['messages'] = []
    assert goal.after_response(result) is None
    assert result['failed'] and not result['completed']


def test_midturn_command_only_applies_after_native_delivery_and_once(goal):
    packet = {'input': {'id': 42, 'content': '/goal pause'}}
    relay = SimpleNamespace(control=lambda *a: packet)
    seen = []
    def accept(item):
        seen.append(item['id'])
        goal.accept(item['content'])
    steer = ActiveTurnSteering(relay, on_input=accept)
    steer.step(SimpleNamespace(steer=lambda text: False))
    assert goal.active and not seen
    agent = SimpleNamespace(steer=lambda text: True)
    steer.step(agent)
    steer.step(agent)
    assert seen == [42] and not goal.active


def test_pending_steer_has_priority_over_completion(goal):
    calls = []
    def run(*a, **k):
        calls.append(a[0])
        goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
        result = complete(goal)
        if len(calls) == 1:
            result['pending_steer'] = 'also verify docs'
        return result
    run_goal_conversation(SimpleNamespace(run_conversation=run), 'original', [], '', goal)
    assert calls == ['original', 'also verify docs']


def test_user_guidance_survives_rotation_and_invalidates_prior_claim(goal):
    stale = complete(goal)
    goal.steer('Also verify the documentation examples.')
    goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
    assert goal.after_response(stale)
    goal.save()
    restored = GoalLoop(goal.path, goal.run_id, continuation=True)
    assert 'Also verify the documentation examples.' in restored.instructions()


def test_deadline_interrupts_at_step_only_once(goal):
    stops = []
    goal.clock = lambda: goal.state['started'] + 3601
    agent = SimpleNamespace(interrupt=lambda: stops.append(True))
    goal.step(agent)
    goal.step(agent)
    assert stops == [True]
    assert goal.state['status'] == 'paused'


def test_midturn_status_does_not_auto_continue(goal):
    def run(*a, **k):
        goal.steer('/goal status')
        return response()
    result = run_goal_conversation(SimpleNamespace(run_conversation=run), 'work', [], '', goal)
    assert result['final_response'].startswith('Goal:')
    assert goal.state['continues'] == 0


def test_timeout_is_not_reset_between_goal_rounds(goal, monkeypatch):
    clock = iter([0, 1, 4])
    monkeypatch.setattr('agent.goals.time.monotonic', lambda: next(clock))
    budgets = []
    def run(*a, **k):
        budgets.append(agent.run_budget_seconds)
        goal.tool_complete('check', 'terminal', {}, {'exit_code': 0})
        return response() if len(budgets) == 1 else complete(goal)
    agent = SimpleNamespace(run_conversation=run, run_budget_seconds=10)
    run_goal_conversation(agent, 'work', [], '', goal)
    assert budgets == [9, 6]


def test_production_loop_with_real_file_and_command(goal, tmp_path):
    """Scripted model boundary, real subprocess/file verification (no cloud model)."""
    import subprocess
    import sys
    target = tmp_path / 'result.txt'
    calls = []
    def run(prompt, conversation_history, system_message):
        calls.append(prompt)
        if len(calls) == 1:
            target.write_text('goal artifact')
            goal.tool_complete('write', 'write_file', {}, {'success': True})
            return response('The artifact is written; verification is next.')
        check = subprocess.run([sys.executable, '-c',
                                'from pathlib import Path; import sys; '
                                'assert Path(sys.argv[1]).read_text() == "goal artifact"; '
                                'print("artifact verified")', str(target)],
                               capture_output=True, text=True, check=True)
        goal.tool_complete('check', 'terminal', {}, {'exit_code': check.returncode, 'output': check.stdout})
        return complete(goal)
    run_goal_conversation(SimpleNamespace(run_conversation=run), 'work', [], '', goal)
    assert len(calls) == 2 and target.read_text() == 'goal artifact'
    assert goal.state['status'] == 'completed'
