"""Dispatch is an observed HTTP attempt; admission and computer readiness differ."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import performance
from app.security import digest
from test_performance import CURRENT, sample, stamp, store, workspace  # noqa: F401
from test_spend import sign_in


def test_dispatch_preserves_admission_and_pairs_earliest_attempt(store):
    run, message = sample(store, -60, picked_up_at=stamp(-58), first_model_at=stamp(-55))
    run = {**run, 'active_message_id': message['id']}
    performance.model_dispatched(store, run, stamp(-57), stamp(-50), 'first')
    performance.model_dispatched(store, run, stamp(-45), stamp(-40), 'retry')
    report = performance.report(store, current=CURRENT)
    assert report['metrics']['first_model']['average_ms'] == 5000
    assert report['metrics']['model_dispatch']['average_ms'] == 10000
    assert [report['metrics'][k]['average_ms'] for k in
            ('dispatch_queue', 'worker_to_broker', 'broker_prepare')] == [2000, 1000, 7000]
    assert report['recent'][0]['model_request_id'] == 'first'
    # Out-of-order persistence must select the earliest observation atomically,
    # together with that attempt's own ingress and request identity.
    performance.model_dispatched(store, run, stamp(-57), stamp(-52), 'earlier')
    row = performance.report(store, current=CURRENT)['recent'][0]
    assert row['dispatch_ms'] == 8000 and row['broker_prepare_ms'] == 5000
    assert row['model_request_id'] == 'earlier'


def test_legacy_admission_is_never_invented_dispatch_and_cohorts_match(store):
    run, message = sample(store, -60, picked_up_at=stamp(-59), first_model_at=stamp(-58))
    store.execute('UPDATE performance_samples SET timing_version=NULL WHERE message_id=?', (message['id'],))
    new, msg = sample(store, -20, status='failed', picked_up_at=stamp(-19))
    performance.model_dispatched(store, {**new, 'active_message_id': msg['id']},
                                 stamp(-18), stamp(-16), 'attempt')
    sample(store, -10, status='failed')
    report = performance.report(store, current=CURRENT)
    assert report['dispatch_coverage'] == {'eligible': 2, 'legacy': 1, 'complete_breakdowns': 1}
    assert report['metrics']['model_dispatch']['count'] == 1
    assert report['metrics']['model_dispatch']['incomplete'] == 1
    assert all(report['metrics'][key]['count'] == 1 for key in
               ('dispatch_queue', 'worker_to_broker', 'broker_prepare'))
    assert report['metrics']['computer_ready']['total'] == 0


def test_late_dispatch_remains_on_original_turn_and_wrong_run_is_ignored(store):
    run, msg = sample(store)
    other, _ = sample(store)
    performance.model_dispatched(store, {**other, 'active_message_id': msg['id']},
                                 stamp(-50), stamp(-40), 'wrong-run')
    assert store.rows('SELECT dispatch_at FROM performance_samples WHERE message_id=?', (msg['id'],))[0]['dispatch_at'] is None
    store.execute('UPDATE runs SET active_message_id=? WHERE id=?', (msg['id'] + 100, run['id']))
    performance.model_dispatched(store, {**run, 'active_message_id': msg['id']},
                                 stamp(-50), stamp(-40), 'original-turn')
    assert store.rows('SELECT dispatch_request_id FROM performance_samples WHERE message_id=?',
                      (msg['id'],))[0]['dispatch_request_id'] == 'original-turn'


@pytest.mark.parametrize(('snapshot', 'state', 'kind'), [
    ('', {}, 'cold_start'), ('im-saved', {}, 'snapshot_restore'),
    ('im-saved', {'computer': {'phase': 'ready'}}, 'reuse'),
])
def test_computer_timing_is_first_requested_acquisition_only(store, snapshot, state, kind):
    run, message = sample(store)
    run = {**run, 'active_message_id': message['id'], 'snapshot_id': snapshot}
    with store.connect() as conn:
        conn.begin_write()
        performance.computer_requested_in(conn, run, state, stamp(-30))
        performance.computer_requested_in(conn, run, {'computer': {'phase': 'ready'}}, stamp(-10))
    performance.computer_ready(store, run['id'], message['id'], stamp(-20))
    performance.computer_ready(store, run['id'], message['id'], stamp(-5))
    report = performance.report(store, current=CURRENT)
    assert report['metrics']['computer_ready']['average_ms'] == 10000
    assert report['computers'][kind]['count'] == 1
    assert report['recent'][0]['computer_start_kind'] == kind


def test_clock_inversion_is_not_a_zero_or_additive_breakdown(store):
    run, message = sample(store, -60, picked_up_at=stamp(-55))
    performance.model_dispatched(store, {**run, 'active_message_id': message['id']},
                                 stamp(-59), stamp(-50), 'clock-skew')
    report = performance.report(store, current=CURRENT)
    assert report['metrics']['model_dispatch']['count'] == 1
    assert report['dispatch_coverage']['complete_breakdowns'] == 0
    assert report['recent'][0]['worker_to_broker_ms'] is None


@pytest.mark.parametrize('transport_failure', [False, True])
def test_real_broker_marks_dispatch_at_send_including_failed_attempts(workspace, monkeypatch, transport_failure):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client)
    monkeypatch.setattr('app.main.now', lambda: stamp(-10))
    run = client.post('/api/runs', json={'prompt': 'Safe dispatch timing test'}).json()
    monkeypatch.setattr('app.db.now', lambda: stamp(-8))
    app.state.store.claim_message(run['id'])
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run['id'],))
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    app.state.settings.litellm_api_key = 'local-test'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    monkeypatch.setattr('app.spend.now', lambda: stamp(-3))
    monkeypatch.setattr('app.model_request.datetime',
                        SimpleNamespace(now=lambda tz: CURRENT + timedelta(seconds=-1)))
    monkeypatch.setattr('app.broker_diagnostics.datetime',
                        SimpleNamespace(now=lambda tz: CURRENT + timedelta(seconds=-5)))
    sent = []

    def gateway(request):
        sent.append(request)
        if transport_failure:
            raise httpx.ConnectError('synthetic failure', request=request)
        return httpx.Response(200, json={'choices': [
            {'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})

    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient',
                        lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions",
                           headers={'Authorization': 'Bearer capability'}, json={'messages': []})
    assert response.status_code == (502 if transport_failure else 200)
    report = performance.report(app.state.store, current=CURRENT)
    assert len(sent) == 1
    assert report['metrics']['first_model']['average_ms'] == 7000
    assert report['metrics']['model_dispatch']['average_ms'] == 9000
    assert report['metrics']['broker_prepare']['average_ms'] == 4000
    assert report['recent'][0]['model_request_id'] == sent[0].headers['x-litellm-call-id']


@pytest.mark.asyncio
async def test_background_admission_and_observation_failure_do_not_change_model_result(store, monkeypatch):
    from app.model_request import ModelRequest
    run, message = sample(store)
    run = {**run, 'active_message_id': message['id']}
    request = SimpleNamespace(scope={'moyai_broker_received_at': stamp(-50)})
    accounting = ModelRequest(settings=None, store=store, spend=None, tracing=None,
                              checkpoints=None, run=run, model='test', started=1)
    accounting.dispatched(request)
    assert accounting.dispatch_observation is None
    accounting.startup = True

    def unavailable(*args):
        raise RuntimeError('synthetic timing failure')

    monkeypatch.setattr(performance, 'model_dispatched', unavailable)
    accounting.dispatched(request)
    await accounting.dispatch_observation
    assert performance.report(store, current=CURRENT)['metrics']['model_dispatch']['count'] == 0


@pytest.mark.parametrize('rejection', ['authentication', 'invalid_body'])
def test_rejection_before_http_send_does_not_record_dispatch(workspace, monkeypatch, rejection):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client)
    run = client.post('/api/runs', json={'prompt': 'Safe rejection timing test'}).json()
    app.state.store.claim_message(run['id'])
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run['id'],))
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    token = 'wrong' if rejection == 'authentication' else 'capability'
    body = {'messages': [] if rejection == 'authentication' else 'invalid'}
    response = client.post(f"/broker/{run['id']}/v1/chat/completions",
                           headers={'Authorization': 'Bearer ' + token}, json=body)
    assert response.status_code in {401, 403, 422}
    assert app.state.store.rows('SELECT dispatch_at FROM performance_samples') == [{'dispatch_at': None}]


@pytest.mark.asyncio
@pytest.mark.parametrize('needs_repair', [False, True])
async def test_computer_readiness_requires_usable_project(store, monkeypatch, needs_repair):
    from app.independent_runtime import IndependentRuntime
    run, message = sample(store)
    run = {**run, 'active_message_id': message['id']}
    state = {'message_id': message['id'], 'computer': {'phase': 'ready',
             'project_health': {'status': 'needs_repair' if needs_repair else 'ready'}}}
    with store.connect() as conn:
        conn.begin_write()
        performance.computer_requested_in(conn, run, state, stamp(-30))

    async def sandbox(computer):
        return object()

    runtime = IndependentRuntime(SimpleNamespace(store=store, submit_in=lambda *args: None, sandbox=sandbox))
    monkeypatch.setattr('app.db.now', lambda: stamp(-20))
    # A damaged project remains accessible to repair tools, but is not measured
    # as a ready project.
    assert await runtime.ensure_computer(run['id'], state)
    report = performance.report(store, current=CURRENT)
    assert report['metrics']['computer_ready']['count'] == (0 if needs_repair else 1)
