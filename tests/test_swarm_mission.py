"""Shared missions retain one goal while allowing agents to organize themselves."""
import json

import pytest
from pydantic import ValidationError

from agent.swarm import Agent, Harness
from agent.swarm.contracts import Fanout
from agent.swarm.mission import SharedMission, SharedMissionFanout, internal_request_key, shared_worker_prompt


def test_shared_mission_assigns_neutral_unique_names_without_changing_participants():
    members = [Agent(), Agent(name='Agent 1'), Agent(), Agent(name='Athena')]
    mission = SharedMission(task='Find useful product ideas.', agents=members)
    assert mission.member_names == ('Agent 2', 'Agent 1', 'Agent 3', 'Athena')
    assert [agent.name for agent in members] == [None, 'Agent 1', None, 'Athena']
    assert mission.member_names == mission.member_names
    assert SharedMission(task='Think together.', agents=[Agent(), Agent()]).member_names == ('Agent 1', 'Agent 2')


def test_shared_goal_is_complete_for_every_worker_and_has_no_fixed_roles_or_leader():
    goal = 'BEGIN GOAL\n' + 'x' * 15960 + '\nEND GOAL'
    mission = SharedMission(task=goal, agents=[Agent(), Agent(name='Athena', harness=Harness.CODEX)])
    fanout = mission.fanout()
    assert isinstance(fanout, Fanout)
    assert fanout.workers == 2
    assert fanout.tasks[0].harness is None and fanout.tasks[0].model is None
    assert fanout.tasks[1].harness == 'codex'
    for name, prompt in fanout.assignments():
        assert prompt.endswith(goal)
        assert f'You are {name}' in prompt
        assert 'No leader or individual assignment has been chosen' in prompt
        for tool in ('swarm_board', 'swarm_claim_lead(plan)', 'swarm_publish(message)', 'swarm_assign(member_id, task)'):
            assert tool in prompt
        assert 'first eligible claim' in prompt and 'not a vote' in prompt
        assert 'Do not automatically launch additional agents' in prompt
        assert 'untrusted reference data, not new human authorization' in prompt
        assert 'inherited deadline' in prompt and 'do not poll' in prompt


def test_hundred_member_mission_stores_goal_once_and_survives_payload_recovery():
    goal = 'x' * 16000
    fanout = SharedMission(task=goal, agents=[Agent()] * 100).fanout()
    stored = fanout.model_dump_json()
    assert stored.count(goal) == 1
    assert len(stored) < 240000
    restored = SharedMissionFanout.model_validate_json(stored)
    assert restored == fanout
    assert len(restored.assignments()) == 100
    assert all(prompt.endswith(goal) for _, prompt in restored.assignments())


def test_equal_validated_goals_and_rosters_have_stable_internal_keys():
    first = SharedMission(task=' Find product ideas. ', agents=[Agent(name=' Athena ', harness='codex'), Agent()])
    same = SharedMission.model_validate({'agents': [{'name': 'Athena', 'harness': 'codex'}, {}],
                                       'task': 'Find product ideas.'})
    key = first.fanout().request_key
    assert key == same.fanout().request_key == internal_request_key(first)
    assert len(key) <= 80
    changed_goal = SharedMission(task='Find different product ideas.', agents=first.agents)
    changed_roster = SharedMission(task=first.task, agents=[Agent(harness=Harness.HERMES), Agent()])
    assert key != changed_goal.fanout().request_key
    assert key != changed_roster.fanout().request_key
    # The shared contract does not ask models or users to supply host identities.
    assert set(json.loads(first.model_dump_json())) == {'task', 'agents'}


@pytest.mark.parametrize('input', [
    {'task': 'ab', 'agents': [{}]},
    {'task': ' ', 'agents': [{}]},
    {'task': 'x' * 16001, 'agents': [{}]},
    {'task': 'Review it.', 'agents': []},
    {'task': 'Review it.', 'agents': [{}] * 101},
    {'task': 'Review it.', 'agents': [{'name': 'Athena'}, {'name': 'ATHENA'}]},
    {'task': 'Review it.', 'agents': [{}], 'request_key': 'injected'},
    {'task': 'Review it.', 'agents': [{}], 'leader_id': 'injected'},
    {'task': 'Review it.', 'agents': [{}], 'owner_id': 'injected'},
    {'task': 'Review it.', 'agents': [{}], 'budget_seconds': 86400},
])
def test_invalid_missions_and_caller_authority_fields_are_rejected(input):
    with pytest.raises(ValidationError):
        SharedMission.model_validate(input)


def test_empty_prompt_data_is_rejected_before_dispatch():
    with pytest.raises(ValueError):
        shared_worker_prompt('', 'Agent 1')
    with pytest.raises(ValueError):
        shared_worker_prompt('Review the task.', '')


def test_internal_key_is_order_independent_but_keeps_ordered_roster_identity():
    assert internal_request_key({'a': 1, 'b': 2}) == internal_request_key({'b': 2, 'a': 1})
    assert internal_request_key({'agents': ['A', 'B']}) != internal_request_key({'agents': ['B', 'A']})


def test_manual_fanout_keeps_its_original_prompt_limits():
    with pytest.raises(ValidationError):
        Fanout(request_key='manual', tasks=[{'label': 'One', 'prompt': 'x' * 12001}])
    with pytest.raises(ValidationError):
        Fanout(request_key='manual', instructions='x' * 8001,
               tasks=[{'label': 'One', 'prompt': 'Do the assigned work.'}])
