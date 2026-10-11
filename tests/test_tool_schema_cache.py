from pydantic import BaseModel

from app.tool_schema_cache import tool_schema, _schema
from test_workspace import workspace, cloud_capability  # noqa: F401


def test_cached_schemas_are_reused_without_sharing_mutable_results(monkeypatch):
    class Input(BaseModel):
        name: str

    calls = []
    generate = Input.model_json_schema
    monkeypatch.setattr(Input, 'model_json_schema', lambda: (calls.append(True), generate())[1])
    first = tool_schema(Input)
    first['properties']['name']['type'] = 'integer'
    assert tool_schema(Input)['properties']['name']['type'] == 'string'
    assert calls == [True]
    _schema.cache_clear()


def test_catalog_cache_never_caches_authorization(workspace):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['github'])
    endpoint = f'/broker/{run_id}/tools'
    first = client.get(endpoint, headers=headers)
    assert first.status_code == 200 and 'github_repositories' in first.text
    before = _schema.cache_info().hits
    assert client.get(endpoint, headers=headers).json() == first.json()
    assert _schema.cache_info().hits > before
    response = client.patch('/api/connections/github/policy', json={'enabled': False, 'read_only': False})
    assert response.status_code == 200
    assert 'github_repositories' not in client.get(endpoint, headers=headers).text
    other, other_headers = cloud_capability(app, [])
    assert 'github_repositories' not in client.get(f'/broker/{other}/tools', headers=other_headers).text
    app.state.store.update_run(run_id, token_hash='')
    assert client.get(endpoint, headers=headers).status_code == 401
