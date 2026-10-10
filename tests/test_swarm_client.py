"""Contract tests for the agent-owned client without the Moyai application."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from agent.swarm import GroupResult, PlannedTask, Role, Runtime, Swarm, Task, plan_team
from agent.swarm.contracts import Assignment, Fanout


GROUP = 'a' * 32
CHILD = 'b' * 32


class Backend:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def receipt(**updates):
    return {'group_id': GROUP, 'children': [{'id': CHILD, 'label': 'Research'}],
            'moyai_wait_group': GROUP, **updates}


def result(**updates):
    return {'group_id': GROUP, 'status': 'completed', 'settled': True,
            'completed': 1, 'total': 1, 'result_scope': 'handoff',
            'children': [{'id': CHILD, 'agent_label': 'Research', 'status': 'idle',
                          'summary': 'The saved answer.', 'error': '', 'checkpoint_error': '',
                          'harness': 'codex', 'model': 'openai/example', 'active_model': 'openai/example',
                          'has_artifact': True, 'message_id': 3, 'session_url': '/#run=' + CHILD,
                          'future_artifact_metadata': {'version': 'v1'}}], **updates}


async def test_start_validates_contract_and_returns_checkpoint_receipt_without_waiting():
    backend = Backend(receipt())
    group = await Swarm(backend).start(request_key='research-1', instructions='Use evidence.', tasks=[
        Task(label='Research', prompt='Investigate the request.', harness='codex', model='openai/example'),
    ])
    assert group.id == GROUP and group.checkpoint_required
    assert group.children[0].id == CHILD and group.children[0].label == 'Research'
    assert len(backend.calls) == 1
    name, arguments = backend.calls[0]
    assert name == 'agents_fanout'
    assert arguments == Fanout(request_key='research-1', instructions='Use evidence.', workers=10, tasks=[
        Assignment(label='Research', prompt='Investigate the request.', harness='codex', model='openai/example'),
    ]).model_dump()
    assert Task is Assignment
    assert Fanout.model_json_schema()['$defs']['Assignment']['title'] == 'Assignment'


async def test_partition_request_and_invalid_input_never_reach_backend():
    backend = Backend(receipt())
    swarm = Swarm(backend)
    await swarm.start(request_key='batch-1', items=['a', 'b', 'c'], instructions='Review each item.',
                      workers=2, harness='hermes')
    payload = backend.calls[0][1]
    assert payload['workers'] == 2 and payload['items'] == ['a', 'b', 'c']
    for arguments in [
        {'request_key': 'bad key', 'items': ['a'], 'instructions': 'Review.'},
        {'request_key': 'empty'},
        {'request_key': 'mixed', 'items': ['a'], 'tasks': [Task(label='One', prompt='Review it.')]},
        {'request_key': 'too-many', 'items': ['a'], 'instructions': 'Review.', 'workers': 101},
    ]:
        with pytest.raises(ValidationError):
            await swarm.start(**arguments)
    assert len(backend.calls) == 1


async def test_planner_output_submits_through_shared_contract_with_exact_allowed_pairs():
    runtimes = [Runtime('codex', 'openai/example'), Runtime('hermes', 'anthropic/example')]
    planned = plan_team('Develop a useful answer.', runtimes=runtimes,
                        roles=[Role('Research', 'Find evidence.'), Role('Critic', 'Challenge assumptions.')])
    assert all(isinstance(task, PlannedTask) for task in planned)
    backend = Backend(receipt())
    group = await Swarm(backend).start(request_key='planned-team', tasks=planned)
    assert group.checkpoint_required
    name, payload = backend.calls[0]
    validated = Fanout.model_validate(payload)
    assert name == 'agents_fanout'
    assert {(task.harness, task.model) for task in validated.tasks} == {
        (runtime.harness, runtime.model) for runtime in runtimes
    }
    assert [task.prompt for task in validated.tasks] == [task.prompt for task in planned]
    assert [task.label for task in validated.tasks] == ['Research', 'Critic']


async def test_planned_tasks_retain_wire_input_caps_before_backend_submission():
    backend = Backend()
    planned = plan_team('x' * 12000, runtimes=[Runtime('codex', 'openai/example')])
    with pytest.raises(ValidationError):
        await Swarm(backend).start(request_key='oversized-plan', tasks=planned)
    assert not backend.calls


async def test_restore_reads_frozen_handoff_by_default_and_preserves_unknown_metadata():
    backend = Backend(result(), result(result_scope='latest'))
    group = Swarm(backend).group(GROUP)
    assert not backend.calls and not group.checkpoint_required and not group.children
    frozen = await group.result()
    assert isinstance(frozen, GroupResult)
    assert frozen.successful and not frozen.failed and frozen.settled
    worker = frozen.children[0]
    assert (worker.summary, worker.harness, worker.model) == ('The saved answer.', 'codex', 'openai/example')
    assert worker.has_artifact and worker.message_id == 3
    assert worker.model_dump()['future_artifact_metadata'] == {'version': 'v1'}
    latest = await group.result(latest=True)
    assert latest.result_scope == 'latest'
    assert backend.calls == [
        ('agents_results', {'group_id': GROUP, 'latest': False}),
        ('agents_results', {'group_id': GROUP, 'latest': True}),
    ]


@pytest.mark.parametrize('status,error,checkpoint_error', [
    ('failed', '', ''), ('interrupted', '', ''), ('cancelled', '', ''),
    ('idle', 'Host error', ''), ('idle', '', 'Files were not saved'),
])
async def test_settled_failed_workers_are_never_reported_successful(status, error, checkpoint_error):
    value = result()
    value['children'][0].update(status=status, error=error, checkpoint_error=checkpoint_error)
    snapshot = await Swarm(Backend(value)).group(GROUP).result()
    assert snapshot.settled and snapshot.failed and not snapshot.successful


async def test_pending_unknown_worker_status_is_preserved_without_success_claim():
    value = result(settled=False, status='running', completed=0, future_version=2)
    value['children'][0]['status'] = 'future-provider-wait'
    snapshot = await Swarm(Backend(value)).group(GROUP).result()
    assert not snapshot.successful and not snapshot.settled
    assert snapshot.children[0].status == 'future-provider-wait'
    assert snapshot.model_dump()['future_version'] == 2


async def test_cancel_and_explicit_retry_use_same_owner_backend_and_persisted_group():
    backend = Backend(result(status='cancelled', settled=False), receipt())
    group = Swarm(backend).group(GROUP)
    cancelled = await group.cancel()
    assert cancelled.failed and not cancelled.successful
    resumed = await group.retry(request_key='recover-1', child_ids=[CHILD],
                                instructions='Verify the previous outcome before continuing.')
    assert resumed.id == GROUP and resumed.checkpoint_required
    assert not group.checkpoint_required
    assert backend.calls == [
        ('agents_cancel', {'group_id': GROUP}),
        ('agents_retry', {'group_id': GROUP, 'request_key': 'recover-1', 'child_ids': [CHILD],
                          'instructions': 'Verify the previous outcome before continuing.'}),
    ]


async def test_owner_authorization_and_transport_errors_propagate_without_replay():
    failure = PermissionError('The group does not belong to this owner.')
    backend = Backend(failure)
    with pytest.raises(PermissionError) as captured:
        await Swarm(backend).group(GROUP).result()
    assert captured.value is failure and len(backend.calls) == 1
    timeout = TimeoutError('Delegation outcome is unknown.')
    backend = Backend(timeout)
    with pytest.raises(TimeoutError) as captured:
        await Swarm(backend).start(request_key='stable-key', tasks=[Task(label='Review', prompt='Review the task.')])
    assert captured.value is timeout and len(backend.calls) == 1


async def test_invalid_or_mismatched_backend_receipts_fail_instead_of_claiming_started():
    for value in [{}, {'error': 'denied'}, {'isError': True}, [],
                  receipt(moyai_wait_group='c' * 32), receipt(children=[{'id': 'invalid', 'label': 'bad'}])]:
        with pytest.raises((ValueError, RuntimeError)):
            await Swarm(Backend(value)).start(request_key='start-1', tasks=[Task(label='Review', prompt='Review task.')])
    for operation, value in [('result', result(group_id='c' * 32)), ('cancel', result(group_id='c' * 32))]:
        with pytest.raises(ValueError, match='different group'):
            await getattr(Swarm(Backend(value)).group(GROUP), operation)()
    with pytest.raises(ValueError, match='different group'):
        await Swarm(Backend(receipt(group_id='c' * 32, moyai_wait_group='c' * 32))).group(GROUP).retry(
            request_key='retry-1', child_ids=[CHILD], instructions='Verify before retrying.')


def test_group_id_is_validated_before_io():
    backend = Backend()
    with pytest.raises(ValidationError):
        Swarm(backend).group('../foreign-group')
    assert not backend.calls


def test_public_api_imports_without_application_or_host_runtime():
    script = '''
import importlib.abc
import sys
class BlockHost(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'sandbox', 'fastapi', 'temporalio', 'modal'}:
            raise AssertionError('Unexpected host dependency: ' + fullname)
sys.meta_path.insert(0, BlockHost())
from agent.swarm import Swarm, SwarmBackend, SwarmRun, Task
print(Task(label='Research', prompt='Investigate this task.').model_dump_json())
'''
    completed = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout)['label'] == 'Research'
