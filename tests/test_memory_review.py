"""Exercise real turn settlement, gateway transport, private storage and recall."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.db import Store
from app.main import create_app
from app.memory import Note
from app.model_slots import ModelSlots
from app.model_selection import ASTRA, ASTRA_ULTRAFAST


@pytest.fixture
def review_app(tmp_path):
    values = {k: f.get_default(call_default_factory=True) for k, f in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, litellm_api_base='https://gateway.example/v1', litellm_api_key='test-key',
                  google_client_id='test', google_client_secret='test', google_admin_emails='alice@berri.ai',
                  session_secret='test-session-secret', memory_review_idle_seconds=0,
                  auto_prepare_repositories=False, session_titles_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    for user in ('alice', 'bob'):
        app.state.store.identity({'method': 'google', 'identity': {'sub': user, 'email': user+'@berri.ai'}})
    yield app
    app.state.store.close()


def turn(app, text='For benchmark reports, I prefer p95 latency and error rate.', *, actor='google:alice',
         status='completed', parent='', mode='modal', repo=''):
    store = app.state.store
    run = store.create_run(text, repo, mode, [], chat_enabled=True, user_id=actor, model=app.state.settings.agent_model)
    if parent:
        store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, run['id']))
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'Task answer; no memory tools were used.', status)
    store.update_run(run['id'], status='idle')
    return run, message['id']


def proposal(request, **changes):
    data = json.loads(json.loads(request.content)['messages'][1]['content'])
    source = data['messages'][-1]
    return {'key': 'benchmark-reports', 'title': 'Benchmark reports',
            'content': 'Use p95 latency and error rate in benchmark reports.', 'kind': 'preference',
            'repository_specific': False, 'source_message_id': source['id'], 'source_quote': source['content'], **changes}


def completion(notes):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'memories': notes})}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130}},
        headers={'x-litellm-call-id': 'test-receipt', 'x-litellm-response-cost': '0.001'})


async def process(app, handler):
    service = app.state.memory_review
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.client = client
        return await service.process_next()


def jobs(app):
    return app.state.store.rows('SELECT * FROM memory_reviews ORDER BY message_id')


async def test_finished_turn_is_captured_without_agent_save_and_recalled_after_restart(review_app):
    app = review_app
    run, message_id = turn(app)
    app.state.store.finish_message(run['id'], message_id, 'Duplicate completion')
    assert len(jobs(app)) == 1
    requests = []
    await process(app, lambda r: requests.append(r) or completion([proposal(r)]))
    assert jobs(app)[0]['status'] == 'completed' and jobs(app)[0]['saved_count'] == 1
    assert await process(app, lambda r: pytest.fail('Completed review replayed')) is False
    request = json.loads(requests[0].content)
    assert request['model'] == app.state.settings.resolve_model()
    assert not request.get('tools') and request['stream'] is False
    note = app.state.memory.listing('google:alice')[0]
    assert note['source']['message_id'] == message_id and note['source']['capture'] == 'background'
    assert app.state.memory.listing('google:bob') == []
    assert 'p95' not in json.dumps(jobs(app))
    ciphertext = app.state.store.rows('SELECT encrypted FROM personal_memories')[0]['encrypted']
    # A short word can occur by chance in random base64 ciphertext. Verify the
    # encryption round trip and absence of the complete plaintext instead.
    plaintext = app.state.security.decrypt(ciphertext)
    assert ciphertext != plaintext and note['content'] not in ciphertext
    assert json.loads(plaintext)['content'] == note['content']
    assert 'p95' not in json.dumps(app.state.store.rows('SELECT * FROM events'))
    billed = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert (billed['message_id'], billed['user_id'], billed['status'], billed['total_tokens']) == (message_id, 'google:alice', 'completed', 130)
    assert billed['cost'] == '0.001'
    app.state.store.close()  # A restart releases the previous process fence.
    restarted = create_app(app.state.settings)
    try:
        new = restarted.state.store.create_run('Prepare benchmark report', '', 'modal', [], chat_enabled=True, user_id='google:alice')
        restarted.state.store.claim_message(new['id'])
        new = restarted.state.store.run(new['id'])
        assert restarted.state.memory.search(new, 'google:alice', 'benchmark')['loaded'] == 1
        assert 'p95' in restarted.state.memory.context(new)
    finally:
        restarted.state.store.close()


async def test_bounded_backfill_merges_corrections_without_duplicate_notes(review_app):
    app = review_app
    app.state.store.memory_review = None
    _, first = turn(app)
    _, second = turn(app, 'Correction: for benchmark reports I prefer p99 latency and error rate.')
    app.state.store.memory_review = app.state.memory_review
    app.state.memory_review.backfill()
    await process(app, lambda r: completion([proposal(r)]))
    first_note = app.state.memory.listing('google:alice')[0]
    await process(app, lambda r: completion([proposal(r, content='Use p99 latency and error rate in benchmark reports.')]))
    notes = app.state.memory.listing('google:alice')
    assert len(notes) == 1 and notes[0]['id'] == first_note['id'] and notes[0]['revision'] == 2
    assert notes[0]['source']['message_id'] == second and 'p99' in notes[0]['content']
    # An older source must not overwrite a newer correction during recovery.
    app.state.store.execute("UPDATE memory_reviews SET status='pending' WHERE message_id=?", (first,))
    await process(app, lambda r: completion([proposal(r)]))
    assert app.state.memory.listing('google:alice')[0]['revision'] == 2


@pytest.mark.parametrize('status', ['failed','cancelled','interrupted','steered','save_failed'])
async def test_unsuccessful_turns_are_not_inputs(review_app, status):
    turn(review_app, status=status)
    review_app.state.memory_review.backfill()
    assert not jobs(review_app)


def test_demo_subagent_and_unverified_requester_are_not_inputs(review_app):
    app = review_app
    parent, _ = turn(app, actor='shared:password:admin')
    turn(app, parent=parent['id'])
    turn(app, mode='demo')
    app.state.memory_review.backfill()
    assert not jobs(app)


async def test_manual_mode_and_pause_revoke_pending_reviews_and_backfill(review_app):
    app = review_app
    turn(app)
    with app.state.store.connect() as conn:
        conn.execute("INSERT INTO memory_preferences VALUES('google:alice',1,0,1)")
        app.state.memory.capture_barrier_in(conn, 'google:alice')
    await process(app, lambda r: pytest.fail('Manual mode sent private input to model'))
    assert jobs(app)[0]['status'] == 'skipped'
    turn(app)
    assert len(jobs(app)) == 1
    app.state.store.execute("UPDATE memory_preferences SET enabled=0,auto_save=1,revision=2")
    turn(app)
    app.state.store.execute("UPDATE memory_preferences SET enabled=1,auto_save=1,revision=3")
    app.state.memory_review.backfill()
    # Old queued work stays revoked by the captured settings revision.
    assert jobs(app)[0]['status'] == 'skipped'


@pytest.mark.parametrize('mutation', ['pause','delete_run','edit_source','change_identity','manual_edit'])
async def test_authorization_and_source_rechecked_after_model_returns(review_app, mutation):
    app = review_app
    run, mid = turn(app)
    def handler(request):
        answer = completion([proposal(request)])
        if mutation == 'pause':
            app.state.store.execute("INSERT INTO memory_preferences VALUES('google:alice',0,1,1)")
        elif mutation == 'delete_run':
            app.state.store.execute("UPDATE runs SET deleted_at='removed' WHERE id=?", (run['id'],))
        elif mutation == 'edit_source':
            app.state.store.execute('UPDATE messages SET content=? WHERE id=?', ('Edited unrelated request', mid))
        elif mutation == 'change_identity':
            app.state.store.execute("UPDATE messages SET user_id='google:bob' WHERE id=?", (mid,))
        else:
            app.state.memory.save('google:alice', Note(key='manual-note', title='Manual preference', content='Prefer a short answer.', request_id='manual-save'))
        return answer
    if mutation == 'edit_source':
        with pytest.raises(ValueError):
            await process(app, handler)
    else:
        await process(app, handler)
    assert all(n['source']['type'] == 'manual' for n in app.state.memory.listing('google:alice'))
    assert not app.state.memory.listing('google:bob')


async def test_original_requester_and_injected_inputs_are_isolated(review_app):
    app = review_app
    run, mid = turn(app)
    # A new active requester does not change who owns the completed input.
    app.state.store.execute("UPDATE runs SET active_user_id='google:bob',active_message_id=? WHERE id=?", (mid+999, run['id']))
    app.state.store.execute("""INSERT INTO messages(run_id,role,content,status,created_at,user_id,steering_parent_id)
        VALUES(?,'user','Keep benchmark tables compact.','completed','2026-10-08','google:alice',?)""", (run['id'],mid))
    app.state.store.execute("""INSERT INTO messages(run_id,role,content,status,created_at,user_id,steering_parent_id)
        VALUES(?,'user','BOB PRIVATE TEXT','completed','2026-10-08','google:bob',?)""", (run['id'],mid))
    def handler(request):
        assert b'BOB PRIVATE TEXT' not in request.content
        assert b'Task answer' not in request.content
        return completion([proposal(request)])
    await process(app, handler)
    assert len(app.state.memory.listing('google:alice')) == 1
    assert not app.state.memory.listing('google:bob')


@pytest.mark.parametrize('invalid', ['quote','secret','scope','duplicate'])
async def test_invalid_proposal_rolls_back_entire_review(review_app, invalid):
    app = review_app
    turn(app)
    def handler(request):
        first = proposal(request)
        second = {**first, 'key': 'second-memory'}
        if invalid == 'quote': second['source_quote'] = 'This quote was never said.'
        if invalid == 'secret': second['content'] = 'password=secret-value'
        if invalid == 'scope': second['repository_specific'] = True
        if invalid == 'duplicate': second['key'] = first['key']
        return completion([first,second])
    with pytest.raises((ValueError, HTTPException)):
        await process(app, handler)
    assert not app.state.memory.listing('google:alice')
    assert jobs(app)[0]['status'] == 'pending'


async def test_forgetting_blocks_old_pending_inputs_even_with_different_key(review_app):
    app = review_app
    turn(app)
    await process(app, lambda r: completion([proposal(r)]))
    turn(app, 'Always include error rate in benchmark reports.')
    note = app.state.memory.listing('google:alice')[0]
    app.state.memory.forget('google:alice', note['id'], note['revision'])
    await process(app, lambda r: pytest.fail('Forgotten memory was re-extracted'))
    app.state.memory_review.backfill()
    assert not app.state.memory.listing('google:alice')
    assert jobs(app)[-1]['status'] == 'skipped'


async def test_gateway_failures_retry_with_a_limit_and_no_private_error_logs(review_app):
    app = review_app
    turn(app)
    for attempt in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            await process(app, lambda r: httpx.Response(429, json={'error': 'PRIVATE ERROR'}))
        assert jobs(app)[0]['attempts'] == attempt+1
        app.state.store.execute("UPDATE memory_reviews SET available_at='' WHERE status='pending'")
    assert jobs(app)[0]['status'] == 'failed'
    assert not await process(app, lambda r: pytest.fail('Unlimited retries'))
    assert 'PRIVATE ERROR' not in json.dumps(app.state.store.rows('SELECT * FROM events'))
    assert len(app.state.store.rows("SELECT * FROM model_requests WHERE status='failed'")) == 3


async def test_active_sessions_defer_and_cancelled_review_can_resume(review_app):
    app = review_app
    run, mid = turn(app)
    app.state.store.execute("UPDATE messages SET status='running' WHERE id=?", (mid,))
    await process(app, lambda r: pytest.fail('In-progress input reviewed'))
    assert jobs(app)[0]['status'] == 'skipped'
    app.state.store.execute("UPDATE messages SET status='completed' WHERE id=?", (mid,))
    app.state.store.execute("UPDATE memory_reviews SET status='pending',available_at='' WHERE message_id=?", (mid,))
    started = asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(process(app, handler))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert jobs(app)[0]['status'] == 'pending'
    assert app.state.store.rows('SELECT status FROM model_requests')[0]['status'] == 'interrupted'


async def test_noop_and_secret_inputs_do_not_create_notes(review_app):
    app = review_app
    turn(app, 'Run the current unit tests.')
    await process(app, lambda r: completion([]))
    assert jobs(app)[0]['status'] == 'completed' and jobs(app)[0]['saved_count'] == 0
    turn(app, 'The api_key=do-not-store-this belongs to my project.')
    await process(app, lambda r: pytest.fail('Secret-containing message sent to extractor'))
    assert not app.state.memory.listing('google:alice')


@pytest.mark.parametrize('finish', ['length','content_filter','tool_calls'])
async def test_partial_or_refused_completion_cannot_save_memory(review_app, finish):
    app = review_app
    turn(app)
    def handler(request):
        data = completion([proposal(request)]).json()
        data['choices'][0]['finish_reason'] = finish
        return httpx.Response(200, json=data)
    with pytest.raises(ValueError): await process(app, handler)
    assert not app.state.memory.listing('google:alice')


async def test_concurrent_agent_save_requires_fresh_deduplication(review_app):
    app = review_app
    _, mid = turn(app)
    def handler(request):
        with app.state.store.connect() as conn:
            app.state.memory.save_in(conn, 'google:alice', Note(key='agent-chosen-key', title='Benchmark reports',
                content='Use p95 latency and error rate.', request_id='agent-save'),
                source={'type':'chat','message_id':mid})
        return completion([proposal(request)])
    with pytest.raises(ValueError, match='library changed'): await process(app, handler)
    assert len(app.state.memory.listing('google:alice')) == 1
    app.state.store.execute("UPDATE memory_reviews SET available_at='' WHERE status='pending'")
    await process(app, lambda r: completion([]))
    assert jobs(app)[0]['status'] == 'completed' and jobs(app)[0]['saved_count'] == 0


async def test_lifecycle_recovers_durable_job_and_stops_worker(review_app):
    app = review_app
    turn(app)
    app.state.store.execute("UPDATE memory_reviews SET status='running',attempts=1")
    service = app.state.memory_review
    service.start()
    # Swap transport before the worker gets its first event-loop turn.
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: completion([proposal(r)])))
    for _ in range(100):
        if jobs(app)[0]['status'] == 'completed': break
        await asyncio.sleep(.01)
    assert jobs(app)[0]['status'] == 'completed'
    client = service.client
    await service.close()
    assert service.worker is None and client.is_closed


@pytest.mark.parametrize('preempted', [False, True])
@pytest.mark.parametrize('close_count', [1, 2])
async def test_shutdown_drains_in_flight_review_cleanup(review_app, monkeypatch, preempted, close_count):
    app = review_app
    turn(app)
    service = app.state.memory_review
    service.slots = ModelSlots(1)
    requested, checkpoint_started = asyncio.Event(), asyncio.Event()
    release_checkpoint, checkpoint_saved = asyncio.Event(), asyncio.Event()

    async def response(request):
        requested.set()
        await asyncio.Future()

    async def flush():
        if jobs(app)[0]['status'] == 'pending':
            checkpoint_started.set()
            await release_checkpoint.wait()
            checkpoint_saved.set()

    async def foreground():
        async with service.slots:
            pass

    monkeypatch.setattr(service.memory.checkpoints, 'flush', flush)
    service.start()
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    closers, foreground_request = [], None
    try:
        await asyncio.wait_for(requested.wait(), 3)
        child, client = service.current, service.client
        if preempted:
            foreground_request = asyncio.create_task(foreground())
            await asyncio.wait_for(checkpoint_started.wait(), 3)
        closers = [asyncio.create_task(service.close()) for _ in range(close_count)]
        await asyncio.wait_for(checkpoint_started.wait(), 3)
        # Shutdown must not interrupt the retry checkpoint if a foreground
        # request already cancelled this review and its cleanup is still running.
        await asyncio.wait(closers, timeout=.05)
        assert not any(closer.done() for closer in closers) and not client.is_closed
        assert child.cancelling() == 1
    finally:
        release_checkpoint.set()
        await asyncio.wait_for(asyncio.gather(*closers) if closers else service.close(), 3)
        if foreground_request:
            await asyncio.wait_for(foreground_request, 3)
    assert checkpoint_saved.is_set() and child.done()
    assert client.is_closed and service.worker is None and service.current is None
    assert jobs(app)[0]['status'] == 'pending'
    assert not app.state.memory.listing('google:alice')
    assert app.state.store.rows('SELECT status FROM model_requests')[0]['status'] == 'interrupted'
    async with asyncio.timeout(3):
        async with service.slots:
            pass


async def test_ultrafast_memory_review_uses_the_base_gateway_model(review_app):
    app = review_app
    run, message_id = turn(app)
    app.state.store.execute('UPDATE messages SET model=? WHERE id=?', (ASTRA_ULTRAFAST, message_id))
    requests = []
    await process(app, lambda request: requests.append(request) or completion([]))
    assert jobs(app)[0]['status'] == 'completed'
    body = json.loads(requests[0].content)
    assert body['model'] == ASTRA and 'service_tier' not in body
