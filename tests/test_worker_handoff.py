"""Real Temporal/PostgreSQL mixed-build handoff; deterministic sandbox boundary."""
import asyncio
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from temporalio.testing import WorkflowEnvironment
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import Replayer

from app.config import Settings
from app.main import create_app
from app.schema_migrations import migrate
from app.session_workflow import SessionWorkflow
from test_durable import Cloud
from test_postgres_runtime import target  # noqa: F401
from test_temporal_integration import eventually


async def test_worker_replacement_reuses_live_sandbox_and_rollback_handles_new_writes(target, tmp_path, monkeypatch):
    url, schema = target
    settings = Settings(_env_file=None, data_dir=tmp_path,
        moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
        moyai_runtime_role='coordinator', moyai_separate_broker=True, moyai_build_sha='a' * 40,
        temporal_enabled=True, temporal_tls=False, object_storage_bucket='synthetic-worker-handoff',
        session_secret='synthetic-worker-key', encryption_key=Fernet.generate_key().decode(),
        agent_model='test-model', sandbox_idle_seconds=0, session_titles_enabled=False,
        memory_review_enabled=False, litellm_spend_recovery_enabled=False)
    migrate(settings)
    settings = settings.model_copy(update={'moyai_schema_mode': 'verify'})
    control = create_app(settings)
    apps = [control]

    def worker(build):
        app = create_app(settings.model_copy(update={'moyai_runtime_role': 'worker', 'moyai_build_sha': build}))
        apps.append(app)
        return app.state.manager

    old, candidate = worker('a' * 40), worker('b' * 40)
    run = control.state.store.create_run('Keep the live sandbox', '', 'modal', [], chat_enabled=True)
    cloud = Cloud(monkeypatch, control.state.store, run['id'])
    cloud.finished = False
    for manager in (old, candidate):
        cloud.attach(manager)
    control.state.manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        for app in apps:
            manager = app.state.manager
            client = await Client.connect(env.client.service_client.config.target_host,
                runtime=Runtime(telemetry=TelemetryConfig()), identity=manager.identity)
            manager.connect_temporal = AsyncMock(return_value=client)
        try:
            await control.state.manager.recover()
            await old.recover()
            await old.wait_ready()
            await eventually(lambda: old.state(run['id']).get('phase') == 'monitor', seconds=30)
            token = control.state.store.run(run['id'])['token_hash']
            sandbox = cloud.machines[0].object_id
            await candidate.recover()
            await candidate.wait_ready()
            # Accepted while both releases are consuming the same task queue.
            control.state.store.enqueue_message(run['id'], 'Follow-up during upgrade', 'upgrade-followup')
            control.state.manager.submit(control.state.store.run(run['id']))
            await old.shutdown()
            assert cloud.machines[0].alive and not cloud.terminations
            assert control.state.store.run(run['id'])['token_hash'] == token
            assert candidate.state(run['id'])['sandbox_id'] == sandbox
            assert len(cloud.launches) == 1
            # The existing sandbox supervisor completes once; replacement only
            # resumes observation under the same session journal and capability.
            cloud.finished = True
            cloud.saving_before_answer = False
            await eventually(lambda: control.state.store.run(run['id'])['status'] == 'idle')
            messages = control.state.store.messages(run['id'])
            assert sum(m['role'] == 'assistant' for m in messages) == 2
            assert len(cloud.launches) == len(cloud.machines) == 2
            handle = env.client.get_workflow_handle('moyai-session-' + run['id'])
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())

            # Rollback is another overlapping worker, not a policy rewrite.
            rollback = cloud.attach(worker('a' * 40))
            client = await Client.connect(env.client.service_client.config.target_host,
                runtime=Runtime(telemetry=TelemetryConfig()), identity=rollback.identity)
            rollback.connect_temporal = AsyncMock(return_value=client)
            cloud.finished = False
            control.state.store.enqueue_message(run['id'], 'Turn admitted by candidate', 'rollback-followup')
            control.state.manager.submit(control.state.store.run(run['id']))
            await eventually(lambda: candidate.state(run['id']).get('phase') == 'monitor')
            await rollback.recover()
            await rollback.wait_ready()
            await candidate.shutdown()
            assert cloud.machines[-1].alive and len(cloud.launches) == 3
            cloud.finished = True
            await eventually(lambda: control.state.store.run(run['id'])['status'] == 'idle')
            assert sum(m['role'] == 'assistant' for m in control.state.store.messages(run['id'])) == 3
            assert len(cloud.launches) == len(cloud.machines) == len(cloud.terminations) == 3
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
            await handle.terminate('Synthetic handoff complete')
        finally:
            for app in reversed(apps):
                await app.state.manager.shutdown()
                await app.state.manager.modal_clients.close()
                app.state.store.close()
