"""Integration against the real Hermes pin and the patches installed in Modal."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.config import Settings
from sandbox.hermes_compat import apply_hermes_patches
from test_workspace import workspace  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def runtime(tmp_path_factory):
    source, python = os.environ.get('HERMES_TEST_SOURCE'), os.environ.get('HERMES_TEST_PYTHON')
    if not source or not python:
        pytest.skip('Set HERMES_TEST_SOURCE and HERMES_TEST_PYTHON for real Hermes steering tests')
    source, python = str(Path(source).resolve()), str(Path(python).absolute())
    revision = subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], text=True).strip()
    assert revision == Settings.model_fields['hermes_revision'].default
    # Patch a private checkout, never the caller's development tree.
    directory = tmp_path_factory.mktemp('hermes')
    original, target = directory / 'original', directory / 'patched'
    for checkout in (original, target):
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', source, str(checkout)], check=True)
        subprocess.run(['git', '-C', str(checkout), 'checkout', '--quiet', '--detach', revision], check=True)
    apply_hermes_patches(target)
    # Snapshots created from an already patched image must resume safely too.
    apply_hermes_patches(target)
    return str(original), str(target), python


@pytest.mark.parametrize('scenario,unpatched', [
    ('corrections', True), ('corrections', False), ('redirect-cap', False),
    ('provider-failure', False), ('stop', False), ('guards', False),
])
def test_real_hermes_steering(runtime, tmp_path, scenario, unpatched):
    original, patched, python = runtime
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text(json.dumps({
        'terminal': {'backend': 'local', 'cwd': str(tmp_path)},
        'tools': {'tool_search': {'enabled': 'off'}},
        'display': {'file_mutation_footer': False},
        'agent': {'auto_recovery_cycles': 0},
    }))
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
    env.update(HERMES_HOME=str(profile), PYTHONPATH=original if unpatched else patched,
               HERMES_RUNTIME_DIR=str(tmp_path / 'runtimes'))
    command = [python, str(ROOT / 'tests/hermes_steering_probe.py'), scenario]
    if unpatched:
        command.append('--unpatched')
    result = subprocess.run(command, env=env, cwd=tmp_path, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = next(line for line in result.stdout.splitlines() if line.startswith('STEERING_PROOF '))
    print(result.stdout)
    assert json.loads(proof.removeprefix('STEERING_PROOF '))['scenario'] == scenario


def test_real_hermes_background_compaction_preserves_tools(runtime, workspace, tmp_path, monkeypatch):
    import shlex
    from test_codex_sdk_transport import background_gateway
    from test_harnesses import BACKGROUND_RECEIPT_REPEATS, BACKGROUND_STEPS, background_chat_response
    _, source, python = runtime
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text(json.dumps({
        'terminal': {'backend': 'local', 'cwd': str(tmp_path)},
        'tools': {'tool_search': {'enabled': 'off'}},
        'security': {'allow_lazy_installs': False},
        'agent': {'auto_recovery_cycles': 0},
    }))
    steps = 0
    def upstream(body, state):
        nonlocal steps
        message = {'role': 'assistant', 'content': 'background-tools-ok'}
        if steps < BACKGROUND_STEPS:
            steps += 1
            script = ('import pathlib; pathlib.Path("executions.txt").open("a").write('
                + repr(str(steps) + '\n') + '); print(' + repr(f'receipt-{steps} ' + state.tail_marker)
                + f" + ' x' * {BACKGROUND_RECEIPT_REPEATS})")
            message['content'] = None
            message['tool_calls'] = [{'id': f'background-{steps}', 'type': 'function',
                'function': {'name': 'terminal', 'arguments': json.dumps({
                    'command': shlex.quote(python) + ' -c ' + shlex.quote(script)})}}]
        return background_chat_response(body, message, len(state.requests))
    with background_gateway(tmp_path, monkeypatch, workspace, 'hermes', upstream) as state:
        window = state.relay.context_window()
        assert window['live_compaction'] is True
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
        env.update(HERMES_HOME=str(profile), PYTHONPATH=source,
                   HERMES_RUNTIME_DIR=str(tmp_path / 'runtimes'), WORKSPACE_RUN_TOKEN=state.capability)
        result = subprocess.run([python, str(ROOT / 'tests/hermes_steering_probe.py'), 'background',
            '--broker-url', state.relay.url, '--model', state.app.state.settings.agent_model,
            '--live-compaction'], env=env, cwd=tmp_path, text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        proof = json.loads(next(line.removeprefix('BACKGROUND_PROOF ') for line in result.stdout.splitlines()
                               if line.startswith('BACKGROUND_PROOF ')))
        assert proof['completed'] and proof['final_response'] == 'background-tools-ok'
        assert state.held_calls >= 2 and state.projections and not state.faults
        assert state.tail_marker not in json.dumps(state.summaries[0])
        assert (tmp_path / 'executions.txt').read_text().splitlines() == [str(index) for index in range(1, BACKGROUND_STEPS + 1)]
        started = [call for event, call in proof['events'] if event == 'start']
        completed = [call for event, call in proof['events'] if event == 'complete']
        assert len(started) == len(set(started)) == BACKGROUND_STEPS and sorted(started) == sorted(completed)
        assert state.summary_marker not in json.dumps(state.original)
        assert not proof['private_summary_visible']
        print(f'Hermes: {len(state.requests)} foreground calls, {state.held_calls} while summary held, '
              f'{len(state.projections)} projected calls, {BACKGROUND_STEPS} tool receipts, one native process')


def test_incompatible_snapshot_is_rejected_before_any_patch_is_applied(runtime, tmp_path):
    original, _, _ = runtime
    (tmp_path / 'agent').mkdir()
    for name in ('conversation_loop.py', 'turn_iteration_prep.py', 'turn_finalizer.py'):
        shutil.copyfile(Path(original) / 'agent' / name, tmp_path / 'agent' / name)
    # A runtime that cannot accept the second patch must not get half upgraded.
    (tmp_path / 'agent/turn_finalizer.py').write_text('# Incompatible runtime\n')
    before = {path.name: path.read_bytes() for path in (tmp_path / 'agent').iterdir()}
    with pytest.raises(RuntimeError, match='does not match this runtime'):
        apply_hermes_patches(tmp_path)
    assert {path.name: path.read_bytes() for path in (tmp_path / 'agent').iterdir()} == before
