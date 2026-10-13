import copy
import asyncio
import hashlib

import pytest

from app.github import GitHub, Publish
from app.connector_errors import ConnectorError
from test_github import GitHubAPI, PAYLOAD, connected
from test_workspace import workspace


@pytest.mark.parametrize('value', [None, {}, [], 'bad', 1, {'head': None}])
def test_malformed_creation_ack_is_reconciled(workspace, monkeypatch, value):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    async def request(method, path, **kwargs):
        result = await api.request(method, path, **kwargs)
        return value if method == 'POST' and path.endswith('/pulls') else result
    monkeypatch.setattr(github, 'request', request)
    result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                        json={'name':'github_create_pull_request','arguments':PAYLOAD}).json()
    assert result['number'] == 100
    assert [m for m,p,k in api.calls if p.endswith('/pulls')] == ['GET','POST','GET']


@pytest.mark.parametrize('field,value', [('number',True),('number',-1),('number','100'),('html_url','https://other.example/100'),('draft',None),('state','bad'),('head',None),('base',[])])
def test_invalid_receipt_never_accepted_on_any_recovery(workspace, monkeypatch, field, value):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = 'pr'
    async def request(method, path, **kwargs):
        if method == 'GET' and path.endswith('/pulls') and api.pr:
            pr = copy.deepcopy(api.pr); pr[field] = value
            return [pr]
        return await api.request(method,path,**kwargs)
    monkeypatch.setattr(github, 'request', request)
    for _ in range(2):
        result = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                            json={'name':'github_create_pull_request','arguments':PAYLOAD}).json()
        assert result['outcome_uncertain'] and 'branch moyai/' in result['error']
    assert not app.state.store.rows('SELECT result FROM github_publications')[0]['result']
    assert len([c for c in api.calls if c[0]=='POST' and c[1].endswith('/pulls')]) == 1


def test_attempt_marker_survives_service_recreation_and_empty_readback(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch); api.lose = 'pr'
    async def request(method, path, **kwargs):
        if method == 'GET' and path.endswith('/pulls'): return []
        return await api.request(method,path,**kwargs)
    monkeypatch.setattr(github,'request',request)
    body = {'name':'github_create_pull_request','arguments':PAYLOAD}
    assert client.post(f'/broker/{run_id}/tools/call',headers=headers,json=body).json()['outcome_uncertain']
    fresh = GitHub(app.state.store,app.state.security,app.state.settings,app.state.connectors)
    monkeypatch.setattr(fresh,'request',request)
    monkeypatch.setattr(fresh,'installation_token',github.installation_token)
    monkeypatch.setattr(app.state.connectors,'github',fresh)
    assert client.post(f'/broker/{run_id}/tools/call',headers=headers,json=body).json()['outcome_uncertain']
    assert len([c for c in api.calls if c[0]=='POST' and c[1].endswith('/pulls')]) == 1


def test_legacy_incomplete_publication_migration_is_conservative(workspace):
    app, _ = workspace
    store = app.state.store
    store.execute('ALTER TABLE github_publications DROP COLUMN attempted')
    for identity, commit, result in [('unsent','',''),('uncertain','a'*40,''),('done','a'*40,'{}')]:
        store.execute('INSERT INTO github_publications VALUES(?,?,1,?,?,?,?,?,?)',(identity,'run','hash','branch',commit,result,'connection','now'))
    GitHub(store, app.state.security, app.state.settings, app.state.connectors)
    assert {r['id']:r['attempted'] for r in store.rows('SELECT * FROM github_publications')} == {'unsent':0,'uncertain':1,'done':0}


@pytest.mark.parametrize('commit', ['', 'd' * 40])
def test_legacy_unfinished_row_cannot_invent_frozen_base(workspace, monkeypatch, commit):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    args = Publish.model_validate(PAYLOAD)
    identity = hashlib.sha256(f'{run_id}:{args.request_key}'.encode()).hexdigest()
    app.state.store.execute('''INSERT INTO github_publications
        (id,run_id,message_id,arguments_hash,branch,commit_sha,connection_version,created_at)
        VALUES(?,?,0,?,?,?,?,?)''', (identity, run_id, github.arguments_hash(args, 101),
        f'moyai/{run_id[:12]}/{identity[:16]}', commit, github.connection_version(), 'before-upgrade'))
    with pytest.raises(ConnectorError, match='frozen repository and base'):
        asyncio.run(github.publish(app.state.store.run(run_id), args))
    assert all(method == 'GET' for method, _, _ in api.calls)
    assert not app.state.store.rows('SELECT * FROM github_publication_intents')
