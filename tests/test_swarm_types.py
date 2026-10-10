"""Public swarm definitions validate caller input before durable submission."""
import json

import pytest
from pydantic import ValidationError

from agent.swarm.contracts import Assignment
from agent.swarm.types import Agent, Harness


def test_harness_enum_matches_registered_built_in_runtimes():
    from agent.harnesses.harness_registry import HARNESSES

    assert {harness.value for harness in Harness} == set(HARNESSES)
    assert Harness.CODEX == 'codex'
    assert str(Harness.CLAUDE_AGENT_SDK) == 'claude-agent-sdk'
    assert set(json.loads(json.dumps(list(Harness)))) == set(HARNESSES)


def test_agent_schema_exposes_required_typed_harness_and_public_field_names():
    schema = Agent.model_json_schema()
    assert set(schema['properties']) == {'name', 'task', 'harness', 'model'}
    assert set(schema['required']) == {'name', 'task', 'harness'}
    assert schema['additionalProperties'] is False
    assert schema['properties']['harness'] == {'$ref': '#/$defs/Harness'}
    assert set(schema['$defs']['Harness']['enum']) == {harness.value for harness in Harness}


@pytest.mark.parametrize('harness', list(Harness))
def test_agents_round_trip_json_and_map_to_the_unchanged_wire_contract(harness):
    agent = Agent(name='  Researcher  ', task='  Find supporting evidence.  ',
                  harness=harness, model='  openai/example  ')
    serialized = agent.model_dump_json()
    assert json.loads(serialized) == {
        'name': 'Researcher', 'task': 'Find supporting evidence.',
        'harness': harness.value, 'model': 'openai/example',
    }
    restored = Agent.model_validate_json(serialized)
    assert restored == agent and restored.harness is harness
    assignment = agent.to_assignment()
    assert isinstance(assignment, Assignment)
    assert assignment.model_dump() == {
        'label': 'Researcher', 'prompt': 'Find supporting evidence.',
        'harness': harness.value, 'model': 'openai/example',
    }
    assert type(assignment.harness) is str


def test_omitted_model_preserves_host_inheritance_without_mutating_the_agent():
    agent = Agent(name='Critic', task='Challenge the assumptions.', harness=Harness.HERMES)
    assert agent.model is None and agent.to_assignment().model is None
    assignment = agent.to_assignment()
    assignment.label = 'Changed by the host'
    assert agent.name == 'Critic'
    with pytest.raises(ValidationError, match='frozen'):
        agent.harness = Harness.CODEX


@pytest.mark.parametrize('updates', [
    {'name': ''}, {'name': ' \n\t'}, {'name': 'a' * 101}, {'name': 42},
    {'task': ''}, {'task': ' \n\t'}, {'task': 'ab'}, {'task': 'a' * 12001}, {'task': None},
    {'harness': 'future-harness'}, {'harness': ''}, {'harness': None}, {'harness': 42},
    {'model': ''}, {'model': ' \n\t'}, {'model': 'a' * 121},
    {'owner_id': 'another-agent'}, {'budget_seconds': 86400}, {'label': 'Legacy name'},
])
def test_invalid_or_unknown_fields_are_rejected(updates):
    values = {'name': 'Reviewer', 'task': 'Review the proposal.', 'harness': Harness.CODEX}
    with pytest.raises(ValidationError):
        Agent(**(values | updates))


def test_harness_is_required_and_valid_string_ids_become_enum_members():
    with pytest.raises(ValidationError, match='harness'):
        Agent(name='Reviewer', task='Review the proposal.')
    agent = Agent(name='Reviewer', task='Review the proposal.', harness='codex')
    assert agent.harness is Harness.CODEX


def test_documented_text_limits_accept_their_boundaries():
    shortest = Agent(name='A', task='Run', harness=Harness.PI, model='M')
    longest = Agent(name='a' * 100, task='a' * 12000, harness=Harness.DEEPAGENTS, model='m' * 120)
    assert shortest.to_assignment().prompt == 'Run'
    assert len(longest.to_assignment().prompt) == 12000
