from test_workspace import workspace  # noqa: F401
from test_spend import sign_in


def test_capacity_requires_admin_and_reports_real_gate_state(workspace):
    app, client = workspace
    sign_in(app, client)
    app.state.model_slots.record_queue()
    result = client.get('/api/admin/capacity')
    assert result.status_code == 200
    body = result.json()
    assert body['model']['capacity'] == app.state.settings.max_concurrent_model_requests
    assert body['model']['queue_responses'] == 1
    assert body['model']['active'] == body['model']['waiting'] == 0
    assert body['sandbox']['occupied'] is None  # Legacy non-Temporal runner.
    assert 'session_secret' not in result.text and 'prompt' not in result.text
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/capacity').status_code == 403
    client.cookies.clear()
    assert client.get('/api/admin/capacity').status_code == 401
