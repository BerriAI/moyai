"""Exercise real Temporal Schedules and worker replacement without paid inference."""
import asyncio
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.automation_workflow import AutomationWorkflow
from app.automations import Automations, Definition, Save
from app.config import Settings
from app.db import Store
from app.security import Security
from app.temporal_runtime import TemporalRunManager
from app.persistence import Checkpoints
from test_temporal_integration import eventually


async def test_real_schedule_launch_survives_replacement_and_skips_overlap(tmp_path):
    settings=Settings(_env_file=None,data_dir=tmp_path,public_url='http://127.0.0.1:8787',
                      session_secret='stable-test-key', temporal_enabled=True,
                      agent_model='test-model', demo_step_seconds=0.01, sandbox_idle_seconds=0)
    store=Store(tmp_path,default_model='test-model')
    owner=store.identity({'method':'local','role':'admin'})
    manager=TemporalRunManager(store,settings)
    security=Security(settings)
    automations=Automations(store,settings,security,manager,None,None,Checkpoints(store,settings))
    manager.automations=automations
    a=automations.save(Save(definition=Definition(name='Scheduled check',prompt='PRIVATE WORKFLOW CONTENT',mode='demo')),owner)
    store.execute('UPDATE automations SET paused=0 WHERE id=?',(a['id'],))
    # Hold the real session runner so the schedule action remains open.
    async def waiting(run_id): return {'retry_seconds':1}
    manager.advance=waiting
    successor=None
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():return env.client
        manager.connect_temporal=connect
        try:
            await manager.recover()
            await asyncio.wait_for(manager.ready.wait(),20)
            await automations.sync(env.client)
            handle=env.client.get_schedule_handle('moyai-automation-'+a['id'])
            description=await handle.describe()
            assert not description.schedule.state.paused
            assert description.info.next_action_times
            await handle.trigger()
            await eventually(lambda: bool(store.rows('SELECT run_id FROM automation_runs WHERE run_id IS NOT NULL')),seconds=25)
            run_id=store.rows('SELECT run_id FROM automation_runs WHERE run_id IS NOT NULL')[0]['run_id']
            assert store.run(run_id)['owner_id']==owner
            await handle.trigger()  # SKIP while the first scheduled session remains active.
            await asyncio.sleep(.5)
            assert len(store.rows('SELECT * FROM automation_runs'))==1
            occurrence=store.rows('SELECT occurrence FROM automation_runs')[0]['occurrence']
            await manager.shutdown()
            successor=TemporalRunManager(Store(tmp_path),settings)
            successor.connect_temporal=connect
            service=Automations(successor.store,settings,security,successor,None,None,Checkpoints(successor.store,settings))
            successor.automations=service
            await successor.recover()
            await eventually(lambda: successor.store.run(run_id)['status']=='idle',seconds=30)
            wrapper=env.client.get_workflow_handle(occurrence)
            assert (await asyncio.wait_for(wrapper.result(),25))['run_id']==run_id
            assert len(store.rows('SELECT * FROM automation_runs'))==1
            history=await wrapper.fetch_history()
            await Replayer(workflows=[AutomationWorkflow]).replay_workflow(history)
            assert 'PRIVATE WORKFLOW CONTENT' not in history.to_json()
            # Pause takes effect on the authoritative DB before scheduler delivery.
            store.execute('UPDATE automations SET paused=1,revision=2 WHERE id=?',(a['id'],))
            await service.sync(env.client)
            assert (await handle.describe()).schedule.state.paused
            await handle.delete()
            await env.client.get_workflow_handle('moyai-session-'+run_id).terminate('Test complete')
        finally:
            if successor:await successor.shutdown()
            await manager.shutdown()
