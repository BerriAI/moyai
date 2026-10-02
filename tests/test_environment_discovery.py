import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.db import Store
from app.temporal_runtime import TemporalRunManager
from app.environments import Environments, EnvironmentPending, SaveRecipe
from app.connector_errors import ConnectorError
from sandbox.detect_environment import detect, DetectionError, node_version
from test_environments import prepared, recipe
from test_workspace import workspace  # noqa: F401
from test_durable import durable, drive, aio  # noqa: F401


def connections():
    repos = ['BerriAI/litellm', 'BerriAI/agentchat']
    async def selected(run, repository):
        if repository not in repos:
            raise ConnectorError('Repository access was removed.')
        return repository
    return SimpleNamespace(list=lambda: [{'id': 'github', 'connected': True, 'enabled': True}],
                           credentials=AsyncMock(return_value={}),
                           github=SimpleNamespace(connected_targets=lambda _: repos, selected_target=AsyncMock(side_effect=selected)))


@pytest.fixture
def environments(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, modal_token_id='test', modal_token_secret='test')
    return Environments(Store(tmp_path), settings, None, SimpleNamespace(client=AsyncMock(return_value='client')),
                        connections(), SimpleNamespace(flush=AsyncMock()))


def new_run(env, repository='BerriAI/agentchat', **kwargs):
    return env.store.create_run('Use the project', 'https://github.com/' + repository if repository else '',
                                'modal', [], chat_enabled=True, **kwargs)


@pytest.mark.asyncio
async def test_discovery_registers_all_connected_repos_without_building(environments):
    env = environments
    await asyncio.gather(env.sync_repositories(), env.sync_repositories())
    assert {r['repository'] for r in env.catalog(True)} == {'BerriAI/litellm', 'BerriAI/agentchat'}
    assert not env.store.rows('SELECT id FROM environment_builds')
    assert not any(r['is_default'] or r['enabled'] for r in env.catalog(True))
    assert len(env.catalog()) == 2  # Selectable, but still awaiting first build.
    assert await env.prepare(new_run(env, '')['id']) == {}
    assert await env.prepare(new_run(env, environment_id='none')['id']) == {}
    assert not env.store.rows('SELECT id FROM environment_builds')


@pytest.mark.asyncio
async def test_discovery_preserves_admin_overrides_and_opt_out(environments):
    env = environments
    env.settings.auto_prepare_repositories = False
    await env.sync_repositories()
    assert env.catalog(True) == []
    env.settings.auto_prepare_repositories = True
    prepared(env)
    env.store.execute('UPDATE environments SET enabled=0,is_default=0')
    await env.sync_repositories()
    assert len(env.catalog(True)) == 2
    assert not env.get('e' * 32)['enabled']
    assert await env.prepare(new_run(env, 'BerriAI/litellm')['id']) == {}
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1


@pytest.mark.asyncio
async def test_first_sessions_share_one_pending_build(environments):
    env = environments
    runs = [new_run(env), new_run(env)]
    results = await asyncio.gather(*(env.prepare(r['id']) for r in runs), return_exceptions=True)
    assert all(isinstance(r, EnvironmentPending) for r in results)
    assert results[0].build_id == results[1].build_id
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1
    assert all(env.store.run(r['id'])['environment_build_id'] == '' for r in runs)
    env.update(results[0].build_id, phase='failed')
    with pytest.raises(HTTPException, match='Project setup failed'):
        await env.prepare(runs[0]['id'])
    await env.sync_repositories()
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1


@pytest.mark.asyncio
async def test_ready_environment_only_matches_its_repository(environments, monkeypatch):
    env = environments
    run = new_run(env)
    with pytest.raises(EnvironmentPending) as pending:
        await env.prepare(run['id'])
    build_id = pending.value.build_id
    env.update(build_id, phase='building', sandbox_id='sb-build')
    sandbox = SimpleNamespace(object_id='sb-build', terminate=aio(AsyncMock()),
                              snapshot_filesystem=aio(AsyncMock(return_value=SimpleNamespace(object_id='im-ready'))))
    monkeypatch.setattr('app.environments.modal.Sandbox.from_name', aio(AsyncMock(return_value=sandbox)))
    resolved = json.loads(env.build(build_id)['recipe'])
    resolved.update(startup='service example start', instructions='Detected instructions')
    env.rpc = AsyncMock(return_value={'done': True, 'success': True, 'commit_sha': 'a'*40, 'recipe': resolved})
    await env.advance(env.build(build_id))
    assert (await env.prepare(run['id']))['startup'] == 'service example start'
    assert (await env.prepare(new_run(env)['id']))['snapshot_id'] == 'im-ready'
    assert await env.prepare(new_run(env, '')['id']) == {}
    assert not any(r['is_default'] for r in env.catalog(True))
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1
    with pytest.raises(EnvironmentPending):
        await env.prepare(new_run(env, 'BerriAI/litellm')['id'])


@pytest.mark.asyncio
async def test_new_sessions_recheck_github_access_before_snapshot_use(environments):
    env = environments
    await env.sync_repositories()
    row = next(r for r in env.catalog(True) if r['repository']=='BerriAI/agentchat')
    build = env.enqueue(row['id'], 1, 'Test')
    env.update(build['id'], phase='ready', snapshot_id='im-private', commit_sha='a'*40)
    env.store.execute('UPDATE environments SET enabled=1,activate_on_ready=0,active_build=? WHERE id=?', (build['id'], row['id']))
    env.connectors.github.selected_target = AsyncMock(side_effect=ConnectorError('Repository access was removed.'))
    with pytest.raises(HTTPException, match='access was removed'):
        await env.prepare(new_run(env)['id'])


@pytest.mark.asyncio
async def test_temporal_waits_without_a_session_container_then_continues(durable):
    manager, cloud, run_id = durable
    env = Environments(manager.store, manager.settings, None, manager, connections(), SimpleNamespace(flush=AsyncMock()))
    manager.environments = env
    manager.settings.modal_token_id = manager.settings.modal_token_secret = 'test'
    manager.store.execute('UPDATE runs SET repo_url=? WHERE id=?', ('https://github.com/BerriAI/agentchat', run_id))
    await manager.advance(run_id)
    assert await manager.advance(run_id) == {'retry_seconds': 5}
    assert manager.state(run_id)['phase'] == 'waiting_environment'
    assert cloud.machines == [] and manager.has_capacity()
    assert await manager.advance(run_id) == {'retry_seconds': 5}
    assert len(manager.store.rows("SELECT id FROM events WHERE run_id=? AND message LIKE 'Preparing project environment:%'", (run_id,))) == 1
    build = env.store.rows('SELECT * FROM environment_builds')[0]
    env.update(build['id'], phase='failed')
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'cleanup'
    assert 'Project setup failed' in manager.store.run(run_id)['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_build_wait_survives_worker_restart_and_stop(durable, cancel):
    manager, cloud, run_id = durable
    manager.settings.modal_token_id = manager.settings.modal_token_secret = 'test'
    env = Environments(manager.store, manager.settings, None, manager, connections(), SimpleNamespace(flush=AsyncMock()))
    manager.environments = env
    manager.store.execute('UPDATE runs SET repo_url=? WHERE id=?', ('https://github.com/BerriAI/agentchat', run_id))
    await manager.advance(run_id)
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'waiting_environment'
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    manager.environments = Environments(manager.store, manager.settings, None, manager, connections(), SimpleNamespace(flush=AsyncMock()))
    assert await manager.advance(run_id) == {'retry_seconds': 5}
    build = env.store.rows('SELECT * FROM environment_builds')[0]
    if cancel:
        await manager.cancel(run_id)
    else:
        env.update(build['id'], phase='ready', snapshot_id='im-project', commit_sha='a'*40)
        env.store.execute('UPDATE environments SET enabled=1,activate_on_ready=0,active_build=? WHERE id=?', (build['id'], build['environment_id']))
    await drive(manager, run_id)
    assert len(cloud.machines) == (0 if cancel else 1)
    if not cancel:
        assert cloud.machines[0].spec['project_environment']['repository'] == 'BerriAI/agentchat'
        assert manager.store.run(run_id)['environment_build_id'] == build['id']
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1


@pytest.mark.parametrize('action', ['edit', 'disable', 'cancel'])
def test_admin_can_stop_automatic_setup(workspace, action):
    app, client = workspace
    env = app.state.environments
    # Drive discovery through the admin API, with no cloud worker credentials.
    env.connectors = connections()
    row = next(r for r in client.get('/api/admin/environments').json()['environments'] if r['repository']=='BerriAI/litellm')
    build = env.enqueue(row['id'], 1, 'Test')
    if action == 'edit':
        result = client.put('/api/admin/environments/' + row['id'], json={'revision':1, 'recipe':recipe().model_dump()})
    elif action == 'disable':
        result = client.put('/api/admin/environments/' + row['id'] + '/policy', json={'enabled':False})
    else:
        result = client.post('/api/admin/environment-builds/' + build['id'] + '/cancel')
    assert result.status_code == 200
    assert not env.get(row['id'])['activate_on_ready']
    client.get('/api/admin/environments')
    assert len(env.catalog(True)) == 2


def test_detection_uses_custom_configuration_and_keeps_identity(tmp_path):
    (tmp_path / '.moyai').mkdir()
    (tmp_path / '.moyai/environment.json').write_text(json.dumps({'setup':'echo setup', 'startup':'echo service', 'verify':'test -d .', 'apt_packages':['postgresql']}))
    original = recipe(setup_mode='detect').model_dump()
    resolved = detect(tmp_path, original)
    assert resolved['startup'] == 'echo service' and resolved['repository'] == original['repository']
    (tmp_path / '.moyai/environment.json').write_text(json.dumps({'verify':'true', 'clone_access':'public'}))
    with pytest.raises(DetectionError):
        detect(tmp_path, original)


@pytest.mark.parametrize('manifest,content,setup,verify', [
    ('uv.lock', '', 'uv sync --frozen', 'uv pip check'),
    ('requirements.txt', 'httpx==0.28.1', '-r requirements.txt', 'uv pip check'),
    ('pyproject.toml', '[project]\nname="example"', '-e .', 'uv pip check'),
    ('package.json', '{"name":"example","version":"1.0.0"}', 'npm install --engine-strict', 'npm ls'),
])
def test_detected_install_and_verification_are_specific_to_manifest(tmp_path, manifest, content, setup, verify):
    (tmp_path / manifest).write_text(content)
    detected = detect(tmp_path, recipe(setup_mode='detect').model_dump())
    assert setup in detected['setup'] and verify in detected['verify']


def test_node_lockfiles_and_versions_are_honored(tmp_path):
    (tmp_path / 'package.json').write_text('{"packageManager":"pnpm@10.12.1"}')
    (tmp_path / 'pnpm-lock.yaml').write_text('lockfileVersion: 9')
    (tmp_path / '.nvmrc').write_text('22')
    detected = detect(tmp_path, recipe(setup_mode='detect').model_dump())
    assert 'install-node 22' in detected['setup']
    assert 'pnpm@10.12.1' in detected['setup'] and '--frozen-lockfile' in detected['setup']
    (tmp_path / '.nvmrc').write_text('22; touch /tmp/unsafe')
    with pytest.raises(DetectionError):
        detect(tmp_path, recipe(setup_mode='detect').model_dump())


@pytest.mark.parametrize('manifest', ['.devcontainer.json', 'Cargo.toml', 'go.mod', 'yarn.lock'])
def test_unsupported_setup_is_not_reported_as_ready(tmp_path, manifest):
    (tmp_path / manifest).write_text('{}')
    if manifest == 'yarn.lock':
        (tmp_path / 'package.json').write_text('{}')
    with pytest.raises(DetectionError, match='recipe|devcontainer'):
        detect(tmp_path, recipe(setup_mode='detect').model_dump())
