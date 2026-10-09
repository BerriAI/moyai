import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import modal
import pytest
from fastapi import HTTPException

from app.environments import Recipe, SaveRecipe, Environments
from app.environment_templates import TEMPLATES
from app.db import Store
from app.config import Settings
from app.durable_runner import DurableRunner
from test_workspace import workspace  # noqa: F401
from test_user_roles import users_app, sign_as  # noqa: F401
from test_durable import durable, drive, aio  # noqa: F401


def recipe(**fields):
    return Recipe(repository_id=101, name='Test project', repository='BerriAI/litellm', verify='true', **fields)


def prepared(env, *, default=True, identity='e' * 32):
    env.save_recipe(identity, SaveRecipe(recipe=recipe()), 'admin')
    build = env.enqueue(identity, 1, 'admin')
    env.update(build['id'], phase='ready', snapshot_id='im-project', commit_sha='a' * 40)
    env.store.execute('UPDATE environments SET enabled=1,is_default=?,active_build=? WHERE id=?', (default, build['id'], identity))
    return build['id']


def test_admin_recipe_api_and_csrf(workspace, monkeypatch):
    monkeypatch.setattr('app.github.GitHub.public_repository', AsyncMock(return_value={'id': 101, 'full_name': 'BerriAI/litellm', 'private': False}))
    app, client = workspace
    body = {'recipe': recipe().model_dump()}
    assert client.post('/api/admin/environments', json=body, headers={'X-CSRF-Token': ''}).status_code == 403
    result = client.post('/api/admin/environments', json=body)
    assert result.status_code == 201
    item = result.json()
    assert item['revision'] == 1
    assert client.get('/api/environments').json() == []
    assert client.put('/api/admin/environments/' + item['id'], json=body).status_code == 409
    assert client.put('/api/admin/environments/' + item['id'] + '/policy', json={'enabled': True}).status_code == 409
    body['recipe']['apt_packages'] = ['--allow-unauthenticated']
    assert client.post('/api/admin/environments', json=body).status_code == 422


def test_internal_users_only_see_ready_catalog(users_app):
    app, client = users_app
    env = app.state.environments
    build_id = prepared(env)
    sign_as(app, client, 'maya@berri.ai')
    catalog = client.get('/api/environments').json()
    assert len(catalog) == 1 and 'recipe' not in catalog[0] and 'builds' not in catalog[0]
    assert client.get('/api/admin/environments').status_code == 403
    assert client.get('/api/admin/environment-builds/' + build_id).status_code == 403
    assert client.post('/api/admin/environments', json={'recipe': recipe().model_dump()}).status_code == 403
    assert client.put('/api/admin/environments/' + 'e' * 32 + '/policy', json={'enabled': False}).status_code == 403


def test_pin_once_checkpoint_precedence_and_opt_out(workspace):
    app, _ = workspace
    env, store = app.state.environments, app.state.store
    build_id = prepared(env)
    run = store.create_run('Use this project', '', 'modal', [], chat_enabled=True)
    context = env.bind(run['id'])
    assert context['build_id'] == build_id and context['snapshot_id'] == 'im-project'
    assert store.run(run['id'])['repo_url'] == ''  # Preserve original submission for idempotent retries.
    env.store.execute('UPDATE environments SET enabled=0,active_build=?', ('some-new-build',))
    assert env.bind(run['id'])['build_id'] == build_id
    old = store.create_run('Existing task', '', 'modal', [], chat_enabled=True)
    store.update_run(old['id'], snapshot_id='im-session')
    assert env.bind(old['id']) == {}
    opted_out = store.create_run('Base only task', '', 'modal', [], environment_id='none')
    assert env.bind(opted_out['id']) == {}


def test_selection_matches_repository_and_rejects_mismatch(workspace):
    app, _ = workspace
    env = app.state.environments
    prepared(env, default=False)
    assert env.choose('auto', '') is None
    assert env.choose('auto', 'https://github.com/berriai/litellm', 101)['id'] == 'e' * 32
    assert env.choose('auto', 'https://github.com/BerriAI/another') is None
    with pytest.raises(HTTPException) as exc:
        env.choose('e' * 32, 'https://github.com/BerriAI/another')
    assert exc.value.status_code == 422


def test_edits_and_failed_rebuild_keep_last_good_snapshot(workspace):
    app, _ = workspace
    env = app.state.environments
    build_id = prepared(env)
    new = recipe()
    new.repository = 'BerriAI/moyai'
    env.save_recipe('e' * 32, SaveRecipe(recipe=new, revision=1), 'admin')
    build = env.enqueue('e' * 32, 2, 'admin')
    assert env.enqueue('e' * 32, 2, 'admin')['id'] == build['id']
    env.update(build['id'], phase='failed')
    assert env.get('e' * 32)['active_build'] == build_id
    assert env.catalog()[0]['repository'] == 'BerriAI/litellm'


def test_environment_choice_is_part_of_submission_idempotency(workspace):
    app, client = workspace
    body = {'prompt': 'Preview this project', 'mode': 'demo', 'client_id': 'same-submission'}
    first = client.post('/api/runs', json=body).json()
    assert client.post('/api/runs', json=body).json()['id'] == first['id']
    assert client.post('/api/runs', json={**body, 'environment_id': 'none'}).status_code == 409


@pytest.mark.asyncio
async def test_durable_provision_uses_project_then_session_snapshot(durable, monkeypatch):
    manager, cloud, run_id = durable
    env = Environments(manager.store, manager.settings, None, manager, None, SimpleNamespace(flush=AsyncMock()))
    build_id = prepared(env)
    manager.environments = env
    images = []
    monkeypatch.setattr('app.durable_runner.modal.Image.from_id', lambda value, **kw: images.append(value) or 'image')
    await drive(manager, run_id, phase='monitor')
    assert images == ['im-project']
    assert manager.store.run(run_id)['environment_build_id'] == build_id
    assert cloud.machines[0].spec['project_environment']['build_id'] == build_id
    assert cloud.machines[0].spec['repo_url'].endswith('/BerriAI/litellm')
    # A saved session's changed files take precedence over the clean template.
    state = dict(manager.state(run_id), sandbox_id='', snapshot_id='im-session', segment=1)
    await manager.provision(run_id, state)
    assert images[-1] == 'im-session'


@pytest.mark.asyncio
async def test_disabled_environment_while_queued_fails_without_infinite_retry(durable):
    manager, _, run_id = durable
    env = Environments(manager.store, manager.settings, None, manager, None, SimpleNamespace(flush=AsyncMock()))
    prepared(env)
    manager.environments = env
    manager.store.execute('UPDATE runs SET environment_id=? WHERE id=?', ('e' * 32, run_id))
    manager.store.execute('UPDATE environments SET enabled=0')
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'failed'
    assert 'enable this environment' in manager.store.run(run_id)['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False])
async def test_build_reattaches_publishes_only_after_verification(workspace, monkeypatch, success):
    app, _ = workspace
    env = app.state.environments
    old = prepared(env)
    env.save_recipe('e' * 32, SaveRecipe(recipe=recipe(), revision=1), 'admin')
    build = env.enqueue('e' * 32, 2, 'admin')
    env.update(build['id'], phase='building', sandbox_id='sb-existing')
    sandbox = SimpleNamespace(object_id='sb-existing', terminate=aio(AsyncMock()), snapshot_filesystem=aio(AsyncMock(return_value=SimpleNamespace(object_id='im-new'))))
    monkeypatch.setattr(env.manager, 'client', AsyncMock(return_value='client'))
    monkeypatch.setattr('app.environments.modal.Sandbox.from_name', aio(AsyncMock(return_value=sandbox)))
    env.rpc = AsyncMock(return_value={'done': True, 'success': success, 'commit_sha': 'b' * 40, 'log': 'Verification result'})
    await env.advance(env.build(build['id']))
    assert env.build(build['id'])['phase'] == ('ready' if success else 'failed')
    assert env.get('e' * 32)['active_build'] == (build['id'] if success else old)
    assert env.rpc.await_args.args[1] == 'status'  # No setup replay after restart.
    assert sandbox.snapshot_filesystem.aio.await_count == (1 if success else 0)


@pytest.mark.asyncio
async def test_cancel_during_snapshot_does_not_publish(workspace, monkeypatch):
    app, _ = workspace
    env = app.state.environments
    old = prepared(env)
    build = env.enqueue('e' * 32, 1, 'admin')
    env.update(build['id'], phase='building')
    async def snapshot(**kw):
        env.update(build['id'], phase='cancelling')
        return SimpleNamespace(object_id='im-unwanted')
    sandbox = SimpleNamespace(object_id='sb-existing', snapshot_filesystem=aio(snapshot))
    monkeypatch.setattr(env.manager, 'client', AsyncMock(return_value='client'))
    monkeypatch.setattr('app.environments.modal.Sandbox.from_name', aio(AsyncMock(return_value=sandbox)))
    env.rpc = AsyncMock(return_value={'done': True, 'success': True, 'commit_sha': 'b' * 40})
    await env.advance(env.build(build['id']))
    assert env.get('e' * 32)['active_build'] == old
    assert env.build(build['id'])['phase'] == 'cancelling'


def test_litellm_template_has_real_proxy_database_checks():
    template = Recipe.model_validate(TEMPLATES[0])
    assert 'uv sync --frozen' in template.setup
    assert 'litellm.proxy.proxy_server' in template.verify
    assert 'prisma db push' in template.verify and 'generate_series(1,100)' in template.verify
    assert 'postgresql stop' in template.shutdown
    assert '/health/readiness' in template.verify


@pytest.mark.parametrize('existing_env', [False, True])
@pytest.mark.parametrize('has_schema', [False, True])
def test_litellm_setup_persists_defaults_and_requires_prisma(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing_env: bool, has_schema: bool,
) -> None:
    import os
    import subprocess
    binaries = tmp_path / '.venv/bin'
    binaries.mkdir(parents=True)
    for name, body in {
        'python': 'exit 0',
        'uv': 'if test "$1" = sync; then printf "%s\\n" "${CARGO_BUILD_JOBS-}" "${CARGO_PROFILE_DEV_DEBUG-}" > cargo-settings; fi',
        'prisma': 'test -f "${2#--schema=}" || exit 9\nprintf generated > prisma-ran',
    }.items():
        executable = binaries / name
        executable.write_text('#!/bin/sh\n' + body + '\n')
        executable.chmod(0o755)
    if has_schema:
        (tmp_path / 'schema.prisma').write_text('fixture schema')
    if existing_env:
        (tmp_path / '.env').write_text('DATABASE_URL=postgresql://custom.invalid/existing\n')
    monkeypatch.delenv('CARGO_BUILD_JOBS', raising=False)
    monkeypatch.delenv('CARGO_PROFILE_DEV_DEBUG', raising=False)
    monkeypatch.setenv('PATH', str(binaries) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', TEMPLATES[0]['setup']], cwd=tmp_path, capture_output=True)
    assert result.returncode == (0 if has_schema else 9)
    assert (tmp_path / 'cargo-settings').read_text() == '2\n0\n'
    assert (tmp_path / 'prisma-ran').exists() is has_schema
    saved = (tmp_path / '.env').read_text()
    assert saved == ('DATABASE_URL=postgresql://custom.invalid/existing\n' if existing_env else
                     'DATABASE_URL=postgresql://moyai_dev:local-development-only@127.0.0.1:5432/moyai_dev\n')


@pytest.mark.parametrize('database', ['connected', 'Not connected'])
def test_litellm_verification_rejects_db_less_health_and_exercises_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: str,
) -> None:
    import io
    import subprocess
    import urllib.request
    requests, lifecycle = [], []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: lifecycle.append('terminated'), wait=lambda **kw: 0)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)

    def respond(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        path = request.full_url.removeprefix('http://127.0.0.1:18000')
        requests.append(path)
        if path == '/health/readiness':
            data = {'status': 'healthy', 'db': database}
        elif path == '/key/generate':
            assert json.loads(request.data)['key_alias'] == 'moyai-environment-smoke'
            data = {'key': 'sk-fixture-only'}
        elif path == '/key/info':
            assert request.get_header('Authorization') == 'Bearer sk-fixture-only'
            data = {'info': {'key_alias': 'moyai-environment-smoke'}}
        elif path == '/key/delete':
            assert json.loads(request.data) == {'keys': ['sk-fixture-only']}
            data = {'deleted_keys': ['sk-fixture-only']}
        else:
            raise AssertionError('Unexpected endpoint: ' + path)
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', respond)
    source = TEMPLATES[0]['verify'].rsplit(".venv/bin/python - <<'PY'\n", 1)[1].split('\nPY', 1)[0]
    source = source.replace('/tmp/moyai-proxy-smoke.log', str(tmp_path / 'proxy.log'))
    if database == 'connected':
        exec(source, {})
        assert requests == ['/health/readiness', '/key/generate', '/key/info', '/key/delete']
    else:
        with pytest.raises(RuntimeError, match='database is not connected'):
            exec(source, {})
        assert requests == ['/health/readiness']
    assert lifecycle == ['terminated']


def test_daily_refresh_is_opt_in_and_coalesces_pending_builds(workspace):
    app, _ = workspace
    env = app.state.environments
    old = prepared(env)
    env.update(old, created_at='2020-01-01T00:00:00+00:00')
    env.queue_refreshes()
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1
    env.store.execute('UPDATE environments SET refresh_daily=1')
    env.queue_refreshes()
    env.queue_refreshes()
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 2
    pending = env.store.rows("SELECT * FROM environment_builds WHERE phase='queued'")[0]
    assert pending['actor'] == 'Automatic refresh'
    env.update(pending['id'], phase='failed')
    env.queue_refreshes()
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 2  # Failed builds do not retry in a tight loop.


def test_auto_refresh_schedules_edited_recipes_and_preserves_active_build(workspace):
    app, _ = workspace
    env = app.state.environments
    old = prepared(env)
    env.store.execute('UPDATE environments SET refresh_daily=1')
    env.save_recipe('e' * 32, SaveRecipe(recipe=recipe(), revision=1), 'admin')
    assert env.get('e' * 32)['active_build'] == old
    assert env.store.rows("SELECT revision FROM environment_builds WHERE phase='queued'")[0]['revision'] == 2


@pytest.mark.asyncio
async def test_subagents_inherit_pinned_environment(durable):
    from test_agents import launch
    manager, _, run_id = durable
    manager.store.execute('UPDATE runs SET environment_id=?,environment_build_id=? WHERE id=?', ('e' * 32, 'pinned-build', run_id))
    coordinator, result, _ = await launch(durable)
    children = coordinator.children(result['group_id'])
    for child in children:
        run = manager.store.run(child['id'])
        assert run['environment_id'] == 'e' * 32 and run['environment_build_id'] == 'pinned-build'


@pytest.mark.asyncio
async def test_older_build_does_not_publish_over_a_newer_recipe(workspace, monkeypatch):
    app, _ = workspace
    env = app.state.environments
    old = prepared(env)
    build = env.enqueue('e' * 32, 1, 'admin')
    env.update(build['id'], phase='building')
    env.save_recipe('e' * 32, SaveRecipe(recipe=recipe(), revision=1), 'admin')
    sandbox = SimpleNamespace(object_id='sb-existing', terminate=aio(AsyncMock()), snapshot_filesystem=aio(AsyncMock(return_value=SimpleNamespace(object_id='im-obsolete'))))
    monkeypatch.setattr(env.manager, 'client', AsyncMock(return_value='client'))
    monkeypatch.setattr('app.environments.modal.Sandbox.from_name', aio(AsyncMock(return_value=sandbox)))
    env.rpc = AsyncMock(return_value={'done': True, 'success': True, 'commit_sha': 'b' * 40})
    await env.advance(env.build(build['id']))
    assert env.get('e' * 32)['active_build'] == old
    assert env.build(build['id'])['phase'] == 'ready'


def test_project_startup_omits_agent_capabilities(monkeypatch, tmp_path):
    from sandbox.project_environment import prepare_project
    monkeypatch.setattr('sandbox.project_environment.Path', lambda _: tmp_path)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'private-broker-token')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-model-token')
    calls = []
    monkeypatch.setattr('sandbox.project_environment.subprocess.run', lambda *a, **kw: calls.append(kw))
    prepare_project({'project_environment': {'name': 'Project', 'commit_sha': 'a' * 40, 'startup': 'true'}}, lambda *a: None)
    assert 'WORKSPACE_RUN_TOKEN' not in calls[0]['env'] and 'OPENAI_API_KEY' not in calls[0]['env']


def test_clone_token_is_not_available_to_setup_or_saved_config(monkeypatch, tmp_path):
    import base64
    import os
    from sandbox import environment_build
    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)
    monkeypatch.setattr(environment_build, 'REPO', root)
    monkeypatch.setenv('MOYAI_CLONE_TOKEN', 'clone-only-test-token')
    calls = []
    monkeypatch.setattr(environment_build.subprocess, 'run', lambda args, **kw: calls.append((args, dict(kw.get('env', {})))))
    monkeypatch.setattr(environment_build.subprocess, 'check_output', lambda *a, **kw: 'a' * 40)
    scopes = []
    monkeypatch.setattr(environment_build, 'shell', lambda *a, **kw: scopes.append(os.environ.get('MOYAI_CLONE_TOKEN')))
    environment_build.build(recipe().model_dump())
    fetch = next(env for args, env in calls if 'fetch' in args)
    scheme, encoded = fetch['GIT_CONFIG_VALUE_0'].removeprefix('Authorization: ').split(' ')
    assert scheme == 'Basic'
    assert base64.b64decode(encoded).decode() == 'x-access-token:clone-only-test-token'
    assert scopes == [None, None, None, None]
    assert 'clone-only-test-token' not in (root / '.git/moyai.json').read_text()


def test_environment_build_fetches_through_authenticated_git_http(monkeypatch, tmp_path):
    """Exercise real Git fetch/checkout against a Basic-only smart HTTP server."""
    import base64
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    from sandbox import environment_build

    run = subprocess.run
    source = tmp_path / 'source'
    run(['git', 'init', '-b', 'main', str(source)], check=True, capture_output=True)
    (source / 'marker.txt').write_text('authenticated checkout')
    run(['git', '-C', str(source), 'add', '.'], check=True)
    run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
         'commit', '-m', 'Fixture'], check=True, capture_output=True)
    run(['git', 'clone', '--bare', str(source), str(tmp_path / 'repository.git')], check=True, capture_output=True)
    authenticated = []

    class GitServer(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def serve(self):
            header = self.headers.get('Authorization', '')
            if not header.startswith('Basic ') or base64.b64decode(header[6:]).decode() != 'x-access-token:fixture-token':
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Git"')
                self.end_headers()
                return
            authenticated.append(self.command)
            path = urlsplit(self.path)
            env = {**os.environ, 'GIT_PROJECT_ROOT': str(tmp_path), 'GIT_HTTP_EXPORT_ALL': '1',
                   'REQUEST_METHOD': self.command, 'PATH_INFO': path.path, 'QUERY_STRING': path.query,
                   'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'REMOTE_USER': 'fixture',
                   'HTTP_GIT_PROTOCOL': self.headers.get('Git-Protocol', '')}
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            result = run(['git', 'http-backend'], env=env, input=body, capture_output=True, check=True)
            headers, response = result.stdout.split(b'\r\n\r\n', 1)
            self.send_response(200)
            for line in headers.decode().split('\r\n'):
                name, value = line.split(':', 1)
                self.send_header(name, value.strip())
            self.end_headers()
            self.wfile.write(response)

        do_GET = do_POST = serve

    server = ThreadingHTTPServer(('127.0.0.1', 0), GitServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checkout = tmp_path / 'checkout'
    monkeypatch.setattr(environment_build, 'REPO', checkout)
    monkeypatch.setenv('MOYAI_CLONE_TOKEN', 'fixture-token')

    def local_git(args, **kwargs):
        if 'fetch' in args:
            args = [f'http://127.0.0.1:{server.server_port}/repository.git' if arg == 'origin' else arg for arg in args]
            # Scope the production header to the local GitHub stand-in.
            kwargs['env'] = {**kwargs['env'], 'GIT_CONFIG_KEY_0': 'http.extraHeader'}
        return run(args, **kwargs)

    monkeypatch.setattr(environment_build.subprocess, 'run', local_git)
    try:
        sha, _ = environment_build.build(recipe().model_dump())
        assert (checkout / 'marker.txt').read_text() == 'authenticated checkout'
        assert len(sha) == 40 and 'GET' in authenticated and 'POST' in authenticated
        assert 'fixture-token' not in (checkout / '.git/config').read_text()
        assert 'MOYAI_CLONE_TOKEN' not in os.environ
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_disabled_github_connection_cannot_supply_clone_credentials(workspace, monkeypatch):
    from app.connector_errors import ConnectorError
    app, _ = workspace
    env = app.state.environments
    env.save_recipe('e' * 32, SaveRecipe(recipe=recipe(clone_access='github')), 'admin')
    build = env.enqueue('e' * 32, 1, 'admin')
    sandbox = SimpleNamespace(object_id='sb-existing', filesystem=SimpleNamespace(write_text=aio(AsyncMock())))
    monkeypatch.setattr(env.manager, 'client', AsyncMock(return_value='client'))
    monkeypatch.setattr('app.environments.modal.Sandbox.from_name', aio(AsyncMock(return_value=sandbox)))
    monkeypatch.setattr(env.connectors, 'list', lambda: [{'id':'github','connected':True,'enabled':False}])
    token = AsyncMock()
    monkeypatch.setattr(env.connectors.github, 'installation_token', token)
    with pytest.raises(ConnectorError, match='Enable the shared'):
        await env.advance(build)
    token.assert_not_awaited()
