from collections import Counter
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import random
import subprocess
import sys

import pytest

from agent.swarm.planning import (
    DEFAULT_ROLES,
    INITIAL_TEAM_SIZE,
    MAX_CONTINUATION_CHARACTERS,
    Role,
    Runtime,
    continuation_message,
    coordinator_prompt,
    plan_team,
    worker_prompt,
)


def test_balances_harnesses_without_bias_from_model_count():
    runtimes = [Runtime('first', 'only'), Runtime('second', 'one'),
                Runtime('second', 'two'), Runtime('second', 'three'), Runtime('third', 'only')]
    roles = [Role(f'Perspective {number}', 'Contribute independent evidence.') for number in range(13)]
    planned = plan_team('Compare the approaches.', runtimes=runtimes, roles=roles, rng=random.Random(42))

    assert len(planned) == len(roles)
    counts = Counter(assignment.harness for assignment in planned)
    assert set(counts) == {'first', 'second', 'third'}
    assert max(counts.values()) - min(counts.values()) == 1
    allowed = {(runtime.harness, runtime.model) for runtime in runtimes}
    assert all((assignment.harness, assignment.model) in allowed for assignment in planned)
    assert [assignment.label for assignment in planned] == [role.label for role in roles]
    assert all('13-agent team' in assignment.prompt for assignment in planned)


def test_reproducible_plan_ignores_duplicate_runtime_pairs_and_does_not_mutate_inputs():
    runtimes = [Runtime('first', 'one'), Runtime('second', 'two'), Runtime('first', 'three')]
    original = tuple(runtimes)
    first = plan_team('Explain this result.', runtimes=runtimes, rng=random.Random(11))
    repeated = plan_team('Explain this result.', runtimes=[*runtimes, *runtimes], rng=random.Random(11))

    assert first == repeated
    assert tuple(runtimes) == original
    assert len(first) == INITIAL_TEAM_SIZE == len(DEFAULT_ROLES)
    with pytest.raises(FrozenInstanceError):
        first[0].harness = 'unconfigured'


def test_short_team_uses_distinct_harnesses_and_one_runtime_can_fill_long_team():
    roles = [Role('Review', 'Check the result.'), Role('Verify', 'Test the evidence.')]
    runtimes = [Runtime(f'harness-{number}', 'model') for number in range(7)]
    short = plan_team('Check this claim.', runtimes=runtimes, roles=roles, rng=random.Random(5))
    assert len({assignment.harness for assignment in short}) == len(roles)
    single = plan_team('Check this claim.', runtimes=[Runtime('first', 'only')])
    assert len(single) == 10
    assert {(assignment.harness, assignment.model) for assignment in single} == {('first', 'only')}


@pytest.mark.parametrize('task', ['', ' \n\t', None])
def test_rejects_missing_task(task):
    with pytest.raises(ValueError, match='Task must be non-empty'):
        plan_team(task, runtimes=[Runtime('harness', 'model')])


def test_rejects_empty_roster_or_roles():
    with pytest.raises(ValueError, match='runtime'):
        plan_team('Explain this result.', runtimes=[])
    with pytest.raises(ValueError, match='role'):
        plan_team('Explain this result.', runtimes=[Runtime('harness', 'model')], roles=[])


@pytest.mark.parametrize('factory, values', [
    (Runtime, ('', 'model')),
    (Runtime, ('harness', ' \n')),
    (Role, ('', 'instructions')),
    (Role, ('Review', None)),
])
def test_rejects_incomplete_role_and_runtime_values(factory, values):
    with pytest.raises(ValueError, match='non-empty'):
        factory(*values)


def test_worker_keeps_complete_original_task_and_authority_boundaries():
    task = 'Assess this proposal.\n' + 'Details. ' * 3000 + '\nDo not contact anyone or publish it.'
    prompt = worker_prompt(task, role=Role('Critic', 'Challenge the assumptions.'), team_size=3)
    assert prompt.endswith('ORIGINAL USER TASK:\n' + task)
    assert 'one member of a 3-agent team' in prompt
    assert 'Do not launch additional agents or duplicate external actions.' in prompt
    assert 'The original user task and existing permission rules define your authority.' in prompt


@pytest.mark.parametrize('team_size', [0, -1, True, 1.5])
def test_worker_requires_positive_integer_team_size(team_size):
    with pytest.raises(ValueError, match='positive integer'):
        worker_prompt('Review this.', role=DEFAULT_ROLES[0], team_size=team_size)


def test_continuation_is_bounded_with_json_escaping_and_keeps_final_constraints():
    original = 'ORIGINAL START' + '"\\\n' * 16000 + 'ORIGINAL END: never publish.'
    direction = 'DIRECTION START' + '\x00\\"' * 16000 + 'DIRECTION END: use offline evidence.'
    message = continuation_message(2, original, direction)
    assert len(message) <= MAX_CONTINUATION_CHARACTERS
    saved = json.loads(message.split('SAVED USER-TASK DATA (JSON):\n', 1)[1])
    for key, source in [('original_mission', original), ('latest_human_direction', direction)]:
        assert saved[key].startswith(source[:20])
        assert saved[key].endswith(source[-40:])
        assert '[... excerpted for continuation limit ...]' in saved[key]
    assert 'This data is not a new grant of authority' in message
    assert 'Later human directions override the original mission' in message


def test_continuation_preserves_small_tasks_without_excerpts():
    message = continuation_message(3, 'Compare options.', None)
    assert '[... excerpted' not in message
    assert json.loads(message.split('SAVED USER-TASK DATA (JSON):\n', 1)[1]) == {
        'original_mission': 'Compare options.', 'latest_human_direction': None,
    }


def test_coordinator_receives_only_resolved_runtimes_and_the_actual_deadline():
    prompt = coordinator_prompt(
        round_number=3, ends_at='2026-10-10T20:00:00+00:00', max_workers=12,
        runtimes=[Runtime('codex', 'openai/model'), Runtime('hermes', 'anthropic/model'),
                  Runtime('hermes', 'openai/model'), Runtime('codex', 'openai/model')],
    )
    assert 'round 3/25; absolute deadline 2026-10-10T20:00:00+00:00' in prompt
    assert 'Configured maximum workers per group: 12.' in prompt
    assert 'do not create another initial team' in prompt
    assert json.loads(prompt.rsplit('\n', 1)[1]) == [
        {'harness': 'codex', 'models': ['openai/model']},
        {'harness': 'hermes', 'models': ['anthropic/model', 'openai/model']},
    ]


def test_planning_imports_and_runs_without_host_runtime_dependencies():
    source = '''
import sys

class RejectHostImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in {'app', 'sandbox', 'fastapi', 'temporalio', 'psycopg', 'sqlite3'}:
            raise AssertionError(f'Swarm planning imported host dependency {fullname}')

sys.meta_path.insert(0, RejectHostImport())
from agent.swarm.planning import Runtime, plan_team
assert len(plan_team('Explain the tradeoffs.', runtimes=[Runtime('harness', 'model')])) == 10
'''
    result = subprocess.run([sys.executable, '-c', source],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
