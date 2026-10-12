"""Only explicit owner timing commands may change swarm continuation policy."""
from dataclasses import FrozenInstanceError

import pytest

from agent.swarm.timing import (
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    SwarmTimeIntent,
    SwarmTimeIntentKind,
    parse_swarm_time_intent,
    validate_swarm_budget,
)


@pytest.mark.parametrize(('message', 'kind', 'seconds'), [
    ('run for 4 hours', 'set', 14400),
    ('work for two days', 'set', 172800),
    ('keep going for 3 more hours', 'extend', 10800),
    ('add 2 days', 'extend', 172800),
    ('extend by an hour', 'extend', 3600),
    ('continue for another two and a half hours', 'extend', 9000),
    ('go another half an hour', 'extend', 1800),
    ('run for 1.5 days', 'set', 129600),
    ('run for .5 hours', 'set', 1800),
    ('run for half an hour', 'set', 1800),
    ('keep working for twenty-four hours', 'set', 86400),
    ('run for ninety nine minutes', 'set', 5940),
    ('run for one hundred and twenty minutes', 'set', 7200),
    ('run for seven hundred twenty hours', 'set', MAX_DURATION_SECONDS),
    ('run for 43200 minutes', 'set', MAX_DURATION_SECONDS),
    ('add thirty days', 'extend', MAX_DURATION_SECONDS),
    ('run for one minute', 'set', MIN_DURATION_SECONDS),
    ('run for 1.25 minutes', 'set', 75),
    ('  Please   keep going for TWO more hours!  ', 'extend', 7200),
    ('Could you please add 2 hrs, please?', 'extend', 7200),
    ('run for 2 mins', 'set', 120),
    ('continue', 'continue', None),
    ('keep going', 'continue', None),
    ('resume please', 'continue', None),
    ('Can you keep working?', 'continue', None),
    ('run until I stop you', 'unlimited', None),
    ('no time limit', 'unlimited', None),
    ('remove the time limit', 'unlimited', None),
    ('keep going indefinitely', 'unlimited', None),
    ('continue forever', 'unlimited', None),
    ('Please run without a time limit.', 'unlimited', None),
])
def test_explicit_commands_have_typed_validated_intents(message, kind, seconds):
    intent = parse_swarm_time_intent(message)
    assert intent == SwarmTimeIntent(SwarmTimeIntentKind(kind), seconds)
    assert isinstance(intent.kind, SwarmTimeIntentKind)


@pytest.mark.parametrize('message', [
    '', ' ', 'go more hours', 'run for some hours', 'add a few days',
    'run for 0 hours', 'run for -2 hours', 'run for +2 hours',
    'run for .5 minutes', 'run for 31 days', 'add 721 hours',
    'run for 30.00001 days', 'run for 1.001 minutes',
    'run for 30.00000000000000000000000000000000000001 days',
    'run for inf days', 'run for nan hours', 'run for 1e3 hours',
    'run for one two hours', 'run for twenty zero hours',
    'run for zero hundred hours', 'run for one hundred and hours',
    'run for 2 seconds', 'run for 2 weeks',
    'do not continue', "don't continue", 'never keep going',
    'pause', 'stop', 'stop after 2 hours', 'pause for 2 hours',
    'run for 2 hours and then pause', 'continue and delete everything',
    'continue unless I say otherwise',
    'write a story about an agent that runs for 2 hours',
    'explain how to run for 2 hours', 'what does no time limit mean?',
    'the agent said: continue', 'the user said: add 2 days',
    'Can it run for 2 hours?', 'Should you continue?',
    '"continue"', "'add 2 days'", '`run for 2 hours`',
    '> keep going', '```\ncontinue\n```',
    'notes:\nrun for 2 hours', 'run\nfor 2 hours',
    'continue\rstop', 'continue\x00', 'run\u2028for 2 hours',
    'run for 99999999999999999999999999999999999999 days',
    'continue' + ' ' * 513,
    None, 3600,
])
def test_ambiguous_task_text_negations_and_unsafe_durations_are_not_commands(message):
    assert parse_swarm_time_intent(message) is None


@pytest.mark.parametrize(('kind', 'seconds'), [
    (SwarmTimeIntentKind.SET, None),
    (SwarmTimeIntentKind.EXTEND, None),
    (SwarmTimeIntentKind.SET, 59),
    (SwarmTimeIntentKind.EXTEND, MAX_DURATION_SECONDS + 1),
    (SwarmTimeIntentKind.SET, 60.0),
    (SwarmTimeIntentKind.SET, True),
    (SwarmTimeIntentKind.SET, '3600'),
    (SwarmTimeIntentKind.CONTINUE, 3600),
    (SwarmTimeIntentKind.UNLIMITED, 3600),
    ('set', 3600),
])
def test_intent_type_cannot_represent_invalid_duration_policy(kind, seconds):
    with pytest.raises(ValueError):
        SwarmTimeIntent(kind, seconds)


def test_intents_are_immutable():
    intent = SwarmTimeIntent(SwarmTimeIntentKind.EXTEND, 3600)
    with pytest.raises(FrozenInstanceError):
        intent.seconds = 7200


@pytest.mark.parametrize('seconds', [0, MIN_DURATION_SECONDS, 7200, MAX_DURATION_SECONDS])
def test_budget_validation_accepts_unlimited_and_finite_bounds(seconds):
    assert validate_swarm_budget(seconds) == seconds


@pytest.mark.parametrize('seconds', [
    -1, 1, MIN_DURATION_SECONDS - 1, MAX_DURATION_SECONDS + 1,
    None, True, False, 0.0, 60.0, '3600', float('inf'), float('nan'),
])
def test_budget_validation_rejects_invalid_values_without_coercion(seconds):
    with pytest.raises(ValueError):
        validate_swarm_budget(seconds)


@pytest.mark.parametrize('message', [
    'pause', 'stop', 'stop now', 'pause the swarm', 'stop working',
    'stay paused', 'remain stopped', 'keep it paused', 'keep the swarm stopped',
    'do not continue', "don't continue", 'don’t resume', 'dont restart',
    'do not resume the swarm', 'do not keep working', 'do not run yet',
    'no need to continue', 'Please stay paused.', 'Could you please stop?',
])
def test_explicit_hold_commands_do_not_authorize_automatic_resume(message):
    from agent.swarm.timing import is_swarm_hold_command
    assert is_swarm_hold_command(message)
    assert parse_swarm_time_intent(message) is None


@pytest.mark.parametrize('message', [
    'continue', 'no time limit', 'run for two hours', 'do not stop',
    'write a story about a robot that says stop', 'what does pause mean?',
    '"stop"', '`pause`', '> stay paused', 'instructions:\nstop',
    '', None, 'stop' + ' ' * 513,
])
def test_hold_detection_is_anchored_and_does_not_invert_other_commands(message):
    from agent.swarm.timing import is_swarm_hold_command
    assert not is_swarm_hold_command(message)
