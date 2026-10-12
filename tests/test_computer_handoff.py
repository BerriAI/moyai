from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app.computer_handoff import request_handoff
from test_spend import active, sign_in
from test_workspace import workspace


def handoff(client, run, **arguments):
    return client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
                       json={'name': 'computer_handoff', 'arguments': arguments or {'reason': 'Sign in with SSO.'}})


def test_broker_handoff_is_durable_scoped_and_deduplicated(workspace):
    app, client = workspace
    sign_in(app, client)
    run = active(app)
    tools = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'}).json()
    assert 'computer_handoff' in {tool['name'] for tool in tools}
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: handoff(client, run), range(3)))
    assert all(response.status_code == 200 for response in results)
    assert sum(not response.json()['replayed'] for response in results) == 1
    events = [event for event in app.state.store.events(run['id']) if event['kind'] == 'computer']
    assert len(events) == 1
    assert events[0]['data'] == {'phase': 'handoff', 'version': 1, 'turn_id': run['active_message_id'],
                                 'user_id': 'google:alice', 'reason': 'Sign in with SSO.'}
    app.state.store.finish_message(run['id'], run['active_message_id'], 'Please sign in through Computer.')
    app.state.store.update_run(run['id'], status='idle')
    snapshot = client.get(f"/api/runs/{run['id']}?activity=summary").json()
    assert str(run['active_message_id']) in snapshot['deferred_activity']
    assert events[0] in snapshot['events'], 'Deferred successful history must preserve the actionable handoff'
    other = active(app)
    assert not any(event['kind'] == 'computer' for event in app.state.store.events(other['id']))


@pytest.mark.parametrize('arguments', [{'reason': ''}, {'reason': ' '}, {'reason': 'x' * 501},
                                      {'reason': 'Sign in.', 'run_id': 'someone-else'},
                                      {'reason': 'Sign in.', 'user_id': 'google:bob'}])
def test_handoff_rejects_invalid_or_forged_targets(workspace, arguments):
    app, client = workspace
    run = active(app)
    assert handoff(client, run, **arguments).status_code == 422
    assert not any(event['kind'] == 'computer' for event in app.state.store.events(run['id']))


@pytest.mark.parametrize('changes', [{'active_user_id': 'google:bob'}, {'active_message_id': 999},
                                   {'status': 'stopping'}, {'token_hash': 'rotated'}])
def test_handoff_rechecks_turn_and_capability_before_recording(workspace, changes):
    app, _ = workspace
    run = active(app)
    field, value = next(iter(changes.items()))
    app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, run['id']))
    with pytest.raises(HTTPException) as error:
        request_handoff(app.state.store, run, {'reason': 'Sign in.'})
    assert error.value.status_code == 409
    assert not any(event['kind'] == 'computer' for event in app.state.store.events(run['id']))


def test_expired_capability_cannot_request_handoff(workspace):
    app, client = workspace
    run = active(app)
    app.state.store.update_run(run['id'], status='idle')
    assert handoff(client, run).status_code == 401
