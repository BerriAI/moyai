"""Exercise real broker/connector code with an in-memory Linear API.

Run: uv run python scripts/linear_parenting_demo.py [--pause 1.5]
No provider credentials are loaded and no request leaves this process.
"""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import digest


def main(pause=0):
    issues = {
        f'DEMO-{n}': {'id': f'abcdefab-1234-1234-1234-{n:012d}',
                     'identifier': f'DEMO-{n}', 'title': f'Existing issue {n}', 'parent': None}
        for n in [100, 1, 2, 3, 4, 5]
    }
    original = {key: issue['id'] for key, issue in issues.items()}
    writes = []

    def linear_api(request):
        assert str(request.url) == 'https://api.linear.app/graphql'
        body = json.loads(request.content)
        query, variables = body['query'], body['variables']
        issue = next(i for i in issues.values() if variables['id'] in (i['id'], i['identifier']))
        if 'issueUpdate(' in query:
            writes.append(body)
            parent = next(i for i in issues.values() if i['id'] == variables['input']['parentId'])
            issue['parent'] = {key: parent[key] for key in ('id', 'identifier', 'title')}
            data = {'issueUpdate': {'success': True, 'issue': issue}}
        else:
            assert 'issue(id:' in query
            data = {'issue': issue}
        return httpx.Response(200, json={'data': data})

    http_client = httpx.AsyncClient

    def local_client(*args, **kwargs):
        return http_client(*args, **kwargs, transport=httpx.MockTransport(linear_api))

    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    with TemporaryDirectory(prefix='moyai-linear-demo-') as directory:
        values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8792')
        app = create_app(Settings(_env_file=None, **values))
        with TestClient(app, base_url=values['public_url'], client=('127.0.0.1', 50000)) as client:
            session = client.get('/api/session').json()
            client.headers.update({'Origin': values['public_url'], 'X-CSRF-Token': session['csrf']})
            app.state.connectors.save('linear', {'access_token': 'demo-token', 'kind': 'personal'}, 'Simulated Linear')
            run = app.state.store.create_run('Reparent five existing demo issues', '', 'modal', ['linear'])
            run_id = run['id']
            app.state.store.update_run(run_id, status='running', token_hash=digest('demo-capability'))
            headers = {'Authorization': 'Bearer demo-capability'}
            endpoint = f'/broker/{run_id}/tools/call'
            print('MOYAI DEVIN / LINEAR PARENT UPDATES', flush=True)
            print('LOCAL DEMO: real broker + connector; no approval clicks; simulated Linear API', flush=True)
            print('Before: 5 existing children, no parents. 6 total issues including DEMO-100.\n', flush=True)
            with patch('app.connectors.httpx.AsyncClient', local_client):
                tools = client.get(f'/broker/{run_id}/tools', headers=headers).json()
                assert any(t['name'] == 'linear_update_issue' for t in tools)
                for number in range(1, 6):
                    args = {'issue_id': f'DEMO-{number}', 'parent_id': 'DEMO-100'}
                    print('POST linear_update_issue ' + json.dumps(args), flush=True)
                    time.sleep(pause)
                    response = client.post(endpoint, headers=headers,
                                           json={'name': 'linear_update_issue', 'arguments': args})
                    assert response.status_code == 200
                    updated = response.json()['issueUpdate']['issue']
                    assert not app.state.store.approvals(run_id)
                    assert app.state.store.run(run_id)['status'] == 'running'
                    read = client.post(endpoint, headers=headers,
                                       json={'name': 'linear_issue', 'arguments': {'issue_id': args['issue_id']}}).json()['issue']
                    assert read['id'] == original[args['issue_id']] == updated['id']
                    assert read['parent']['identifier'] == 'DEMO-100'
                    print(f"  EXECUTED / READ BACK: {read['identifier']} -> DEMO-100; same issue ID\n", flush=True)
                    time.sleep(pause)
            assert original == {key: issue['id'] for key, issue in issues.items()}
            assert len(writes) == 5 and all('issueUpdate(' in w['query'] for w in writes)
            print('PASS: 5 parent updates / 0 new issues / all 5 original IDs preserved', flush=True)
            print('PASS: 0 approvals / every update executed directly and was verified with linear_issue', flush=True)
            print('The live Linear workspace was not contacted.', flush=True)
            app.state.store.update_run(run_id, status='completed', token_hash='')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause', type=float, default=0, help='Seconds between steps for a readable terminal recording.')
    main(parser.parse_args().pause)
