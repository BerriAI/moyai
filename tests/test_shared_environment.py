"""Workers share preparation evidence, not running service processes."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.environments import Environments, EnvironmentPending
from app.temporal_runtime import TemporalRunManager
from app.db import Store
from test_environment_discovery import connections, environments, new_run
from test_durable import durable, sandbox_harness_catalog
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
async def test_legacy_swarm_bootstrap_waits_without_worker_computers_and_survives_restart(durable, sandbox_harness_catalog, outcome):
    manager, cloud, _ = durable
    attach(manager)
    manager.settings.independent_agent_runtime = False  # Exercise computer-bound saved runtimes.
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


@pytest.fixture
def project_checkout(tmp_path, monkeypatch):
    from sandbox import agent, project_environment, remote_tool
    root = tmp_path / 'workspace/repo'
    root.mkdir(parents=True)
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/org/project.git'],
                   check=True, capture_output=True)
    (root / '.git/moyai.json').write_text(json.dumps({'repository_id': 42, 'repository': 'org/project', 'base_sha': 'a' * 40}))
    (root / 'uncommitted.txt').write_text('Keep this unfinished work.')
    locations = {value: tmp_path / value.lstrip('/') for value in ('/workspace', '/workspace/repo', '/artifacts', '/session')}
    for module in (agent, project_environment, remote_tool):
        monkeypatch.setattr(module, 'Path', lambda value: locations.get(str(value), Path(value)))
    monkeypatch.setattr(agent, 'prepare_attachments', lambda *args, **kwargs: None)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'project-fixture-capability')
    monkeypatch.setenv('PATH', os.environ['PATH'])
    monkeypatch.chdir(tmp_path)
    return root


def project_spec(**changes):
    return {'repo_url': 'https://github.com/org/project', 'github_enabled': True,
            'broker_url': 'http://fixture', 'harness': 'codex', 'model': 'fixture', 'timeout': 60,
            'project_environment': {'name': 'Shared fixture', 'repository': 'org/project', 'repository_id': 42,
                'commit_sha': 'a' * 40, 'startup': 'touch started', 'verify': 'test -f started', **changes}}


@pytest.mark.parametrize('success', [False, True])
def test_restored_services_are_verified_before_readiness_is_reported(project_checkout, success):
    from sandbox import project_environment
    events = []
    spec = project_spec(verify='test -f started' if success else 'test -f missing')
    health = project_environment.prepare_project(spec, lambda *args: events.append(args))
    if success:
        assert health == {'status': 'ready', 'phase': 'verify'}
        assert any('ready' in event[1] for event in events)
    else:
        assert health == {'status': 'needs_repair', 'phase': 'verify', 'error_type': 'CalledProcessError', 'exit_code': 1}
        assert not any('ready' in event[1] for event in events)
    assert events[-1][2]['project_health'] == health
    assert (project_checkout / 'started').exists()


@pytest.mark.parametrize('phase', ['startup', 'verify'])
def test_native_preparation_failure_preserves_files_and_reaches_repair_prompt(project_checkout, monkeypatch, capfd, phase):
    from agent.prompts import system_prompt
    from sandbox import agent
    root = project_checkout
    metadata = (root / '.git/moyai.json').read_bytes()
    events, calls = [], []
    monkeypatch.setattr(agent, 'github_checkout', lambda *args, **kwargs: pytest.fail('Prepared checkout was mutated'))
    monkeypatch.setattr(agent, 'emit', lambda *args, **kwargs: events.append(args))
    monkeypatch.setattr(agent, 'computer_request', lambda *args, **kwargs: {})
    monkeypatch.setattr(agent, 'collect_archive', lambda *args: None)
    spec = {**project_spec(**{phase: 'touch partial-setup; echo PRIVATE-FAILURE-CONTENT >&2; exit 23'}),
            'github_enabled': False}

    def conversation(actual, **kwargs):
        calls.append(actual)
        health = actual['project_health']
        assert health == {'status': 'needs_repair', 'phase': phase, 'error_type': 'CalledProcessError', 'exit_code': 23}
        assert kwargs['workspace'] == root
        prompt = system_prompt(actual, workspace=root, session=root.parent.parent / 'session')
        assert 'needs_repair' in prompt and 'authorized terminal, file and GitHub tools' in prompt
        return {'exit_code': 0}

    monkeypatch.setattr(agent, 'run_conversation', conversation)
    assert agent._run_agent(spec, SimpleNamespace(url='http://fixture')) == 0
    assert len(calls) == 1 and (root / 'partial-setup').exists()
    assert (root / 'uncommitted.txt').read_text() == 'Keep this unfinished work.'
    assert (root / '.git/moyai.json').read_bytes() == metadata
    assert events[-1][2]['project_health'] == spec['project_health']
    assert not any('ready' in event[1] for event in events)
    captured = capfd.readouterr()
    assert 'PRIVATE-FAILURE-CONTENT' in captured.err
    assert 'PRIVATE-FAILURE-CONTENT' not in captured.out + json.dumps(events)


@pytest.mark.parametrize('restored', [False, True])
@pytest.mark.parametrize('github_enabled', [False, True])
@pytest.mark.parametrize('condition', ['healthy', 'missing', 'empty', 'wrong'])
def test_prepared_workspace_never_reclones_or_runs_recipe_on_wrong_root(
        project_checkout, monkeypatch, condition, github_enabled, restored):
    from sandbox import agent
    root = project_checkout
    preserved = root.parent / 'preserved-project'
    if condition != 'healthy':
        root.rename(preserved)
        if condition in {'empty', 'wrong'}:
            root.mkdir()
        if condition == 'wrong':
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/other/project.git'],
                           check=True, capture_output=True)
    source = root if condition == 'healthy' else preserved
    metadata = (source / '.git/moyai.json').read_bytes()
    monkeypatch.setattr(agent, 'github_checkout', lambda *args, **kwargs: pytest.fail('Automatic checkout must not run'))
    run = subprocess.run

    def commands(arguments, **kwargs):
        assert 'clone' not in arguments
        return run(arguments, **kwargs)

    monkeypatch.setattr(subprocess, 'run', commands)
    spec = {**project_spec(), 'github_enabled': github_enabled, 'context_checkpoint': restored}
    workspace, _ = agent.prepare_workspace(spec, SimpleNamespace())
    assert workspace == (root.parent if condition == 'missing' else root)
    assert spec['repo_url'] == 'https://github.com/org/project'
    assert spec['project_environment']['repository_id'] == 42
    assert (source / '.git/moyai.json').read_bytes() == metadata
    assert (source / 'uncommitted.txt').read_text() == 'Keep this unfinished work.'
    if condition == 'healthy':
        assert spec['project_health']['status'] == 'ready'
        assert (root / 'started').exists()
    else:
        assert spec['project_health']['status'] == 'needs_repair' and spec['project_health']['phase'] == 'root'
        assert not (root / 'started').exists() and not (preserved / 'started').exists()
        if condition == 'missing':
            assert not root.exists()
        elif condition == 'empty':
            assert list(root.iterdir()) == []


@pytest.mark.parametrize('condition', ['empty', 'wrong'])
def test_activation_does_not_load_an_unrelated_project_interpreter(project_checkout, condition):
    from sandbox.project_environment import activate_project
    root = project_checkout
    root.rename(root.parent / 'preserved-project')
    (root / '.venv/bin').mkdir(parents=True)
    if condition == 'wrong':
        subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/other/project.git'],
                       check=True, capture_output=True)
    before = os.environ['PATH']
    assert activate_project(project_spec())['status'] == 'needs_repair'
    assert os.environ['PATH'] == before


def test_unexpected_project_failure_is_not_reclassified_as_repair(project_checkout, monkeypatch):
    from sandbox import agent

    def failed(*args, **kwargs):
        raise RuntimeError('Unconfirmed preparation failure')

    monkeypatch.setattr(agent, 'prepare_project', failed)
    with pytest.raises(RuntimeError, match='Unconfirmed preparation failure'):
        agent.prepare_workspace(project_spec(), SimpleNamespace())


@pytest.mark.parametrize('repository_id', [42, 99])
@pytest.mark.parametrize('renamed', [False, True])
def test_project_identity_uses_permanent_id_and_matching_origin(project_checkout, repository_id, renamed):
    from sandbox.project_environment import prepare_project
    root = project_checkout
    repository = 'org/renamed-project' if renamed else 'org/project'
    subprocess.run(['git', '-C', str(root), 'remote', 'set-url', 'origin', f'https://github.com/{repository}.git'],
                   check=True, capture_output=True)
    metadata = json.dumps({'repository_id': repository_id, 'repository': repository, 'base_sha': 'a' * 40})
    (root / '.git/moyai.json').write_text(metadata)
    spec = project_spec()
    health = prepare_project(spec, lambda *args: None)
    assert health['status'] == ('ready' if repository_id == 42 else 'needs_repair')
    assert (root / 'started').exists() is (repository_id == 42)
    assert (root / '.git/moyai.json').read_text() == metadata
    assert spec['project_environment']['repository'] == 'org/project'


@pytest.mark.parametrize('metadata', [None, 'not-json', '[]', '{}',
    '{"repository_id":42,"repository":"org/renamed-project"}',
    '{"repository_id":true,"repository":"org/renamed-project","base_sha":"' + 'a' * 40 + '"}',
    '{"repository_id":42,"repository":"org/another-project","base_sha":"' + 'a' * 40 + '"}'])
def test_rename_needs_valid_metadata_agreeing_with_actual_origin(project_checkout, metadata):
    from sandbox.project_environment import prepare_project
    root = project_checkout
    subprocess.run(['git', '-C', str(root), 'remote', 'set-url', 'origin', 'https://github.com/org/renamed-project.git'],
                   check=True, capture_output=True)
    path = root / '.git/moyai.json'
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    health = prepare_project(project_spec(), lambda *args: None)
    assert health == {'status': 'needs_repair', 'phase': 'root', 'error_type': 'ProjectRootMismatch'}
    assert not (root / 'started').exists()
