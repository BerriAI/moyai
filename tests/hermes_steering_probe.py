"""Real Hermes loop + Moyai steering, with deterministic model responses.

Runs in the separately installed pinned Hermes environment. File tools and
conversation persistence are real; model responses/cancellations are scripted.
No provider, cloud machine, or connected app is contacted.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from openai.types.chat import ChatCompletion
from run_agent import AIAgent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.continuation import ActiveTurnSteering
from sandbox.goals import GoalLoop, run_goal_conversation


def reply(content='', write=None):
    message = {'role': 'assistant', 'content': content}
    if write is not None:
        message['tool_calls'] = [{'id': f'write-{write}', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({
                'path': str(Path.cwd() / f'step-{write}.txt'), 'content': f'Completed step {write}\n'})}}]
    return ChatCompletion.model_validate({
        'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': 'fixture-model',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls' if write is not None else 'stop',
                     'message': message}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    })


def report(message):
    print(message, flush=True)


def guards(agent, result):
    from agent.turn_finalizer import finalize_turn
    from agent.turn_iteration_prep import apply_retry_restarts
    from agent.turn_retry_state import TurnRetryState

    # The finalizer must describe the cause, even when the last transcript item
    # is a completed tool result. Exercise it with a real tool transcript.
    tool_tail = result['messages'][:next(i for i, m in enumerate(result['messages']) if m['role'] == 'tool') + 1]
    for reason in ('redirect_restart_limit_exceeded', 'rebuilt_restart_limit_exceeded', 'unknown'):
        closed = finalize_turn(agent, final_response=None, api_call_count=1, interrupted=False,
            failed=False, messages=deepcopy(tool_tail), conversation_history=[],
            effective_task_id=agent.session_id, turn_id=result['turn_id'],
            user_message='Work', original_user_message='Work', _should_review_memory=False,
            _turn_exit_reason=reason)
        assert closed['failed'] and not closed['completed'], closed
        assert closed['final_response'], closed
        expected = 'pending_tool_result' if reason == 'unknown' else reason
        assert closed['turn_exit_reason'] == expected, closed
        if reason != 'unknown':
            assert 'tool result was still pending' not in closed['final_response'], closed
    report('PASS: explicit stop reasons survive tool-tail finalization; unknown exits stay visible')

    # Fallback rebuilds share the refunded-restart guard. A real response resets
    # it; repeated fallback attempts without a response must still be bounded.
    count = 0
    for i in range(4):
        agent.iteration_budget.consume()
        verdict = apply_retry_restarts(agent,
            _retry=TurnRetryState(restart_with_rebuilt_messages=True), response=None,
            interrupted=False, messages=[], conversation_history=[], user_message='Work',
            api_kwargs={}, current_turn_user_idx=0, final_response=None, retry_count=0,
            max_retries=3, api_call_count=1, restart_count=count, length_continue_retries=0,
            _preflight_compression_blocked=True, _turn_exit_reason='unknown')
        count = verdict.restart_count
        assert verdict.action == ('continue' if i < 3 else 'break')
    assert verdict._turn_exit_reason == 'rebuilt_restart_limit_exceeded'
    report('PASS: consecutive fallback rebuilds still stop at the existing limit')


def conversation(scenario, *, unpatched=False):
    corrections = [f'Please incorporate correction {i}.' for i in range(1, 7)]
    writes, calls, packet = [], [], {}
    steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: packet))

    def complete(call_id, name, args, result):
        assert name == 'write_file', name
        path = Path(args['path'])
        assert path.read_text() == f"Completed step {call_id.removeprefix('write-')}\n", result
        writes.append(call_id)
        report(f'File tool completed once: {path.name}')

    agent = AIAgent(model='fixture-model', provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='fixture-only', enabled_toolsets=['file'],
        max_iterations=30, quiet_mode=True, skip_memory=True, skip_background_review=True,
        skip_context_files=True, save_trajectories=False, cwd=str(Path.cwd()),
        tool_complete_callback=complete)
    agent._cached_system_prompt = 'Use the requested file tools, then reply.'
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent._api_max_retries = 3
    script = [reply(write=0)]
    if scenario == 'corrections':
        for i, correction in enumerate(corrections, 1):
            script.extend([correction, reply(write=i)])
        script.append(reply('All six corrections incorporated. Work completed.'))
    elif scenario == 'redirect-cap':
        script.extend(corrections[:4])
    elif scenario == 'provider-failure':
        script.extend([SimpleNamespace(choices=[], model='fixture-model', usage=None)] * 3)
    elif scenario == 'stop':
        script.append('STOP')
    elif scenario == 'guards':
        script.append(reply('Ready to verify stop guards.'))

    def model_call(kwargs, **stream_options):
        # This is the model transport boundary, as in upstream's redirect regression.
        # An unexpected retry fails the test instead of replaying a scripted tool.
        assert len(calls) < len(script), 'Unexpected model call / unbounded retry'
        step = script[len(calls)]
        calls.append(kwargs)
        if isinstance(step, str):
            if step == 'STOP':
                agent.interrupt()
            else:
                number = corrections.index(step) + 1
                packet['input'] = {'id': number, 'content': step}
                with steering.model_wait() as generation:
                    steering._poll(agent, boundary=False)
                    assert steering.cancelled(generation)
                    steering._poll(agent, boundary=False)  # Retry the same delivery.
                assert steering.receipts() == list(range(1, number + 1))
                report(f'Correction {number} accepted during the model request')
            raise InterruptedError('Fixture request cancelled')
        return step

    try:
        with patch.object(agent, '_interruptible_api_call', side_effect=model_call), \
                patch.object(agent, '_interruptible_streaming_api_call', side_effect=model_call), \
                patch('agent.retry_utils.jittered_backoff', return_value=0):
            goal = GoalLoop(Path.cwd() / 'goal.json', 'steering-fixture')
            result = run_goal_conversation(agent, 'Write the requested step files.', [], '', goal)
        users = [m['content'] for m in result['messages'] if m['role'] == 'user'][1:]
        if scenario == 'corrections' and not unpatched:
            assert result['completed'] is True, result
            assert not result['failed'] and not result['interrupted']
            assert result['final_response'].startswith('All six corrections incorporated.')
            assert len(calls) == len(script)
            assert writes == [f'write-{i}' for i in range(7)], writes
            assert users == ['[User correction to the current task]\n' + text for text in corrections], users
            assert 'pending_steer' not in result
            report('PASS: six corrections, seven real file writes, no replay, one final answer')
        elif scenario == 'corrections':
            assert result['completed'] is False
            assert len(calls) == 8, len(calls)
            assert writes == [f'write-{i}' for i in range(4)]
            assert result['turn_exit_reason'] == 'pending_tool_result', result
            report('REPRODUCED: unpatched runtime stops at correction 4 after successful tools')
        elif scenario == 'redirect-cap':
            assert not result['completed']
            assert len(calls) == 5 and writes == ['write-0']
            assert result['turn_exit_reason'] == 'redirect_restart_limit_exceeded', result
            assert len(users) == 4 and users[-1].endswith(corrections[3]), users
            assert 'pending_steer' not in result  # Preserved in checkpoint history instead.
            resumed_calls = []
            def resume(kwargs, **stream_options):
                resumed_calls.append(kwargs)
                assert json.dumps(kwargs['messages']).count(corrections[3]) == 1
                return reply('Resumed with the saved correction.')
            with patch.object(agent, '_interruptible_api_call', side_effect=resume), \
                    patch.object(agent, '_interruptible_streaming_api_call', side_effect=resume):
                resumed = run_goal_conversation(agent, 'continue', result['messages'], '', goal)
            assert resumed['completed'] and len(resumed_calls) == 1
            assert writes == ['write-0']  # Prior tools were not re-executed on resume.
            report('PASS: consecutive cancellations still stop; last correction is saved')
        elif scenario == 'provider-failure':
            assert not result['completed'] and result['failed']
            assert result['failure_reason'] == 'invalid_response', result
            assert len(calls) == 4 and writes == ['write-0']
            report('PASS: invalid provider responses stop after three attempts; no tools replayed')
        elif scenario == 'stop':
            assert not result['completed'] and result['interrupted']
            assert len(calls) == 2 and writes == ['write-0']
            report('PASS: explicit stop still interrupts immediately')
        elif scenario == 'guards':
            assert result['completed'] is True
            guards(agent, result)
        report('STEERING_PROOF ' + json.dumps({
            'scenario': scenario, 'completed': result['completed'], 'writes': len(writes),
            'corrections': len(users), 'model_attempts': len(calls),
            'reason': result.get('turn_exit_reason'), 'reply': result['final_response'],
        }))
    finally:
        steering.close()
        agent.close()


def background_compaction(url, model, enabled):
    from sandbox.hermes_harness import HermesAgent
    events = []
    activity = SimpleNamespace(start=lambda call, *args: events.append(('start', call)),
        complete=lambda call, *args: events.append(('complete', call)), commentary=lambda text: None)
    # App-tool discovery is unrelated to this proof; terminal execution and the
    # complete native model loop still use the released Hermes runtime.
    with patch('tools.mcp_tool_discovery.discover_mcp_tools', return_value=[]):
        agent = HermesAgent(spec={'model': model, 'max_iterations': 50, 'timeout': 90},
            relay=SimpleNamespace(url=url, context_window=lambda: {'live_compaction': enabled}),
            config={}, activity=activity, step=lambda: None, cwd=str(Path.cwd()))
    assert agent.compression_enabled is not enabled
    agent.agent._cached_system_prompt = 'Execute the requested fixture tools and retain their receipts.'
    try:
        result = agent.run_conversation('Perform each fixture step once, preserving all new results.',
            conversation_history=[], system_message='Execute the fixture terminal commands.')
        report('BACKGROUND_PROOF ' + json.dumps({'completed': result['completed'], 'events': events,
            'final_response': result['final_response'],
            'private_summary_visible': 'private-background-summary-marker' in json.dumps(result['messages'])}))
    finally:
        agent.close()


def read_recovery(url, model):
    from sandbox.hermes_harness import HermesAgent
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    completed_tools = []
    activity = SimpleNamespace(start=lambda *args: None,
        complete=lambda call, name, *args: completed_tools.append(name), commentary=lambda text: None)
    agent = HermesAgent(spec={'model': model, 'max_iterations': 10, 'timeout': 90},
        relay=SimpleNamespace(url=url, context_window=lambda: {}), config={}, activity=activity,
        step=lambda: None, cwd=str(Path.cwd()))
    agent.validate()
    agent.agent._cached_system_prompt = 'Perform the fixture tools exactly once, then return the final response.'
    agent.agent.compression_enabled = False
    try:
        result = agent.run_conversation('Complete the fixture write and repository lookup, then reply.',
            conversation_history=[], system_message='Retain completed tool receipts during read recovery.')
        report('READ_RECOVERY_PROOF ' + json.dumps({'completed': result['completed'],
            'final_response': result['final_response'], 'completed_tools': completed_tools}))
    finally:
        agent.close()
        shutdown_mcp_servers()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=['corrections', 'redirect-cap', 'provider-failure', 'stop', 'guards', 'background', 'read-recovery'])
    parser.add_argument('--unpatched', action='store_true')
    parser.add_argument('--broker-url')
    parser.add_argument('--model')
    parser.add_argument('--live-compaction', action='store_true')
    args = parser.parse_args()
    if args.scenario == 'background':
        background_compaction(args.broker_url, args.model, args.live_compaction)
    elif args.scenario == 'read-recovery':
        read_recovery(args.broker_url, args.model)
    else:
        conversation(args.scenario, unpatched=args.unpatched)
