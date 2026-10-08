from types import SimpleNamespace

import pytest

from app.config import Settings
from sandbox.continuation import RotationDeadline, resumed_context


def test_resume_context_does_not_claim_pending_workers_or_keys_have_finished():
    pending = resumed_context({'agent_results': {'group_id':'group', 'settled':False, 'children':[]},
                               'credential_resolution': {'status':'pending'}})
    assert 'STILL RUNNING' in pending and 'STILL PENDING' in pending
    assert 'HAVE SETTLED' not in pending and 'REQUEST RESOLVED' not in pending
    assert 'agents_wait' in pending and 'duplicate' in pending
    settled = resumed_context({'agent_results': {'settled':True, 'children':[]},
                               'credential_resolution': {'status':'declined'}})
    assert 'HAVE SETTLED' in settled and 'REQUEST RESOLVED' in settled
    satisfied = resumed_context({'credential_resolution': {'status': 'satisfied'}})
    assert 'REQUEST RESOLVED' in satisfied and 'verified' in satisfied.lower() and 'access' in satisfied.lower()
    assert 'no credential was stored or granted' in satisfied.lower()


def test_rotation_requests_stop_only_at_next_safe_step_and_checks_tool_results():
    now = [0]
    stops = []
    deadline = RotationDeadline(10, clock=lambda: now[0])
    agent = SimpleNamespace(interrupt=lambda: stops.append('stop'))
    deadline.step(agent)
    now[0] = 11
    assert not stops  # No timer interrupts in-flight work.
    deadline.step(agent)
    deadline.step(agent)
    assert stops == ['stop']
    history = [{'role':'assistant', 'tool_calls':[{'id':'call1'}]}]
    result = {'interrupted': True, 'messages': history}
    assert not deadline.can_continue(result)
    history.append({'role':'tool', 'tool_call_id':'call1', 'content':'Already done'})
    assert deadline.can_continue(result)
    assert not deadline.can_continue({**result, 'failed': True})
    assert not deadline.can_continue({**result, 'interrupted': False, 'completed': True})
    assert not deadline.can_continue({'interrupted':True})


def test_disabled_rotation_and_explicit_duration_limit():
    deadline = RotationDeadline(0)
    deadline.step(SimpleNamespace(interrupt=lambda: pytest.fail('Unexpected interrupt')))
    settings = Settings(_env_file=None, run_timeout_seconds=1800)
    assert settings.sandbox_lifetime_seconds() == 2040
    assert Settings(_env_file=None, run_timeout_seconds=0).sandbox_lifetime_seconds() == 86400
    with pytest.raises(ValueError):
        Settings(_env_file=None, run_timeout_seconds=10)


def test_slow_agent_initialization_cannot_loop_without_doing_work():
    clock = [0]
    stops = []
    deadline = RotationDeadline(10, clock=lambda: clock[0])
    clock[0] = 1000  # Startup took longer than the checkpoint interval.
    agent = SimpleNamespace(interrupt=lambda: stops.append(True))
    deadline.step(agent)
    assert not stops
    clock[0] += 11
    deadline.step(agent)
    assert stops == [True]
