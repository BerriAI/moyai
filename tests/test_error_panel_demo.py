"""Local seeded demo uses real session APIs without starting external work."""
from fastapi.testclient import TestClient

from scripts.error_panel_demo import demo


def test_error_panel_demo_keeps_terminal_and_legacy_histories_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv('MOYAI_DATABASE_URL', 'postgresql://must-not-connect.invalid/production')
    monkeypatch.setenv('LITELLM_API_KEY', 'must-not-use-production-key')
    monkeypatch.setenv('TEMPORAL_ENABLED', 'true')
    app = demo(tmp_path)
    try:
        assert not app.state.store.database
        with TestClient(app, base_url='http://127.0.0.1:8838') as client:
            for case, run_id in app.state.demo_runs.items():
                response = client.get('/demo/login', params={'case': case}, follow_redirects=False)
                assert response.status_code == 307
                assert response.headers['location'] == '/#run=' + run_id
                response = client.get(f'/api/runs/{run_id}')
                assert response.status_code == 200
                run = response.json()
                assert run['status'] == 'failed'
                answer = next(message for message in run['messages'] if message['role'] == 'assistant')
                terminal = [event for event in run['events'] if event['data'].get('phase') == 'sdk_failure']
                assert len(terminal) == (1 if case == 'terminal' else 0)
                if terminal:
                    assert terminal[0]['data']['turn_id'] == answer['response_to_id']
                    assert terminal[0]['data']['exception_type'] == 'BrokerRejected'
                    assert terminal[0]['data']['activity_type'] == 'deepagents.invoke_tool'
                    assert terminal[0]['data']['http_status'] == 500
                    assert 'recovered-model-request' not in str(terminal)
                assert len([event for event in run['events'] if event['data'].get('phase') == 'model_response']) == 9
                assert 'must-not-use-production-key' not in str(run)
            assert client.post('/api/runs', json={'prompt': 'Do not execute'}).status_code == 403
            assert client.get('/broker/fixture/tools').status_code == 403
            assert client.get('/api/auth/google/start').status_code == 403
    finally:
        app.state.store.close()
