import httpx
import pytest
from test_workspace import workspace, cloud_capability

@pytest.mark.parametrize('status,expected', [(403, 'permission'), (429, '17 seconds'), (503, 'retry')])
def test_read_failure_has_read_recovery_without_replay(workspace, monkeypatch, status, expected):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    calls = []
    async def request(self, method, url, **kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            return httpx.Response(200, json={'data': {'issue': {'identifier': 'SIM-123'}}})
        return httpx.Response(status, headers={'Retry-After':'17'}, request=httpx.Request(method,url),
                              json={'private':'must not be shown'})
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
        json={'name':'linear_issue','arguments':{'issue_id':'SIM-123'}})
    result = response.json()
    assert response.status_code == 200
    assert result['outcome_uncertain'] is False
    assert 'retry this read' in result['instruction'].lower()
    assert 'verify the destination' not in result['instruction'].lower()
    assert expected in (result['error']+' '+result['instruction']).lower()
    assert 'must not be shown' not in str(result)
    assert len(calls) == 1
    assert app.state.store.run(run_id)['status'] == 'running'
    recovered = client.post(f'/broker/{run_id}/tools/call', headers=headers,
        json={'name':'linear_issue','arguments':{'issue_id':'SIM-123'}})
    assert recovered.json()['issue']['identifier'] == 'SIM-123'
    assert len(calls) == 2
    assert all('mutation' not in call['json']['query'] for call in calls)
