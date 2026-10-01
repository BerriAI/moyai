"""Fault injection at the external-call, receipt and web-restart boundaries."""
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.config import Settings
from app.db import Store
from app.inference import InferenceJobs
from app.security import Security, digest
from app.spend import Spend
from inference.worker import decode, encode, execute


class Checkpoints:
    async def flush(self):
        pass


class Cloud:
    """Model connections live outside the Render observation task."""
    def __init__(self, settings):
        self.settings = settings
        self.files, self.claims, self.tasks = {}, set(), {}
        self.calls = 0
        self.lost_ack = False
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.cipher = Fernet(settings.inference_encryption_key.encode())
        self.fail_results = 0

    async def save(self, name, value):
        if name.endswith('.result') and self.fail_results:
            self.fail_results -= 1
            raise OSError('fault injection')
        self.files[name] = value

    async def claim(self, job_id):
        if job_id in self.claims:
            return False
        self.claims.add(job_id)
        return True

    async def gateway(self, request):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        body = json.loads(request.content)
        assert request.headers['authorization'] == 'Bearer test-key'
        assert body['metadata'] == {'moyai_request_id': request.headers['x-litellm-call-id']}
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.251452',
                             'x-litellm-call-id': request.headers['x-litellm-call-id']},
                             json={'id': 'gateway-reply', 'choices': [{'message': {'content': 'Saved answer'}}],
                                   'usage': {'prompt_tokens': 123, 'completion_tokens': 9, 'total_tokens': 132}})

    async def spawn(self, envelope):
        call_id = uuid4().hex
        self.tasks[call_id] = asyncio.create_task(execute(envelope, key=self.settings.litellm_api_key,
            base=self.settings.litellm_api_base, cipher=self.cipher, storage=self, claim=self.claim,
            client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(self.gateway), **kw)))
        if self.lost_ack:
            self.lost_ack = False
            raise OSError('spawn acknowledgment lost')
        return call_id

    async def read(self, job_id, suffix):
        return self.files.get(job_id + suffix)

    async def wait(self, job_id, call_id, seconds):
        try:
            await asyncio.wait_for(asyncio.shield(self.tasks[call_id]), seconds)
        except TimeoutError:
            pass

    async def finish(self):
        await asyncio.gather(*self.tasks.values())


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, temporal_enabled=True, durable_inference_enabled=True,
        inference_encryption_key=Fernet.generate_key().decode(), litellm_api_key='test-key',
        litellm_api_base='https://gateway.example/v1', encryption_key=Fernet.generate_key().decode(),
        public_url='http://127.0.0.1:8787')
    store = Store(tmp_path)
    checkpoints = Checkpoints()
    spend = Spend(store, settings, Security(settings), checkpoints)
    cloud = Cloud(settings)
    jobs = InferenceJobs(store, settings, spend, checkpoints, cloud)
    run = store.create_run('Private prompt', '', 'modal', [], chat_enabled=True, user_id='google:alice')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('capability'))
    return jobs, cloud, store.run(run['id'])


def admit(jobs, run, client_id=None):
    body = {'inference_id': client_id or uuid4().hex, 'messages': [{'role': 'user', 'content': 'Private prompt'}]}
    return jobs.admit(run, body, {**body, 'model': 'openai/gpt-6-astra'}, 'openai/gpt-6-astra'), body


async def test_restart_recovers_exact_answer_and_cost_without_repeating_inference(setup):
    jobs, cloud, run = setup
    job, body = admit(jobs, run)
    cloud.release.clear()
    observer = asyncio.create_task(jobs.response(job))
    await cloud.started.wait()
    observer.cancel()  # Render exits; the independent connection stays alive.
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert cloud.calls == 1
    # The originating session can stop or move to another person meanwhile.
    jobs.store.update_run(run['id'], status='cancelled', token_hash='')
    jobs.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run['id']))
    cloud.release.set()
    await cloud.finish()
    reopened = Store(jobs.settings.data_dir)
    successor = InferenceJobs(reopened, jobs.settings, jobs.spend, jobs.checkpoints, cloud)
    assert await successor.advance(job['id']) is True
    answer, streaming = await successor.response(job)
    assert answer['choices'][0]['message']['content'] == 'Saved answer'
    assert not streaming
    for _ in range(3):
        assert await successor.advance(job['id']) is True
    row = reopened.rows('SELECT * FROM model_requests')[0]
    assert row['cost'] == '0.251452' and row['total_tokens'] == 132
    assert row['user_id'] == 'google:alice' and row['key_hash'] == digest('test-key')
    assert row['status'] == 'completed' and row['cost_source'] == 'response_header'
    assert jobs.spend.report()['total']['spend'] == '0.251452'
    assert cloud.calls == 1 and len(reopened.rows('SELECT * FROM model_requests')) == 1
    assert reopened.run(run['id'])['model_calls'] == 1
    assert b'Private prompt' not in job['envelope']
    assert all(b'Saved answer' not in data for data in cloud.files.values())


async def test_lost_spawn_ack_and_concurrent_dispatch_never_repeat_gateway(setup):
    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    cloud.release.clear()
    cloud.lost_ack = True
    with pytest.raises(OSError):
        await jobs.advance(job['id'])
    await cloud.started.wait()
    jobs.store.execute('UPDATE inference_jobs SET dispatched=0 WHERE id=?', (job['id'],))
    successor = InferenceJobs(jobs.store, jobs.settings, jobs.spend, jobs.checkpoints, cloud)
    await asyncio.gather(jobs.advance(job['id']), successor.advance(job['id']))
    await asyncio.sleep(0)
    assert cloud.calls == 1
    cloud.release.set()
    await cloud.finish()
    assert await successor.advance(job['id']) is True
    assert jobs.spend.report()['total']['spend'] == '0.251452'


def test_duplicate_id_input_integrity_and_admission_are_atomic(setup):
    jobs, cloud, run = setup
    job, body = admit(jobs, run)
    assert jobs.admit(run, body, {'messages': []}, 'changed-model')['id'] == job['id']
    assert jobs.store.run(run['id'])['model_calls'] == 1
    with pytest.raises(HTTPException) as exc:
        jobs.existing(run, {**body, 'messages': []})
    assert exc.value.status_code == 409
    different_run = {**run, 'token_hash': digest('different-capability')}
    assert jobs.existing(different_run, body)[0] is None
    jobs.settings.max_concurrent_model_requests = 1
    with pytest.raises(HTTPException) as exc:
        admit(jobs, run)
    assert exc.value.status_code == 429
    assert len(jobs.store.rows('SELECT * FROM model_requests')) == 1
    assert jobs.store.run(run['id'])['model_calls'] == 1


async def test_key_rotation_rejects_unsubmitted_but_recovers_original_receipts(setup):
    jobs, cloud, run = setup
    finished, _ = admit(jobs, run)
    await jobs.advance(finished['id'], wait=3)
    rejected, _ = admit(jobs, run)
    jobs.settings.litellm_api_key = 'rotated-key'
    await jobs.advance(rejected['id'], wait=3)
    rows = jobs.store.rows('SELECT * FROM model_requests ORDER BY rowid')
    assert rows[0]['cost'] == '0.251452' and rows[0]['key_hash'] == digest('test-key')
    assert rows[1]['cost'] == '0' and rows[1]['cost_source'] == 'not_submitted'
    assert cloud.calls == 1
    assert jobs.spend.report()['total']['spend'] == '0'  # current-key filter unchanged
    assert (await jobs.response(finished))[0]['id'] == 'gateway-reply'


async def test_header_receipt_survives_worker_death_and_late_result_can_backfill(setup):
    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    original_save = cloud.save
    async def kill_after_headers(name, data):
        await original_save(name, data)
        if name.endswith('.headers'):
            raise asyncio.CancelledError()
    cloud.save = kill_after_headers
    await jobs.advance(job['id'])
    await asyncio.gather(*cloud.tasks.values(), return_exceptions=True)
    await jobs.advance(job['id'])
    row = jobs.store.rows('SELECT * FROM model_requests')[0]
    assert row['cost'] == '0.251452' and not row['finished_at']
    jobs.store.execute('UPDATE inference_jobs SET created=? WHERE id=?', (time.time()-5000, job['id']))
    assert (await jobs.advance(job['id']))['retry_seconds'] == 3600
    assert jobs.get(job['id'])['status'] == 'unknown'
    # Simulate a previously committed final receipt becoming readable late.
    receipt = decode(cloud.cipher, cloud.files[job['id'] + '.headers'])
    receipt.update(status='completed', finished=time.time(), response={'id':'late', 'choices':[]})
    cloud.files[job['id'] + '.result'] = encode(cloud.cipher, receipt)
    assert await jobs.advance(job['id']) is True
    assert (await jobs.response(job))[0]['id'] == 'late'
    assert cloud.calls == 1 and jobs.spend.report()['total']['spend'] == '0.251452'


async def test_expired_claim_cannot_rebill_and_uncertain_cost_is_not_zero(setup):
    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    envelope = decode(cloud.cipher, job['envelope'])
    envelope['created'] = time.time() - 8 * 86400
    await cloud.spawn(encode(cloud.cipher, envelope))
    await cloud.finish()
    assert cloud.calls == 0 and not cloud.claims
    jobs.store.execute('UPDATE inference_jobs SET created=? WHERE id=?', (envelope['created'], job['id']))
    await jobs.advance(job['id'])
    row = jobs.store.rows('SELECT * FROM model_requests')[0]
    assert row['cost'] is None and row['status'] == 'unknown'
    assert jobs.spend.report()['total']['missing_costs'] == 1


async def test_receipt_identity_mismatch_and_disabled_admissions(setup):
    jobs, cloud, run = setup
    job, body = admit(jobs, run)
    jobs.settings.durable_inference_enabled = False
    with pytest.raises(HTTPException) as exc:
        admit(jobs, run)
    assert exc.value.status_code == 409
    assert jobs.admit(run, body, {}, 'ignored')['id'] == job['id']
    await jobs.advance(job['id'], wait=3)
    receipt = decode(cloud.cipher, jobs.get(job['id'])['result'])
    receipt['key_hash'] = digest('someone-else')
    with pytest.raises(ValueError):
        jobs.import_receipt(job, encode(cloud.cipher, receipt), final=True)
    assert jobs.spend.report()['total']['spend'] == '0.251452'


async def test_store_failure_retries_only_receipt_not_inference(setup):
    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    cloud.fail_results = 1
    await jobs.advance(job['id'], wait=5)
    assert cloud.calls == 1 and jobs.get(job['id'])['recovered'] == 1
    assert jobs.spend.report()['total']['spend'] == '0.251452'


async def test_recovery_settings_cannot_silently_orphan_pending_jobs(setup):
    jobs, cloud, run = setup
    admit(jobs, run)
    jobs.settings.durable_inference_enabled = False
    jobs.settings.temporal_enabled = False
    with pytest.raises(ValueError, match='pending jobs'):
        InferenceJobs(jobs.store, jobs.settings, jobs.spend, jobs.checkpoints, cloud)


async def test_real_temporal_recovers_offline_receipt_and_history_has_only_ids(setup):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer
    from app.inference_workflow import InferenceWorkflow
    from app.temporal_runtime import TemporalRunManager

    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    cloud.release.clear()
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager = TemporalRunManager(jobs.store, jobs.settings)
        manager.inference = jobs
        manager.temporal = env.client
        async with manager.make_worker(env.client):
            await manager.dispatch()
            async with asyncio.timeout(20):
                await cloud.started.wait()
        # Temporal worker and web application are both offline at completion.
        cloud.release.set()
        await cloud.finish()
        assert jobs.get(job['id'])['recovered'] == 0
        reopened = Store(jobs.settings.data_dir)
        successor = TemporalRunManager(reopened, jobs.settings)
        successor.inference = InferenceJobs(reopened, jobs.settings, jobs.spend, jobs.checkpoints, cloud)
        successor.temporal = env.client
        async with successor.make_worker(env.client):
            # Simulate a lost workflow-start acknowledgment too.
            jobs.store.execute('UPDATE inference_jobs SET workflow_sent=0 WHERE id=?', (job['id'],))
            await successor.dispatch()
            handle = env.client.get_workflow_handle('moyai-inference-' + job['id'])
            await asyncio.wait_for(handle.result(), timeout=30)
            assert jobs.get(job['id'])['recovered'] == 1
            assert jobs.spend.report()['total']['spend'] == '0.251452' and cloud.calls == 1
            history = await handle.fetch_history()
            await Replayer(workflows=[InferenceWorkflow]).replay_workflow(history)
            text = history.to_json()
            for private in ('test-key', 'Private prompt', 'Saved answer', jobs.settings.inference_encryption_key):
                assert private not in text


async def test_gateway_connection_loss_is_unknown_and_never_blindly_retried(setup):
    jobs, cloud, run = setup
    job, _ = admit(jobs, run)
    async def lost_response(request):
        cloud.calls += 1
        raise httpx.ReadError('response lost after submission')
    cloud.gateway = lost_response
    await jobs.advance(job['id'], wait=3)
    row = jobs.store.rows('SELECT * FROM model_requests')[0]
    assert row['status'] == 'unknown' and row['cost'] is None
    with pytest.raises(HTTPException) as exc:
        await jobs.response(job)
    assert exc.value.status_code == 400  # Do not trigger an SDK's automatic 409/5xx retry.
    await jobs.advance(job['id'])
    await cloud.spawn(job['envelope'])  # Even a duplicate Modal launch cannot replay.
    await cloud.finish()
    assert cloud.calls == 1


async def test_small_receipt_is_durable_before_archive_and_large_receipt_waits(tmp_path):
    from inference.storage import FAST_RECEIPT_LIMIT, ReceiptStorage
    ledger = {}
    committed = asyncio.Event()
    started = asyncio.Event()
    async def put(name, data):
        ledger[name] = data
    async def commit():
        started.set()
        await committed.wait()
    storage = ReceiptStorage(tmp_path, put, commit)
    task = asyncio.create_task(storage.save('small.result', b'encrypted-receipt'))
    await started.wait()
    assert ledger['small.result'] == b'encrypted-receipt' and not task.done()
    committed.set()
    await task
    assert (tmp_path / 'small.result').read_bytes() == ledger['small.result']
    large = b'x' * (FAST_RECEIPT_LIMIT + 1)
    await storage.save('large.result', large)
    assert 'large.result' not in ledger
    assert (tmp_path / 'large.result').read_bytes() == large
