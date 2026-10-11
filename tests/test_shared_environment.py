"""Workers share preparation evidence, not running service processes."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.environments import Environments, EnvironmentPending
from app.temporal_runtime import TemporalRunManager
from app.db import Store
from test_environment_discovery import connections, environments, new_run
from test_durable import durable
from test_agents import attach
from test_environments import recipe


def child(env, parent, **fields):
    run = new_run(env, **fields)
    env.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent['id'], run['id']))
    return run


@pytest.mark.asyncio
async def test_snapshotless_workers_wait_for_one_build_and_keep_the_same_version(environments):
    env = environments
    root = new_run(env)
    children = [child(env, root) for _ in range(20)]
    results = await asyncio.gather(*(env.prepare(r['id']) for r in children), return_exceptions=True)
    assert all(isinstance(r, EnvironmentPending) for r in results)
    build_id = results[0].build_id
    assert {r.build_id for r in results} == {build_id}
    assert len(env.store.rows('SELECT id FROM environment_builds')) == 1
    assert all(env.store.run(r['id'])['environment_build_id'] == '' for r in [root, *children])
    build = env.build(build_id)
    env.update(build_id, phase='ready', snapshot_id='im-shared', commit_sha='a'*40)
    env.store.execute('UPDATE environments SET active_build=?,enabled=1,activate_on_ready=0 WHERE id=?', (build_id, build['environment_id']))
    assert (await env.prepare(children[0]['id']))['build_id'] == build_id
    # An administrator's later build must not split a single team across versions.
    newer = env.enqueue(build['environment_id'], build['revision'], 'Next build')
    env.update(newer['id'], phase='ready', snapshot_id='im-newer', commit_sha='b'*40)
    env.store.execute('UPDATE environments SET active_build=? WHERE id=?', (newer['id'], build['environment_id']))
    for r in children:
        result = await env.prepare(r['id'])
        assert (result['build_id'], result['snapshot_id'], result['commit_sha']) == (build_id, 'im-shared', 'a'*40)
    assert env.store.run(root['id'])['environment_build_id'] == build_id
    nested = child(env, children[-1])
    assert (await env.prepare(nested['id']))['build_id'] == build_id


@pytest.mark.asyncio
@pytest.mark.parametrize('preserved', ['snapshot', 'opt_out'])
async def test_existing_worker_workspace_and_opt_out_do_not_trigger_new_preparation(environments, preserved):
    env = environments
    root = new_run(env)
    worker = child(env, root, **({'environment_id': 'none'} if preserved == 'opt_out' else {}))
    if preserved == 'snapshot':
        env.store.update_run(worker['id'], snapshot_id='im-worker-files')
    assert await env.prepare(worker['id']) == {}
    assert not env.store.rows('SELECT id FROM environment_builds')
    assert env.store.run(root['id'])['environment_build_id'] == ''


@pytest.mark.asyncio
async def test_missing_and_cyclic_shared_dependencies_are_explicit(environments):
    env = environments
    root = new_run(env)
    for parent in ['missing-parent', root['id']]:
        env.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, root['id']))
        with pytest.raises(HTTPException, match='missing or cyclic'):
            await env.prepare(root['id'])
    assert not env.store.rows('SELECT id FROM environment_builds')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ready', 'failed', 'stopped'])
async def test_real_swarm_bootstrap_waits_without_worker_computers_and_survives_restart(durable, outcome):
    manager, cloud, _ = durable
    attach(manager)
    manager.settings.modal_token_id = manager.settings.modal_token_secret = 'test'
    env = Environments(manager.store, manager.settings, None, manager, connections(), SimpleNamespace(flush=AsyncMock()))
    manager.environments = env
    root = manager.store.create_run('Inspect the shared project', 'https://github.com/BerriAI/agentchat', 'modal', [],
        chat_enabled=True, model='anthropic/claude-opus-5-5', user_id='user:swarm', swarm_budget_seconds=0,
        durable_submit=manager.submit_in, swarm_bootstrap=manager.swarms.bootstrap_in)
    workers = manager.store.rows('SELECT id FROM runs WHERE parent_run_id=?', (root['id'],))
    assert len(workers) == 20
    for worker in workers:
        await manager.advance(worker['id'])
        await manager.advance(worker['id'])
        assert manager.state(worker['id'])['phase'] == 'waiting_environment'
    assert not cloud.machines and not cloud.launches and manager.has_capacity()
    builds = manager.store.rows('SELECT * FROM environment_builds')
    assert len(builds) == 1
    build = builds[0]
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    manager.environments = Environments(manager.store, manager.settings, None, manager, connections(), SimpleNamespace(flush=AsyncMock()))
    env = manager.environments
    if outcome == 'ready':
        env.update(build['id'], phase='ready', snapshot_id='im-shared', commit_sha='a'*40)
        env.store.execute('UPDATE environments SET active_build=?,enabled=1,activate_on_ready=0 WHERE id=?', (build['id'], build['environment_id']))
    elif outcome == 'failed':
        env.update(build['id'], phase='failed', error='Missing fixture dependency')
    else:
        manager.swarms.stop(root['id'])
    for worker in workers:
        await manager.advance(worker['id'])
        if outcome == 'ready':
            assert manager.store.run(worker['id'])['environment_build_id'] == build['id']
        elif outcome == 'failed':
            assert 'Project setup failed' in manager.store.run(worker['id'])['error']
    assert len(cloud.machines) == (len(workers) if outcome == 'ready' else 0)
    assert not cloud.launches


def test_complete_repository_recipe_and_build_output_are_retained(tmp_path, monkeypatch):
    from sandbox import detect_environment, environment_build
    instructions = 'Project guidance 😀\n' * 10000 + 'FINAL CONSTRAINT'
    commands = {key: '# setup context\n'*3000+'true' for key in ('setup', 'startup', 'verify', 'shutdown')}
    packages = ['package-'+str(i) for i in range(60)]
    path = tmp_path / '.moyai' / 'environment.json'
    path.parent.mkdir()
    path.write_text(json.dumps({**commands, 'instructions': instructions, 'apt_packages': packages}))
    resolved = detect_environment.detect(tmp_path, recipe().model_dump())
    validated = type(recipe()).model_validate(resolved)
    assert validated.instructions.startswith(instructions)
    assert validated.apt_packages == packages
    for key, value in commands.items():
        assert getattr(validated, key) == value
    monkeypatch.setattr(environment_build, 'ROOT', tmp_path)
    log = 'First readiness check\n' + 'log 😀\n'*15000 + 'Last readiness check\n'
    (tmp_path / 'output.log').write_text(log)
    assert environment_build.main('status')['log'] == log


@pytest.mark.parametrize('success', [False, True])
def test_restored_services_are_verified_before_readiness_is_reported(tmp_path, monkeypatch, success):
    from sandbox import project_environment
    monkeypatch.setattr(project_environment, 'activate_project', lambda _: tmp_path)
    events = []
    spec = {'project_environment': {'name': 'Shared fixture', 'commit_sha': 'a'*40,
        'startup': 'touch started', 'verify': 'test -f started' if success else 'test -f missing'}}
    if success:
        project_environment.prepare_project(spec, lambda *args: events.append(args))
        assert any('ready' in event[1] for event in events)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            project_environment.prepare_project(spec, lambda *args: events.append(args))
        assert not any('ready' in event[1] for event in events)
    assert (tmp_path / 'started').exists()
