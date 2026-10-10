"""Portable goal state transitions; persistence and authorization belong to app/."""
from copy import deepcopy
from uuid import uuid4

OPEN = {'active', 'wrapping', 'paused', 'needs_input', 'interrupted'}


def create(objective, source, *, now, budget=None):
    return dict(version=2, id=uuid4().hex, objective=objective, source_input_id=source,
                directive_revision=1, status='active', reason='', elapsed_seconds=0,
                created_at=now, updated_at=now, budget=budget or {}, guidance=[],
                progress=None, review=None, report=None)


def advance(state, *, now, elapsed=0):
    state = deepcopy(state)
    if state['status'] == 'active':
        state['elapsed_seconds'] += max(0, elapsed)
        budget = state['budget']
        seconds = budget.get('seconds')
        if ((budget.get('kind') in {'timebox', 'cap'} and seconds is not None
             and state['elapsed_seconds'] >= seconds) or
                (budget.get('deadline') is not None and now >= budget['deadline'])):
            state.update(status='wrapping', reason='The authorized work budget ended.',
                         terminal_reason='budget_exhausted')
    state['updated_at'] = now
    return state


def floor_unmet(state):
    return (state['budget'].get('kind') in {'floor', 'timebox'} and
            state['elapsed_seconds'] < state['budget'].get('seconds', 0))


def verdict(state, result):
    state = deepcopy(state)
    previous = state.get('review') or {}
    state['review'] = result
    if state['status'] != 'active':
        return state
    decision = result['verdict']
    if decision == 'complete' and not floor_unmet(state):
        state.update(status='wrapping', terminal_reason='completed', reason=result['reasons'])
    elif decision == 'needs_input':
        # As in QM: first change approach; only a repeated, independently
        # confirmed external blocker ends work awaiting the human.
        if previous.get('verdict') == 'step_back':
            state.update(status='wrapping', terminal_reason='needs_input', reason=result['reasons'])
        else:
            state['review'] = {**result, 'verdict': 'step_back'}
    return state


def fallback_report(state, execution_reason=''):
    """Factual, inference-free accounting; claims are never promoted to evidence."""
    progress = state.get('progress') or {}
    reason = execution_reason or state.get('reason') or 'Execution ended before completion was verified.'
    lines = ['Goal report', '', 'Objective: ' + state['objective'], '',
             f"Recorded active work: {state['elapsed_seconds']:.1f} seconds.",
             'Outcome: ' + reason]
    if state.get('terminal_reason') != 'completed':
        lines.append('Completion has not been verified.')
    if progress.get('note'):
        lines += ['', 'Last saved progress (agent report):', progress['note']]
    if progress.get('remaining'):
        lines += ['', 'Remaining work:', progress['remaining']]
    if progress.get('evidence'):
        lines += ['', 'Saved evidence: ' + ', '.join('journal item ' + str(n) for n in progress['evidence'])]
    if not progress:
        lines += ['', 'No progress checkpoint was saved. The conversation and tool receipts remain available in this session.']
    return '\n'.join(lines)
