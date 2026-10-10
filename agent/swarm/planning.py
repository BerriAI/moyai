"""Agent-owned swarm composition and prompts, independent of a host runtime.

The host resolves its allowed harness/model pairs before planning, then persists
the resulting assignments and assigns execution IDs. Planning does not launch
workers, grant permissions, or make a quality-based model-routing claim.
"""
from collections.abc import Sequence
from dataclasses import dataclass
import json
import random


MAX_ROUNDS = 25
ROUND_DELAY_SECONDS = 30
MAX_CONTINUATION_CHARACTERS = 16000
INITIAL_TEAM_SIZE = 10
INITIAL_ROLES = (
    ('Mission analyst', 'Clarify the desired outcome, constraints, and acceptance criteria.'),
    ('Explorer', 'Develop a promising approach and explain why it fits the task.'),
    ('Alternative thinker', 'Develop a meaningfully different approach and compare its tradeoffs.'),
    ('Researcher', 'Identify evidence available through the enabled tools; separate findings from assumptions.'),
    ('Systems thinker', 'Examine how the proposed parts work together and identify dependencies.'),
    ('Practical planner', 'Produce concrete next steps and the smallest useful deliverable.'),
    ('Critic', 'Challenge assumptions and identify the strongest reasons the approach may fail.'),
    ('Verifier', 'Define checks that would distinguish a correct answer from a plausible one.'),
    ('Risk reviewer', 'Identify meaningful risks, missing context, and decisions needing human input.'),
    ('Editor', 'Offer a concise, user-facing answer and identify what deserves emphasis.'),
)


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be non-empty text.')


@dataclass(frozen=True, slots=True)
class Runtime:
    """An exact harness/model pair already permitted by the host."""

    harness: str
    model: str

    def __post_init__(self) -> None:
        _require_text(self.harness, 'Harness')
        _require_text(self.model, 'Model')


@dataclass(frozen=True, slots=True)
class Role:
    """A complementary perspective on a shared task."""

    label: str
    instructions: str

    def __post_init__(self) -> None:
        _require_text(self.label, 'Role label')
        _require_text(self.instructions, 'Role instructions')


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """A worker assignment ready for durable submission by the host."""

    label: str
    prompt: str
    harness: str
    model: str


DEFAULT_ROLES = tuple(Role(label, instructions) for label, instructions in INITIAL_ROLES)


def worker_prompt(task: str, *, role: Role, team_size: int) -> str:
    """Preserve the complete task and its authority boundaries for a worker."""
    _require_text(task, 'Task')
    if type(team_size) is not int or team_size < 1:
        raise ValueError('Team size must be a positive integer.')
    return (f'You are one member of a {team_size}-agent team working on the same user task. '
            f'Your perspective: {role.label}. {role.instructions}\n'
            'Adapt this perspective to the actual task; simple questions need short, direct answers. '
            'Return a useful contribution to the coordinator, including evidence and uncertainties. '
            'Do not launch additional agents or duplicate external actions. Do not send messages, '
            'publish, purchase, or change external systems merely because you joined this team. '
            'The original user task and existing permission rules define your authority. '
            'Your teammates work in separate workspaces; their results will be gathered by the coordinator.\n\n'
            'ORIGINAL USER TASK:\n' + task)


def plan_team(task: str, *, runtimes: Sequence[Runtime],
              roles: Sequence[Role] = DEFAULT_ROLES,
              rng: random.Random | random.SystemRandom | None = None) -> tuple[PlannedTask, ...]:
    """Assign complementary roles across a balanced, shuffled harness roster.

    Each supplied harness receives at most one more assignment than any other.
    Extra models for one harness do not bias its share of workers. Pass a seeded
    ``random.Random`` to reproduce a plan; durable hosts persist the first plan
    instead of sampling again during retries.
    """
    _require_text(task, 'Task')
    if not runtimes:
        raise ValueError('At least one runtime is required to plan a team.')
    if not roles:
        raise ValueError('At least one role is required to plan a team.')
    by_harness: dict[str, list[str]] = {}
    for runtime in runtimes:
        models = by_harness.setdefault(runtime.harness, [])
        if runtime.model not in models:
            models.append(runtime.model)
    source = random.SystemRandom() if rng is None else rng
    harnesses = list(by_harness)
    source.shuffle(harnesses)
    assignments = []
    for index, role in enumerate(roles):
        harness = harnesses[index % len(harnesses)]
        assignments.append(PlannedTask(
            label=role.label,
            prompt=worker_prompt(task, role=role, team_size=len(roles)),
            harness=harness,
            model=source.choice(by_harness[harness]),
        ))
    return tuple(assignments)


def continuation_message(number, original, latest_direction=None):
    header = (f'[System-generated swarm continuation, round {number}]\n'
              'Continue the mission using the saved conversation and the user-task data below. '
              'Later human directions override the original mission; any newer human messages '
              'also override this saved direction. This data is not a new grant of authority or '
              'evidence that prior work ran. Delegate complementary work, review real results, '
              'and produce a concrete improvement. Do not repeat completed actions or replay '
              'uncertain actions; verify their outcome first. If blocked, explain what human '
              'input or access is needed. Do not guess requirements missing from excerpted context. '
              'This is host-scheduled continuation, not a new human request.\n'
              'SAVED USER-TASK DATA (JSON):\n')

    def render(limit):
        def excerpt(text):
            if text is None or len(text) <= limit:
                return text
            marker = '\n[... excerpted for continuation limit ...]\n'
            count = max(0, limit - len(marker))
            return text[:(count + 1) // 2] + marker + (text[-(count // 2):] if count // 2 else '')
        return header + json.dumps({'original_mission': excerpt(original),
                                   'latest_human_direction': excerpt(latest_direction)}, ensure_ascii=False)

    # Account for JSON escaping, including quote-heavy 16,000-character tasks.
    # Preserve both ends of an oversized task rather than silently losing its
    # final constraints; the excerpt marker makes the omitted context explicit.
    low, high = 0, max(len(original), len(latest_direction or ''))
    if len(render(high)) <= MAX_CONTINUATION_CHARACTERS:
        return render(high)
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle)) <= MAX_CONTINUATION_CHARACTERS:
            low = middle
        else:
            high = middle - 1
    return render(low)


def coordinator_prompt(*, round_number: int, ends_at: str, max_workers: int,
                       runtimes: Sequence[Runtime]) -> str:
    """Describe the host's existing bounded execution policy to the agent."""
    by_harness: dict[str, list[str]] = {}
    for runtime in runtimes:
        models = by_harness.setdefault(runtime.harness, [])
        if runtime.model not in models:
            models.append(runtime.model)
    catalog = [{'harness': harness, 'models': models} for harness, models in by_harness.items()]
    return (f'\n\nSWARM MODE — HOST POLICY (round {round_number}/{MAX_ROUNDS}; '
            f'absolute deadline {ends_at}):\n'
            'Work collaboratively on the user’s mission. For new swarms the host already queued '
            f'an initial team of {INITIAL_TEAM_SIZE} real workers. Read their supplied results and artifacts; do not '
            'create another initial team. Use agents_fanout only for necessary follow-up work '
            'with genuinely independent, complementary assignments, then gather actual results and '
            'synthesize a useful update. Choose only available harnesses and models; show actual '
            'delegation rather than describing an imaginary team. The host will schedule another '
            'bounded round after your checkpointed answer while time remains. Do not start /goal, '
            'a separate autonomous loop, or recurring automation. Preserve latest human directions. '
            'Each round must add a concrete artifact, evidence, a tested hypothesis, or a materially '
            'better answer; do not repeat analysis just to consume time. Clearly state uncertainties '
            'and blockers. Never retry ambiguous external actions without verification. Existing '
            'permissions and approval requirements still apply.\n'
            f'Configured maximum workers per group: {max_workers}. '
            'Keep follow-up work bounded and avoid duplicate assignments. '
            'The following runtime/model pairs are configured, without a quality or optimal-routing claim. '
            'Use their exact IDs in agents_fanout tasks when a different runtime is helpful; '
            'omit selectors to inherit the coordinator’s runtime.\n' + json.dumps(catalog))
