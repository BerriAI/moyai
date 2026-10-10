"""Public swarm definitions validate participants without assigning them jobs."""
import json

import pytest
from pydantic import ValidationError

from agent.swarm.types import Agent, Harness


def test_harness_enum_matches_registered_built_in_runtimes():
    from agent.harnesses.harness_registry import HARNESSES

    assert {harness.value for harness in Harness} == set(HARNESSES)
    assert Harness.CODEX == 'codex'
    assert str(Harness.CLAUDE_AGENT_SDK) == 'claude-agent-sdk'
    assert set(json.loads(json.dumps(list(Harness)))) == set(HARNESSES)


def test_agent_schema_has_optional_preferences_without_per_member_tasks():
    schema = Agent.model_json_schema()
    assert set(schema['properties']) == {'name', 'harness', 'model'}
    assert not schema.get('required')
    assert schema['additionalProperties'] is False
    assert {'$ref': '#/$defs/Harness'} in schema['properties']['harness']['anyOf']
    assert set(schema['$defs']['Harness']['enum']) == {harness.value for harness in Harness}
    assert Agent().model_dump() == {'name': None, 'harness': None, 'model': None}


@pytest.mark.parametrize('harness', list(Harness))
def test_agents_round_trip_json_with_typed_harness_preferences(harness):
    agent = Agent(name='  Athena  ', harness=harness, model='  openai/example  ')
    serialized = agent.model_dump_json()
    assert json.loads(serialized) == {
        'name': 'Athena', 'harness': harness.value, 'model': 'openai/example',
    }
    restored = Agent.model_validate_json(serialized)
    assert restored == agent and restored.harness is harness


def test_omitted_preferences_are_left_to_the_host_and_agent_is_immutable():
    agent = Agent(name='Athena')
    assert agent.model is None and agent.harness is None
    with pytest.raises(ValidationError, match='frozen'):
        agent.harness = Harness.CODEX


@pytest.mark.parametrize('updates', [
    {'name': ''}, {'name': ' \n\t'}, {'name': 'a' * 101}, {'name': 42},
    {'task': 'Find supporting evidence.'},
    {'harness': 'future-harness'}, {'harness': ''}, {'harness': 42},
    {'model': ''}, {'model': ' \n\t'}, {'model': 'a' * 121},
    {'owner_id': 'another-agent'}, {'budget_seconds': 86400}, {'label': 'Legacy name'},
])
def test_invalid_preferences_and_authority_fields_are_rejected(updates):
    with pytest.raises(ValidationError):
        Agent(**updates)


def test_harness_is_optional_and_valid_string_ids_become_enum_members():
    assert Agent(name='Athena').harness is None
    assert Agent(harness='codex').harness is Harness.CODEX


def test_documented_text_limits_accept_their_boundaries():
    assert Agent(name='A', model='M').name == 'A'
    longest = Agent(name='a' * 100, harness=Harness.DEEPAGENTS, model='m' * 120)
    assert len(longest.name) == 100 and len(longest.model) == 120
