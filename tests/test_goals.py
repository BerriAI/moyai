"""Durable goal contracts with fake time and real database/journal boundaries."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent.goals import GoalLoop, command, run_goal_conversation
from agent.goal_state import advance, create, floor_unmet, fallback_report
from app.db import Store
from app.goals import Goals, GoalArgs
from app.session_journal import SessionJournal


@pytest.fixture
def env(tmp_path):
    now = [100.0]
    store = Store(tmp_path / 'server')
    run = store.create_run('Work for 3 hours and report.', '', 'demo', [], chat_enabled=True, user_id='owner')
    source = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='capability')
    caller = store.run(run['id'])
    service = store.goals = Goals(store, clock=lambda: now[0])
    journal = SessionJournal(store)
    journal.exchange(caller, {'action': 'import', 'manifest': {'id': 'empty', 'count': 0}, 'entries': [], 'complete': True})
    def relay_goal(body):
        result = service.exchange(caller, body, internal=True)
        if body['action'] == 'report':
            result['report'] = fallback_report(result['goal'])
        return result
    env = SimpleNamespace(store=store, caller=caller, service=service, now=now, source=source,
                          relay=SimpleNamespace(goal=relay_goal), journal=journal, path=tmp_path / 'goal.json')
    yield env
    store.close()


def register(env, **budget):
    return env.service.exchange(env.caller, {'action': 'create', 'source_input_id': env.source['id'],
        'objective': 'Verify every requirement and report.', 'budget': budget})['goal']


def pulse(env, seconds, segment='machine1'):
    return env.service.exchange(env.caller, {'action': 'tick', 'segment': segment, 'elapsed': seconds}, internal=True)['goal']


def evidence(env, text='Tests passed but deployment was not verified.'):
    return env.journal.exchange(env.caller, {'action': 'append', 'entries': [{'id': text,
        'message': {'role': 'tool', 'tool_call_id': 'check', 'content': text}}]})['seq']


def response(text='Progress', **flags):
    return dict(completed=True, final_response=text, messages=[{'role': 'assistant', 'content': text}], **flags)


def new_input(env, text):
    message, _ = env.store.enqueue_message(env.caller['id'], text, '', user_id='owner')
    env.store.execute("UPDATE messages SET status='injected',steering_parent_id=? WHERE id=?", (env.source['id'], message['id']))
    return message['id']


@pytest.mark.parametrize('text', ['please /goal fix', '`/goal fix`', '/goals fix', '/goal/file', 'https://x/goal'])
def test_source_commands_are_not_inferred_from_quoted_text(text):
    assert command(text) is None


def test_model_interprets_natural_language_and_no_hidden_limit(env):
    state = register(env)
    assert state['budget'] == {'kind': 'outcome'}
    assert pulse(env, 100000)['status'] == 'active'
    assert pulse(env, 100000)['elapsed_seconds'] == 100000
    # A restarted process does not need a local file and does not pause work.
    restored = GoalLoop(env.path, env.caller['id'], relay=env.relay)
    assert restored.active and restored.state['elapsed_seconds'] == 100000


def test_three_hours_restart_near_boundary_and_failed_final_generation(env):
    register(env, kind='timebox', seconds=10800)
    seq = evidence(env)
    env.service.exchange(env.caller, {'action': 'progress', 'note': 'Verified the first part.', 'remaining': 'Verify deployment.', 'evidence': [seq]})
    assert pulse(env, 10799)['status'] == 'active'
    env.now[0] += 3600  # Computer is absent: no active time is banked.
    assert pulse(env, 0, 'machine2')['elapsed_seconds'] == 10799
    assert pulse(env, 1, 'machine2')['status'] == 'wrapping'
    def broken(body):
        if body['action'] == 'report':
            raise ConnectionError('final inference failed')
        return env.relay.goal(body)
    goal = GoalLoop(env.path, env.caller['id'], relay=SimpleNamespace(goal=broken))
    result = response(interrupted=True)
    assert goal.after_response(result) is None
    assert result['completed'] and not result['interrupted']
    assert 'Completion has not been verified' in result['final_response']
    saved = env.store.finish_message(env.caller['id'], env.source['id'], result['final_response'])
    assert 'Verify deployment.' in saved
    assert env.store.goal(env.caller['id'])['status'] == 'budget_exhausted'
    assert env.store.messages(env.caller['id'])[-1]['content'] == saved


def test_process_dies_without_emitting_final_report(env):
    register(env, kind='cap', seconds=10800)
    pulse(env, 10800)
    saved = env.store.finish_message(env.caller['id'], env.source['id'], 'Worker lost.', 'failed')
    assert 'Goal report' in saved and 'Completion has not been verified' in saved
    assert env.store.goal(env.caller['id'])['report']['response_to_id'] == env.source['id']


def test_resume_preserves_elapsed_budget_and_needs_new_user_authority(env):
    register(env, kind='cap', seconds=10)
    pulse(env, 10)
    env.store.finish_message(env.caller['id'], env.source['id'], 'Budget ended.')
    with pytest.raises(HTTPException):
        env.service.exchange(env.caller, {'action': 'resume', 'source_input_id': env.source['id']})
    source = new_input(env, 'Resume without extending the budget')
    env.store.update_run(env.caller['id'], status='running')
    state = env.service.exchange(env.caller, {'action': 'resume', 'source_input_id': source})['goal']
    assert state['status'] == 'wrapping' and state['elapsed_seconds'] == 10


def test_clock_and_creation_are_retry_safe_and_old_owner_is_fenced(env):
    first = register(env, kind='floor', seconds=10800)
    assert register(env, kind='floor', seconds=10800)['id'] == first['id']
    assert pulse(env, 50)['elapsed_seconds'] == pulse(env, 50)['elapsed_seconds'] == 50
    assert pulse(env, 40)['elapsed_seconds'] == 50
    env.store.update_run(env.caller['id'], token_hash='new-owner')
    with pytest.raises(HTTPException, match='ownership'):
        pulse(env, 60)


def test_floor_and_timebox_do_not_complete_early():
    from agent.goal_state import verdict
    for kind in ('floor', 'timebox'):
        state = create('Improve it', 1, now=0, budget={'kind': kind, 'seconds': 10800})
        assert floor_unmet(state)
        assert verdict(state, {'verdict': 'complete', 'reasons': 'Verified'})['status'] == 'active'
        state = advance(state, now=10800, elapsed=10800)
        assert state['status'] == ('active' if kind == 'floor' else 'wrapping')
    state = create('Until done', 1, now=0)
    assert verdict(state, {'verdict': 'complete', 'reasons': 'Verified'})['status'] == 'wrapping'


async def test_independent_reviewer_can_read_contrary_original_evidence(env):
    register(env)
    seq = evidence(env)
    calls = []
    async def assess(system, data):
        calls.append(data)
        if not data['records']:
            return json.dumps({'verdict': 'read', 'reasons': 'Inspect claimed test', 'read': [{'seq': seq}]})
        assert 'deployment was not verified' in data['records'][0]['text']
        return json.dumps({'verdict': 'continue', 'reasons': 'Deployment remains unverified.'})
    result = await env.service.review(env.caller, GoalArgs(action='complete', note='Everything done'), assess)
    assert len(calls) == 2 and result['goal']['status'] == 'active'
    assert 'unverified' in result['goal']['review']['reasons']


async def test_new_input_invalidates_inflight_completion_review(env):
    register(env)
    seq = evidence(env, 'All original checks passed.')
    async def assess(*args):
        new_input(env, 'Also verify the mobile UI.')
        return json.dumps({'verdict': 'complete', 'reasons': 'Original checks passed.'})
    result = await env.service.review(env.caller, GoalArgs(action='complete', evidence=[seq]), assess)
    assert result['goal']['status'] == 'active' and result['review']['verdict'] == 'continue'


async def test_unavailable_verifier_does_not_complete_or_pause(env):
    register(env)
    async def assess(*args):
        raise ConnectionError('Unavailable')
    result = await env.service.review(env.caller, GoalArgs(action='complete', note='done'), assess)
    assert result['goal']['status'] == 'active'


async def test_completion_and_blocker_are_independent_decisions(env):
    register(env)
    async def assess(*args):
        return json.dumps({'verdict': 'needs_input', 'reasons': 'Which environment is authorized?'})
    args = GoalArgs(action='block')
    first = await env.service.review(env.caller, args, assess)
    assert first['goal']['status'] == 'active' and first['goal']['review']['verdict'] == 'step_back'
    second = await env.service.review(env.caller, args, assess)
    assert second['goal']['status'] == 'wrapping' and second['goal']['terminal_reason'] == 'needs_input'


def test_real_loop_continues_past_twenty_five_rounds_without_tools(env):
    register(env)
    goal = GoalLoop(env.path, env.caller['id'], relay=env.relay)
    calls = []
    def run(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 30:
            # Runtime failure saves a report through normal settlement; it does
            # not fabricate a user pause or silently reset the budget.
            return response(failed=True)
        return response('Considering the next part.')
    result = run_goal_conversation(SimpleNamespace(run_conversation=run), 'work', [], 'rules', goal)
    goal.save()
    assert len(calls) == 30 and goal.active and result['failed']


def test_goal_command_text_is_not_auto_parsed_as_duration(env):
    goal = GoalLoop(env.path, env.caller['id'], relay=env.relay)
    assert goal.accept('/goal work for three hours') is None
    assert goal.state is None
    assert 'timebox' in goal.instructions()


def test_legacy_import_is_once_and_requires_budget_confirmation(env):
    legacy = {'objective': 'Original goal', 'status': 'active', 'elapsed_seconds': 99}
    one = env.service.exchange(env.caller, {'action': 'import', 'legacy': legacy}, internal=True)['goal']
    two = env.service.exchange(env.caller, {'action': 'import', 'legacy': {**legacy, 'elapsed_seconds': 0}}, internal=True)['goal']
    assert one == two and one['elapsed_seconds'] == 99 and one['status'] == 'paused'


def test_no_goal_leaves_normal_conversation_unchanged(env):
    goal = GoalLoop(env.path, env.caller['id'])
    result = response('Hello')
    assert run_goal_conversation(SimpleNamespace(run_conversation=lambda *a, **k: result), 'hi', [], '', goal) is result


def test_nonchat_initial_source_and_failure_report(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Work until every source is checked', '', 'demo', [], user_id='owner')
    store.update_run(run['id'], status='running', token_hash='token')
    service = store.goals = Goals(store)
    caller = store.run(run['id'])
    source = service.exchange(caller, {'action': 'get'})['inputs'][0]
    assert source['id'] == 'run:' + run['id']
    service.exchange(caller, {'action': 'create', 'source_input_id': source['id'], 'objective': 'Check every source'})
    store.update_run(run['id'], status='failed', error='Computer lost.')
    assert 'Goal report' in store.run(run['id'])['summary']
    assert store.goal(run['id'])['status'] == 'interrupted'
    store.close()


def test_wrapped_slack_control_uses_receipt_bound_source(env):
    register(env)
    source = new_input(env, 'Slack reply from U12345678:\n/goal pause')
    env.store.execute('''INSERT INTO slack_receipts(event_id,team_id,channel,message_ts,user_id,run_id,message_id,command,handled)
        VALUES(?,?,?,?,?,?,?,?,?)''', ('event-control', 'T12345678', 'C12345678', '1000000000.000001', 'U12345678', env.caller['id'], source, '', 1))
    goal = GoalLoop(env.path, env.caller['id'], relay=env.relay)
    assert 'paused' in goal.accept('Slack reply from U12345678:\n/goal pause', source)
    assert goal.state['status'] == 'paused'
    with pytest.raises(HTTPException):
        env.service.exchange(env.caller, {'action': 'resume', 'source_input_id': env.source['id']})


async def test_new_input_after_review_cannot_be_settled_as_completed(env):
    register(env)
    async def assess(*args):
        return json.dumps({'verdict': 'complete', 'reasons': 'Original objective verified.'})
    await env.service.review(env.caller, GoalArgs(action='complete'), assess)
    new_input(env, 'Also check the final artifact.')
    saved = env.store.finish_message(env.caller['id'], env.source['id'], 'Original work verified.')
    assert 'newer user message' in saved
    state = env.store.goal(env.caller['id'])
    assert state['status'] == 'active' and 'terminal_reason' not in state


def test_recovery_of_already_interrupted_input_saves_missing_goal_report(env):
    register(env)
    pulse(env, 300)
    env.store.interrupt_messages(env.caller['id'])
    answers = [m for m in env.store.messages(env.caller['id']) if m['role'] == 'assistant']
    assert len(answers) == 1 and 'Goal report' in answers[0]['content']
    env.store.interrupt_messages(env.caller['id'])
    assert len([m for m in env.store.messages(env.caller['id']) if m['role'] == 'assistant']) == 1
