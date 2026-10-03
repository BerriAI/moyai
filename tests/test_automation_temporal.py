"""Exercise real Temporal Schedules and worker replacement without paid inference."""
import asyncio
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.automation_workflow import AutomationWorkflow
from app.automations import Automations, Definition, Save
from app.automation_events import EventTrigger
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


async def test_event_receipt_recovers_on_replacement_without_duplicate_session(tmp_path):
    settings=Settings(_env_file=None,data_dir=tmp_path,public_url='http://127.0.0.1:8787',
                      session_secret='stable-test-key',temporal_enabled=True,
                      agent_model='test-model',demo_step_seconds=0.01,sandbox_idle_seconds=0)
    store=Store(tmp_path,default_model='test-model')
    owner=store.identity({'method':'local','role':'admin'})
    original=TemporalRunManager(store,settings)
    security=Security(settings)
    service=Automations(store,settings,security,original,None,None,Checkpoints(store,settings))
    a=service.save(Save(definition=Definition(name='Webhook check',prompt='Read the event',mode='demo',
        event=EventTrigger(provider='webhook',event='build.ready'))),owner)
    store.execute('UPDATE automations SET paused=0 WHERE id=?',(a['id'],))
    await service.events.accept(service.row(a['id']),'accepted-before-restart',{'title':'PRIVATE EVENT CONTENT'})
    # Replacement creates a new manager over the saved DB, with no in-memory jobs.
    successor=TemporalRunManager(Store(tmp_path),settings)
    recovered=Automations(successor.store,settings,security,successor,None,None,Checkpoints(successor.store,settings))
    successor.automations=recovered
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():return env.client
        successor.connect_temporal=connect
        try:
            await successor.recover()
            await asyncio.wait_for(successor.ready.wait(),20)
            # No Temporal Schedule is created for an event definition.
            await recovered.sync(env.client)
            assert recovered.row(a['id'])['synced_revision']==1
            await recovered.events.dispatch()
            run_id=store.rows('SELECT run_id FROM automation_runs')[0]['run_id']
            await eventually(lambda: successor.store.run(run_id)['status']=='idle',seconds=30)
            assert (await recovered.events.accept(recovered.row(a['id']),'accepted-before-restart',{'title':'PRIVATE EVENT CONTENT'}))['status']=='duplicate'
            await recovered.events.dispatch()
            assert len(store.rows('SELECT * FROM runs'))==1
            assert store.run(run_id)['owner_id']==owner
            assert 'PRIVATE EVENT CONTENT' in store.run(run_id)['prompt']
            history=await env.client.get_workflow_handle('moyai-session-'+run_id).fetch_history()
            assert 'PRIVATE EVENT CONTENT' not in history.to_json()
            await env.client.get_workflow_handle('moyai-session-'+run_id).terminate('Test complete')
        finally:
            await successor.shutdown()


async def test_multiple_schedules_cron_and_once_on_real_temporal(tmp_path):
    from datetime import datetime, timezone
    from app.automations import Trigger, Timing
    settings=Settings(_env_file=None,data_dir=tmp_path,public_url='http://127.0.0.1:8787',
                      session_secret='test-stable-key',temporal_enabled=True,
                      agent_model='test-model',demo_step_seconds=.01,sandbox_idle_seconds=0)
    store=Store(tmp_path,default_model='test-model')
    owner=store.identity({'method':'local','role':'admin'})
    manager=TemporalRunManager(store,settings)
    service=Automations(store,settings,Security(settings),manager,None,None,Checkpoints(store,settings))
    manager.automations=service
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():return env.client
        manager.connect_temporal=connect
        try:
            await manager.recover()
            await asyncio.wait_for(manager.ready.wait(),20)
            due=(datetime.now(timezone.utc)+timedelta(seconds=5)).replace(microsecond=0)
            a=service.save(Save(definition=Definition(name='Combined schedule',prompt='Check work',mode='demo',triggers=[
                Trigger(id='cron',schedule=Timing(frequency='cron',cron='0 9 * * 1-5',timezone='UTC')),
                Trigger(id='once',schedule=Timing(frequency='once',run_at=due))])),owner)
            store.execute('UPDATE automations SET paused=0 WHERE id=?',(a['id'],))
            await service.sync(env.client)
            prefix='moyai-automation-'+a['id']+'-'
            cron=env.client.get_schedule_handle(prefix+'cron')
            once=env.client.get_schedule_handle(prefix+'once')
            assert (await cron.describe()).info.next_action_times
            await eventually(lambda:bool(store.rows("SELECT * FROM automation_runs WHERE outcome='started'")),seconds=20)
            run_id=store.rows("SELECT run_id FROM automation_runs WHERE outcome='started'")[0]['run_id']
            await eventually(lambda:store.run(run_id)['status']=='idle',seconds=20)
            assert (await once.describe()).schedule.state.remaining_actions==0
            # A later definition sync must not restore a consumed one-time action.
            store.execute('UPDATE automations SET revision=revision+1 WHERE id=?',(a['id'],))
            service.next_sync=0
            await service.sync(env.client)
            assert service.row(a['id'])['synced_revision']==2, service.row(a['id'])['sync_error']
            assert (await once.describe()).schedule.state.remaining_actions==0
            assert (await once.describe()).schedule.state.paused
            assert len(store.rows("SELECT * FROM automation_runs WHERE outcome='started'"))==1
            await cron.delete();await once.delete()
            await env.client.get_workflow_handle('moyai-session-'+run_id).terminate('Test complete')
        finally:
            await manager.shutdown()
