"""Demonstrate the premature-final guard with real coordinator/SQLite state.

Run: uv run python -m scripts.agent_completion_demo --delay 2
Model replies and cloud machines use the deterministic test fixtures. This does
not call a provider, launch real agents, or change production sessions.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

from app.db import Store
from app.temporal_runtime import TemporalRunManager
from test_agents import attach, launch, pause_parent
from test_durable import durable, drive


async def demonstrate(delay):
    async def show(title, evidence):
        print(f'\n{title}\n  {evidence}', flush=True)
        await asyncio.sleep(delay)

    print('MOYAI | Premature completion regression', flush=True)
    print('Real coordinator + SQLite. Scripted replies; simulated sandboxes.', flush=True)
    with TemporaryDirectory(prefix='moyai-completion-') as directory, pytest.MonkeyPatch.context() as patch:
        fixture = durable.__wrapped__(Path(directory), patch)
        manager, cloud, root = fixture
        coordinator, delegation, _ = await launch(fixture, count=1)
        group = delegation['group_id']
        child = coordinator.children(group)[0]['id']
        await pause_parent(manager, root, group)
        original = manager.state(root)['message_id']
        await show('1. Parent delegates and waits',
                   f"phase={manager.state(root)['phase']}; worker_count={len(coordinator.children(group))}")

        correction, _ = manager.store.enqueue_message(root, 'Apply the design skill.', 'demo-correction',
                                                      user_id='google:tin')
        manager.message_queue.change(root, correction['id'], 'google:tin', False, 0, 'steer')
        await drive(manager, root, phase='monitor')
        control = manager.message_queue.live_control(root, original, [])
        assert control['input']['id'] == correction['id']
        manager.message_queue.live_control(root, original, [correction['id']])
        machine = cloud.machines[-1]
        assert machine.spec['agent_results']['settled'] is False
        reply = "I've read the skill. I'll apply it."
        machine.operations[manager.directory(manager.state(root))].update(message=reply)
        await show('2. Follow-up wakes the parent; model ends its reply early', f'final="{reply}"')

        await drive(manager, root, phase='checkpointed')
        receipt = json.loads(manager.store.run(root)['pending_result'])
        assert receipt['completed'] is False and receipt['continuation'] is True
        assert receipt['wait_group'] == group
        await show('3. Completion guard preserves the unfinished task',
                   f"completed={receipt['completed']}; continuation={receipt['continuation']}; worker handoff pending")

        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        coordinator = attach(manager)
        await drive(manager, root, phase='waiting_children')
        assert manager.state(root)['message_id'] == original
        assert manager.store.messages(root)[0]['status'] == 'running'
        assert await manager.advance(root) == 'children'
        await show('4. Restart the worker process from the saved database',
                   f"phase={manager.state(root)['phase']}; original task still running")

        await drive(manager, child)
        await drive(manager, root, phase='monitor')
        results = cloud.machines[-1].spec['agent_results']
        assert results['result_scope'] == 'handoff' and results['settled'] is True
        await show('5. Workers finish; coordinator resumes with frozen results',
                   f"result_scope={results['result_scope']}; settled={results['settled']}")

        await drive(manager, root)
        messages = manager.store.messages(root)
        answers = [m for m in messages if m['role'] == 'assistant']
        assert len(answers) == 1 and all(m['status'] == 'completed' for m in messages)
        assert len(coordinator.children(group)) == 1
        assert len(cloud.launches) == 4
        await show('PASS: one final response, no duplicate workers',
                   'Original turn completed only after the worker handoff and resumed coordinator segment.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay', type=float, default=0, help='Seconds between observations for a recording.')
    args = parser.parse_args()
    if args.delay < 0:
        parser.error('--delay must be nonnegative')
    asyncio.run(demonstrate(args.delay))
