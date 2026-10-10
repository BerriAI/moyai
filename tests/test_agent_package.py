from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent import agent
from agent.prompts import system_prompt
from sandbox import agent as sandbox_agent


def test_should_keep_repository_cwd_and_workspace_root_distinct(monkeypatch, tmp_path):
    root, session = tmp_path / 'workspace', tmp_path / 'session'
    repository = root / 'repo'
    repository.mkdir(parents=True)
    captured = {}

    def create_agent(harness, **kwargs):
        captured.update(kwargs)

        def run_conversation(prompt, **conversation):
            captured.update(conversation)
            return {'completed': True, 'final_response': 'Done.'}

        return SimpleNamespace(validate=lambda: None, run_conversation=run_conversation, close=lambda: None)

    monkeypatch.setattr(agent, 'create_agent', create_agent)
    outcome = agent.run(
        {'run_id': 'test-session', 'prompt': 'Inspect the repository.', 'harness': 'codex'},
        relay=SimpleNamespace(last_error='', wait_group='', wait_credential=''),
        workspace=repository, workspace_root=root, session=session, config={}, emit=lambda *args, **kwargs: None,
    )
    assert outcome['exit_code'] == 0
    assert captured['cwd'] == str(repository)
    assert f'Work only within {root}.' in captured['system_message']
    assert f'attachments are saved under {root}/.moyai-attachments.' in captured['system_message']
    assert f'references under {session}' in captured['system_message']


def test_should_keep_the_default_production_prompt_paths():
    prompt = system_prompt({'harness': 'codex'})
    assert 'Work only within /workspace.' in prompt
    assert 'attachments are saved under /workspace/.moyai-attachments.' in prompt
    assert 'references under /session' in prompt


@pytest.mark.parametrize('failure_stage', ['factory', 'history', 'close'])
def test_should_release_session_resources_after_startup_or_cleanup_failure(monkeypatch, tmp_path, failure_stage):
    captured = {}
    open_context = agent.open_context

    def fail():
        raise RuntimeError(f'{failure_stage} failed')

    def open_session(*args):
        context = captured['context'] = open_context(*args)
        if failure_stage == 'history':
            context.history = fail
        return context

    def create_agent(harness, **kwargs):
        if failure_stage == 'factory':
            fail()

        def close():
            captured['harness_closed'] = True
            if failure_stage == 'close':
                fail()

        return SimpleNamespace(validate=lambda: None, close=close,
                               run_conversation=lambda *args, **kwargs: {'completed': True, 'final_response': 'Done.'})

    monkeypatch.setattr(agent, 'open_context', open_session)
    monkeypatch.setattr(agent, 'create_agent', create_agent)
    with pytest.raises(RuntimeError, match=f'{failure_stage} failed'):
        agent.run(
            {'run_id': 'test-session', 'prompt': 'Inspect the repository.', 'harness': 'codex'},
            relay=SimpleNamespace(last_error='', wait_group='', wait_credential=''),
            workspace=tmp_path, session=tmp_path / 'session', config={}, emit=lambda *args, **kwargs: None,
        )
    if failure_stage != 'factory':
        assert captured['harness_closed']
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        captured['context'].db.execute('SELECT 1')


@pytest.mark.parametrize('fail_after_final', [False, True])
def test_should_save_final_artifact_before_agent_cleanup(monkeypatch, tmp_path, fail_after_final):
    locations = {path: tmp_path / path.removeprefix('/') for path in ('/workspace', '/session', '/artifacts')}
    monkeypatch.setattr(sandbox_agent, 'Path', lambda path: locations.get(str(path), Path(path)))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'test-capability')
    monkeypatch.setattr(sandbox_agent.os, 'chdir', lambda path: None)
    monkeypatch.setattr(sandbox_agent, 'prepare_attachments', lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox_agent, 'prepare_project', lambda *args: None)
    events = []
    artifact = locations['/artifacts'] / 'result.md'

    def run_conversation(spec, *, emit, workspace, workspace_root, **kwargs):
        assert workspace == workspace_root == locations['/workspace']
        emit('final', 'Tests passed.', completed=True)
        assert artifact.read_text() == 'Tests passed.'
        events.append('agent_cleanup')
        if fail_after_final:
            raise RuntimeError('Cleanup failed after the final response.')
        return {'summary': 'Tests passed.', 'exit_code': 0}

    def finish_recording(request, *, start):
        assert request == {'action': 'finish'} and start is False
        events.append('recording_finished')
        return {}

    monkeypatch.setattr(sandbox_agent, 'emit', lambda kind, *args, **kwargs: events.append(kind))
    monkeypatch.setattr(sandbox_agent, 'run_conversation', run_conversation)
    monkeypatch.setattr(sandbox_agent, 'computer_request', finish_recording)
    monkeypatch.setattr(sandbox_agent, 'collect_archive', lambda *args: events.append('archive'))
    spec = {'repo_url': '', 'harness': 'codex', 'model': 'test-model'}
    if fail_after_final:
        with pytest.raises(RuntimeError, match='Cleanup failed'):
            sandbox_agent._run_agent(spec, SimpleNamespace(url='http://broker'))
        assert events == ['final', 'agent_cleanup', 'recording_finished']
    else:
        assert sandbox_agent._run_agent(spec, SimpleNamespace(url='http://broker')) == 0
        assert events == ['final', 'agent_cleanup', 'recording_finished', 'archive']
    assert artifact.read_text() == 'Tests passed.'
