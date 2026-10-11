"""Requested reporting has durable receipts and never grants a stop or completion."""
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.goals import GoalArgs, Goals
from test_goals import env, evidence, pulse, register
from test_slack import slack_app
from test_slack_web_mirror import mirror, drain
from test_slack_chat import start


def checkpoints(store, run_id):
    return [e for e in store.events(run_id) if e['data'].get('phase') == 'goal_checkpoint']


def new_input(env, text):
    message, _ = env.store.enqueue_message(env.caller['id'], text, uuid4().hex, user_id='owner')
    env.store.execute("UPDATE messages SET status='injected',steering_parent_id=? WHERE id=?", (env.source['id'], message['id']))
    return message['id']


def create(env, reporting, budget=None):
    env.store.execute('UPDATE messages SET content=? WHERE id=?',
        ('Keep working until verified. Report on my requested schedule and continue.', env.source['id']))
    return env.service.exchange(env.caller, {'action':'create', 'source_input_id':env.source['id'],
        'objective':'Finish every requirement', 'reporting':reporting, 'budget':budget})['goal']


def test_three_hour_checkpoint_reports_saved_evidence_and_keeps_working_after_recovery(env):
    create(env, {'at_seconds':[10800]})
    pulse(env, 100)
    seq = evidence(env)
    env.service.exchange(env.caller, {'action':'progress', 'note':'Verified the parser fix.',
        'remaining':'Provider deployment still needs verification.', 'evidence':[seq]})
    assert not checkpoints(env.store, env.caller['id'])
    state = pulse(env, 10800)
    assert state['status'] == 'active' and state['budget'] == {} and not state.get('report')
    events = checkpoints(env.store, env.caller['id'])
    assert len(events) == 1
    assert 'Verified the parser fix.' in events[0]['message']
    assert '100 active-work seconds' in events[0]['message']
    assert 'Provider deployment still needs verification.' in events[0]['message']
    assert 'completion has not been verified' in events[0]['message']
    env.service = Goals(env.store, clock=lambda: env.now[0])
    assert pulse(env, 10800)['status'] == 'active'
    assert pulse(env, 15000)['elapsed_seconds'] == 15000
    assert len(checkpoints(env.store, env.caller['id'])) == 1


def test_periodic_reporting_coalesces_missed_intervals_and_preserves_pause_clock(env):
    create(env, {'interval_seconds':3600})
    assert pulse(env, 10900)['status'] == 'active'
    assert len(checkpoints(env.store, env.caller['id'])) == 1
    source = new_input(env, 'Pause the goal.')
    env.service.exchange(env.caller, {'action':'pause','source_input_id':source})
    assert pulse(env, 20000)['elapsed_seconds'] == 10900
    source = new_input(env, 'Resume the goal with the same reporting cadence.')
    env.service.exchange(env.caller, {'action':'resume','source_input_id':source})
    assert pulse(env, 23500)['elapsed_seconds'] == 14400
    assert len(checkpoints(env.store, env.caller['id'])) == 2
    source = new_input(env, 'Stop periodic reporting and keep working.')
    env.service.exchange(env.caller, {'action':'revise','source_input_id':source, 'reporting':{}})
    assert pulse(env, 40000)['status'] == 'active'
    assert len(checkpoints(env.store, env.caller['id'])) == 2


def test_plain_goal_has_no_invented_report_cadence_and_final_report_supersedes_checkpoint(env):
    register(env)
    pulse(env, 10800)
    assert not checkpoints(env.store, env.caller['id'])
    source = new_input(env, 'Stop at four hours and report hourly until then.')
    env.service.exchange(env.caller, {'action':'revise','source_input_id':source,
        'budget':{'kind':'timebox','seconds':14400},'reporting':{'interval_seconds':3600}})
    assert pulse(env, 14400)['status'] == 'wrapping'
    assert not checkpoints(env.store, env.caller['id'])


@pytest.mark.parametrize('value', [-1, 0, float('inf'), float('nan')])
def test_reporting_times_are_positive_finite(value):
    for schedule in ({'interval_seconds':value}, {'at_seconds':[value]}):
        with pytest.raises(ValidationError):
            GoalArgs(action='create', reporting=schedule)


def test_reporting_changes_need_human_source_and_future_checkpoint(env):
    create(env, {'interval_seconds':3600})
    pulse(env, 500)
    with pytest.raises(HTTPException, match='authenticated'):
        env.service.exchange(env.caller, {'action':'progress','reporting':{}})
    source = new_input(env, 'Report at a new checkpoint.')
    with pytest.raises(HTTPException, match='after'):
        env.service.exchange(env.caller, {'action':'revise','source_input_id':source,'reporting':{'at_seconds':[100]}})
    assert env.store.goal(env.caller['id'])['reporting'] == {'at_seconds':[], 'interval_seconds':3600}


def test_checkpoint_and_delivery_intent_roll_back_together_then_retry_once(env):
    create(env, {'at_seconds':[10800]})
    def unavailable(conn, run_id):
        raise RuntimeError('Commit interrupted')
    env.store.public_delivery = unavailable
    with pytest.raises(RuntimeError, match='interrupted'):
        pulse(env, 10800)
    assert not checkpoints(env.store, env.caller['id'])
    assert env.store.goal(env.caller['id'])['elapsed_seconds'] == 0
    env.store.public_delivery = None
    pulse(env, 10800)
    pulse(env, 10800)
    assert len(checkpoints(env.store, env.caller['id'])) == 1


def test_checkpoint_queues_slack_with_same_saved_event_and_keeps_turn_active(mirror):
    app, client, run_id = start(mirror)
    store = app.state.store
    message = store.claim_message(run_id)
    store.update_run(run_id, status='running', token_hash='owner')
    caller = store.run(run_id)
    service = store.goals
    service.exchange(caller, {'action':'create','source_input_id':message['id'],
        'objective':'Work until verified; report at three hours and continue', 'reporting':{'at_seconds':[10800]}})
    service.exchange(caller, {'action':'progress','note':'Local checks passed.', 'remaining':'Production remains unverified.'})
    tick = {'action':'tick','segment':'worker','elapsed':10800}
    service.exchange(caller, tick, internal=True)
    outbox = store.rows("SELECT * FROM slack_outbox WHERE run_id=? AND kind='progress'", (run_id,))
    assert len(outbox) == 1  # No collector invocation is needed to save send intent.
    assert 'Local checks passed.' in outbox[0]['text']
    assert json.loads(outbox[0]['metadata'])['event_id'] == checkpoints(store, run_id)[0]['id']
    service.exchange(caller, tick, internal=True)
    assert len(store.rows("SELECT * FROM slack_outbox WHERE kind='progress'")) == 1
    drain(app, client)
    assert any('Goal progress checkpoint' in p.get('text','') for p in mirror[3])
    assert store.goal(run_id)['status'] == 'active'
    assert store.run(run_id)['status'] == 'running'
