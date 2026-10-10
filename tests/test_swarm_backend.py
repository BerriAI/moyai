"""The public agent API uses Moyai's real durable delegation machinery."""
from datetime import datetime, timedelta, timezone

import pytest

from agent.swarm import Agent, AgentSwarm, Harness, Task
from app.agents import Assignment, Fanout
from app.db import Store, now
from app.swarms import create_in
from app.temporal_runtime import TemporalRunManager
from test_agents import attach
from test_durable import durable, drive


async def start_owner(durable):
    manager, cloud, owner = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, owner, phase='monitor')
    return manager, cloud, owner, coordinator


def test_broker_and_python_api_share_the_same_request_contract():
    from agent.swarm.contracts import Fanout as AgentFanout
    assert Task is Assignment
    assert AgentFanout is Fanout


def test_saved_swarm_prompt_skips_removed_runtime_catalog_entries(durable):
    manager, _, owner = durable
    with manager.store.connect() as conn:
        create_in(conn, owner, 60, now())
    manager.settings.swarm_models = 'removed-model,anthropic/claude-opus-5-5'
    prompt = manager.swarms.prompt(owner)
    assert 'anthropic/claude-opus-5-5' in prompt
    assert 'removed-model' not in prompt
    with pytest.raises(ValueError):
        manager.swarms.initial_plan('Review the proposal.', 'anthropic/claude-opus-5-5')


async def test_sdk_dispatch_survives_host_replacement_and_returns_saved_handoff(durable):
    manager, cloud, owner, coordinator = await start_owner(durable)
    team = coordinator.swarm(owner)
    assert isinstance(team, AgentSwarm)
    agents = [Agent(name='Research', task='Find the evidence.', harness=Harness.CODEX, model='sol'),
              Agent(name='Review', task='Challenge the assumptions.', harness=Harness.CLAUDE_AGENT_SDK, model='opus')]
    run = await team.start(request_key='sdk-review', agents=agents)
    again = await team.start(request_key='sdk-review', agents=agents)
    assert run.id == again.id and run.checkpoint_required
    assert len(run.children) == 2 and cloud.snapshots == 1
    pending = await run.result()
    assert not pending.settled and pending.total == 2
    assert {child.harness for child in pending.children} == {'codex', 'claude-agent-sdk'}
    with pytest.raises(ValueError, match='different work'):
        await team.start(request_key='sdk-review', tasks=[Task(label='Changed', prompt='Different task.')])

    # Even a host-side Python caller without the broker relay marker is caught
    # by the durable owner's pending-group guard at its natural completion.
    await drive(manager, owner, phase='waiting_children')
    assert not any(machine.alive for machine in cloud.machines)
    for child in coordinator.children(run.id):
        await drive(manager, child['id'])
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    restored_coordinator = attach(replacement)
    await drive(replacement, owner, phase='monitor')
    restored = restored_coordinator.swarm(owner).group(run.id)
    result = await restored.result()
    assert result.settled and result.completed == result.total == 2
    assert result.result_scope == 'handoff'
    assert all(child.summary == 'Saved answer' for child in result.children)
    assert len(restored_coordinator.children(run.id)) == 2


async def test_sdk_is_scoped_to_its_owner_and_children_can_own_their_own_team(durable):
    manager, _, owner, coordinator = await start_owner(durable)
    parent_team = await coordinator.swarm(owner).start(request_key='parent-team', tasks=[
        Task(label='Lead', prompt='Review the assigned work.')])
    child = coordinator.children(parent_team.id)[0]['id']
    await drive(manager, child, phase='monitor')
    child_swarm = coordinator.swarm(child)
    with pytest.raises(ValueError, match='does not belong'):
        await child_swarm.group(parent_team.id).result()
    nested = await child_swarm.start(request_key='child-team', tasks=[
        Task(label='Verifier', prompt='Verify the assigned evidence.')])
    assert coordinator.group(child, nested.id)['parent_id'] == child
    grandchild = coordinator.children(nested.id)[0]['id']
    assert manager.store.run(grandchild)['parent_run_id'] == child
    with pytest.raises(ValueError, match='does not belong'):
        await coordinator.swarm(owner).group(nested.id).cancel()


async def test_sdk_cannot_extend_owner_deadline_or_launch_after_it(durable):
    manager, _, owner, coordinator = await start_owner(durable)
    with manager.store.connect() as conn:
        create_in(conn, owner, 60, now())
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    manager.store.execute('UPDATE swarm_missions SET ends_at=? WHERE run_id=?', (past, owner))
    with pytest.raises(ValueError, match='time budget has ended'):
        await coordinator.swarm(owner).start(request_key='too-late', tasks=[
            Task(label='Late', prompt='Do not start this work.')])
    assert not manager.store.rows('SELECT id FROM agent_groups WHERE parent_id=?', (owner,))


async def test_sdk_cancel_preserves_worker_records_and_uses_normal_cascade(durable):
    manager, _, owner, coordinator = await start_owner(durable)
    run = await coordinator.swarm(owner).start(request_key='cancel-team', tasks=[
        Task(label='Review', prompt='Review the proposal.')])
    result = await run.cancel()
    assert result.status == 'cancelled'
    assert len(coordinator.children(run.id)) == 1
    child = coordinator.children(run.id)[0]['id']
    await manager.advance(child)
    assert (await run.result()).settled
    assert coordinator.children(run.id)[0]['status'] == 'cancelled'
