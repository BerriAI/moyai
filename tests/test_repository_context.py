"""Real broker/context paths; GitHub transport is the only synthetic service."""
import asyncio
import base64
import hashlib
import json
import threading
import time

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.db import database
from app.repository_context import excerpt
from test_github import GitHubAPI, select, OWNER_ID, CURRENT
from test_spend import active, sign_in
from test_workspace import workspace


class ContextAPI(GitHubAPI):
    def __init__(self, github, monkeypatch):
        super().__init__(github, monkeypatch, 'BerriAI/moyai')
        self.commit = CURRENT
        self.contents = {
            'README.md': '# Example repository\n\nAuthentication uses app/auth.py.\nRun pytest tests/test_auth.py to verify login.\n',
            'AGENTS.md': '# Repository conventions\nCheck authentication behavior before opening a PR.\n',
            'docs/architecture.md': '# Architecture\nThe authentication service validates signed sessions.\n',
            'app/auth.py': 'def authenticate(token):\n    return verify_session(token)\n',
            'tests/test_auth.py': 'def test_authentication():\n    assert authenticate("fixture")\n',
            'assets/binary.dat': '\0binary',
        }
        self.blob_calls = []
        self.truncated = False
        self.before_blob = None

    def entries(self):
        return [{'path': path, 'mode': '100644', 'type': 'blob',
                 'sha': hashlib.sha1(content.encode()).hexdigest(), 'size': len(content.encode())}
                for path, content in self.contents.items()]

    async def request(self, method, path, **kwargs):
        if '/git/ref/heads/' in path:
            return {'object': {'sha': self.commit}}
        if '/git/trees/' in path:
            return {'tree': [*self.entries(), {'path': 'linked.md', 'mode': '120000', 'type': 'blob',
                'sha': 'e' * 40, 'size': 5}], 'truncated': self.truncated}
        if '/git/blobs/' in path:
            self.blob_calls.append(path)
            if self.before_blob:
                await self.before_blob()
            entry = next(entry for entry in self.entries() if path.endswith(entry['sha']))
            return {'sha': entry['sha'], 'encoding': 'base64',
                'content': base64.b64encode(self.contents[entry['path']].encode()).decode()}
        return await super().request(method, path, **kwargs)


@pytest.fixture
def repository(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    github = app.state.connectors.github
    github.save_app({'id': 123, 'slug': 'moyai-test', 'pem': 'private-signing-key', 'owner_id': OWNER_ID})
    select(app, (202,))
    app.state.settings.repository_context_enabled = True
    run = active(app)
    app.state.store.execute('UPDATE runs SET plugins=?,github_repository_id=?,repo_url=? WHERE id=?',
        (json.dumps(['github']), 202, 'https://github.com/BerriAI/moyai', run['id']))
    run = app.state.store.run(run['id'])
    api = ContextAPI(github, monkeypatch)
    return app, client, run, api


def refresh(app):
    index = app.state.repository_context
    asyncio.run(index.refresh(index.github.connection_version(), 202))


def invoke(app, run, name='repository_context_search', **args):
    return asyncio.run(app.state.repository_context.call(run, name, args))


def test_background_index_and_cached_guide_need_no_model_or_sandbox(repository, monkeypatch):
    app, _, run, api = repository
    index = app.state.repository_context
    assert index.context(run) == ''
    assert not api.calls and not api.blob_calls
    refresh(app)
    assert len(api.blob_calls) == 3  # Only orientation documents, not source bodies.
    async def offline(*args, **kwargs):
        raise AssertionError('The cached path must not contact GitHub')
    monkeypatch.setattr(index.github, 'request', offline)
    context = index.context(run)
    assert 'Authentication uses app/auth.py' in context and CURRENT in context
    assert 'return verify_session' not in context
    assert len(context.encode()) < 8000
    snapshot = app.state.store.rows('SELECT guide,catalog,documents FROM repository_context_snapshots')[0]
    assert 'Authentication' not in json.dumps(snapshot)
    matches = invoke(app, run, query='auth')['matches']
    assert matches[0]['path'] in {'app/auth.py', 'tests/test_auth.py'}
    assert all(CURRENT in item['url'] for item in matches)
    assert app.state.store.rows('SELECT tainted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['tainted'] & 1


@pytest.mark.parametrize('route', ['chat/completions', 'messages', 'responses'])
def test_guide_reaches_all_harness_protocols_and_sources_load_only_when_read(repository, monkeypatch, route):
    app, client, run, api = repository
    refresh(app)
    captured = []
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    actual = httpx.AsyncClient
    def model(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'response', 'choices': [], 'usage': {'total_tokens': 10}})
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(model), **kw))
    headers = {'Authorization': 'Bearer capability'}
    result = client.post(f"/broker/{run['id']}/v1/{route}", headers=headers,
        json={'messages': [], 'input': [], 'stream': False})
    assert result.status_code == 200, result.text
    assert 'Authentication uses app/auth.py' in json.dumps(captured[-1])
    assert len(api.blob_calls) == 3
    result = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
        json={'name': 'repository_context_read', 'arguments': {'commit_sha': CURRENT, 'path': 'app/auth.py', 'max_lines': 1}})
    assert result.status_code == 200 and result.json()['content'] == 'def authenticate(token):'
    assert result.json()['next_line'] == 2 and len(api.blob_calls) == 4
    public = client.get('/api/runs/' + run['id']).text + json.dumps(app.state.store.events(run['id']))
    assert 'Authentication uses' not in public and 'private-signing-key' not in public


@pytest.mark.parametrize('change', ['disabled', 'connection', 'repository', 'requester', 'turn', 'plugins', 'stopped', 'feature'])
def test_current_authority_always_wins_over_cached_context(repository, change):
    app, _, run, _ = repository
    refresh(app)
    assert app.state.repository_context.context(run)
    if change == 'disabled':
        app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0) ON CONFLICT(provider) DO UPDATE SET enabled=0")
    elif change == 'connection':
        app.state.connectors.github.save_app({'id': 999, 'pem': 'rotated-signing-key'})
    elif change == 'repository':
        select(app, (101,))
    elif change == 'feature':
        app.state.settings.repository_context_enabled = False
    else:
        column, value = {'requester': ('active_user_id', 'google:bob'), 'turn': ('active_message_id', 9999),
            'plugins': ('plugins', '[]'), 'stopped': ('status', 'cancelled')}[change]
        app.state.store.execute(f'UPDATE runs SET {column}=? WHERE id=?', (value, run['id']))
    assert app.state.repository_context.context(run) == ''
    try:
        result = invoke(app, run, query='auth')
        assert result['status'] == 'unavailable'
    except ConnectorError:
        pass


def test_refresh_pins_revision_for_the_turn_and_preserves_old_sources(repository):
    app, _, run, api = repository
    refresh(app)
    index = app.state.repository_context
    first = index.context(run)
    api.commit = 'f' * 40
    api.contents['README.md'] = '# Changed repository\nNew authentication design.\n'
    refresh(app)
    assert index.context(run) == first
    assert invoke(app, run, query='authentication')['commit_sha'] == CURRENT
    old = invoke(app, run, 'repository_context_read', commit_sha=CURRENT, path='README.md')
    assert 'Authentication uses app/auth.py' in old['content']
    app.state.store.execute('UPDATE runs SET active_message_id=? WHERE id=?', (9999, run['id']))
    updated = app.state.store.run(run['id'])
    assert 'Changed repository' in index.context(updated)
    assert invoke(app, updated, query='authentication')['commit_sha'] == api.commit


def test_revocation_during_source_read_returns_no_content(repository):
    app, _, run, api = repository
    refresh(app)
    async def revoke():
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run['id'],))
    api.before_blob = revoke
    with pytest.raises(ConnectorError):
        invoke(app, run, 'repository_context_read', commit_sha=CURRENT, path='app/auth.py')


def test_revocation_during_refresh_never_publishes(repository):
    app, _, _, api = repository
    async def revoke():
        select(app, (101,))
    api.before_blob = revoke
    with pytest.raises(ConnectorError):
        refresh(app)
    assert not app.state.store.rows('SELECT * FROM repository_context_snapshots')


def test_pagination_and_partial_coverage_are_explicit(repository):
    app, _, run, api = repository
    api.contents.update({f'app/auth_{i}.py': 'pass\n' for i in range(30)})
    api.truncated = True
    refresh(app)
    first = invoke(app, run, query='auth')
    assert len(first['matches']) == 12 and first['page']['next']
    assert first['coverage']['paths_incomplete'] is True
    second = invoke(app, run, query='auth', commit_sha=first['commit_sha'], cursor=first['page']['next'])
    assert not ({item['path'] for item in first['matches']} & {item['path'] for item in second['matches']})
    assert not any(item['path'] == 'linked.md' for item in first['matches'] + second['matches'])


def test_bad_or_large_sources_have_actionable_errors(repository):
    app, _, run, api = repository
    api.contents['huge.py'] = 'x' * 256001
    refresh(app)
    for path in ('assets/binary.dat', 'huge.py', '../README.md', 'linked.md'):
        with pytest.raises(ConnectorError):
            invoke(app, run, 'repository_context_read', commit_sha=CURRENT, path=path)
    assert excerpt('x' * 12001)['line_too_long'] is True


def test_failed_refresh_keeps_previous_guide_and_is_not_an_inference_dependency(repository, monkeypatch):
    app, _, run, api = repository
    refresh(app)
    index = app.state.repository_context
    before = index.context(run)
    api.commit = 'f' * 40
    async def scenario():
        entered = asyncio.Event()
        async def stalled(*args):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(index, 'refresh', stalled)
        monkeypatch.setattr(index, 'due', lambda: (index.github.connection_version(), 202))
        index.start()
        await asyncio.wait_for(entered.wait(), 1)
        started = time.monotonic()
        assert await asyncio.wait_for(database(index.context, run), 1) == before
        assert time.monotonic() - started < 1
        await asyncio.wait_for(index.close(), 1)
        assert index.worker is None
    asyncio.run(scenario())


def test_no_selected_repository_does_not_inject_another_connected_repo(repository):
    app, _, run, _ = repository
    refresh(app)
    app.state.store.execute("UPDATE runs SET github_repository_id=NULL,repo_url='' WHERE id=?", (run['id'],))
    run = app.state.store.run(run['id'])
    assert app.state.repository_context.context(run) == ''
    assert invoke(app, run, repository_id=202, query='authentication')['matches']


def test_polling_skips_unchanged_commits_and_never_runs_when_disabled(repository):
    app, _, _, api = repository
    index = app.state.repository_context
    refresh(app)
    reads = len(api.blob_calls)
    refresh(app)
    assert len(api.blob_calls) == reads
    assert index.due() is None
    app.state.store.execute('UPDATE repository_context_heads SET checked_at=0')
    assert index.due()[1] == 202
    app.state.settings.repository_context_enabled = False
    assert index.due() is None


def test_incomplete_document_fetch_is_repaired_at_the_same_commit(repository):
    app, _, run, api = repository
    async def fail():
        raise ConnectorError('Temporarily unavailable')
    api.before_blob = fail
    refresh(app)
    result = invoke(app, run, query='readme')
    assert result['coverage']['failed_documents'] == 3
    api.before_blob = None
    refresh(app)
    assert 'Authentication uses' in app.state.repository_context.context(run)
    assert invoke(app, run, query='readme')['coverage']['failed_documents'] == 0


def test_unused_context_does_no_database_work(repository, monkeypatch):
    app, _, run, _ = repository
    index = app.state.repository_context
    def forbidden(*args):
        raise AssertionError('Unused context must not touch the database')
    monkeypatch.setattr(index.store, 'run', forbidden)
    assert index.context({**run, 'plugins': []}) == ''
    app.state.settings.repository_context_enabled = False
    assert index.context(run) == ''


def test_cold_cache_does_not_wait_for_a_database_writer(repository):
    app, _, run, _ = repository
    done, result = threading.Event(), []
    def read():
        try:
            result.append(app.state.repository_context.context(run))
        finally:
            done.set()
    reader = threading.Thread(target=read)
    try:
        with app.state.store.connect() as conn:
            conn.begin_write()
            reader.start()
            assert done.wait(1), 'An absent guide waited for the database writer'
            assert result == ['']
    finally:
        reader.join(2)
