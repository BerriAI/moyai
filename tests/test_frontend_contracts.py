"""The generated frontend client trusts these response models; keep them honest."""
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.frontend_contracts import WorkspaceSession


def client(tmp_path, **settings):
    settings = Settings(_env_file=None, data_dir=tmp_path / 'data', public_url='http://127.0.0.1:8787', **settings)
    return TestClient(main.create_app(settings), base_url=settings.public_url, client=('127.0.0.1', 50000))


def test_session_matches_its_contract_signed_out_and_in(tmp_path):
    with client(tmp_path, workspace_password='test-workspace-password') as http:
        http.headers['Origin'] = 'http://127.0.0.1:8787'
        signed_out = WorkspaceSession.model_validate(http.get('/api/session').json())
        assert not signed_out.authenticated and signed_out.password_enabled
        assert http.post('/api/login', json={'password': 'test-workspace-password'}).status_code == 200
        signed_in = WorkspaceSession.model_validate(http.get('/api/session').json())
        assert signed_in.authenticated and signed_in.role == 'admin' and signed_in.csrf


def test_local_preview_session_still_sets_its_cookie(tmp_path):
    with client(tmp_path) as http:
        response = http.get('/api/session')
        assert WorkspaceSession.model_validate(response.json()).authenticated
        assert 'workspace_session' in response.cookies


def test_spa_routes_serve_the_built_entry_document(tmp_path, monkeypatch):
    index = tmp_path / 'spa/index.html'
    index.parent.mkdir()
    index.write_text('<script type="module" src="/static/spa/assets/index-one.js"></script>')
    monkeypatch.setattr(main, 'FRONTEND_INDEX', index)
    with client(tmp_path) as http:
        first = http.get('/settings/memory')
        assert 'index-one.js' in first.text and first.headers['cache-control'] == 'no-cache'
        index.write_text('<script type="module" src="/static/spa/assets/index-two.js"></script>')
        assert 'index-two.js' in http.get('/sessions/new').text == http.get('/').text
        assert http.get('/api/not-a-route').status_code == 404
