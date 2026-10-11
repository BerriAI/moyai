"""Integration against the real Hermes pin and the patches installed in Modal."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.config import Settings
from sandbox.hermes_compat import apply_hermes_patches
from test_workspace import workspace, recovery_catalog  # noqa: F401

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


def test_real_hermes_tool_read_recovers_and_continues_without_replaying_write(runtime, recovery_catalog, tmp_path, monkeypatch):
    import shlex
    from http.server import BaseHTTPRequestHandler
    from sandbox.agent import hermes_config
    from sandbox.broker_transport import unseal
    from test_broker_transport import diagnostic_relay
    from test_harnesses import background_chat_response

    _, source, python = runtime
    models, reads = [], []
    tool = 'mcp__workspace__github_repositories'
    script = 'from pathlib import Path; Path("executions.txt").open("a").write("completed-once\\n")'
    steps = [('terminal', {'command': shlex.quote(python) + ' -c ' + shlex.quote(script)}),
             ('tool_search', {'queries': ['github repositories']}),
             ('tool_describe', {'names': [tool]}),
             ('tool_call', {'calls': [{'name': tool, 'arguments': {}}]})]

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path == '/v1/models':
                self.reply(200, json.dumps({'object': 'list', 'data': [
                    {'id': 'openai/gpt-6-astra', 'object': 'model', 'owned_by': 'fixture'}]}).encode())
                return
            assert self.path == '/tools'
            self.reply(200, json.dumps(recovery_catalog).encode())
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            if self.path == '/tools/call':
                assert body == {'name': 'github_repositories', 'arguments': {}}
                reads.append(body)
                self.reply(524 if len(reads) == 1 else 200,
                           b'{}' if len(reads) == 1 else b'{"repositories":[],"receipt":"fixture-read-recovered"}')
                return
            assert self.path == '/v1/chat/completions'
            assert len(models) <= len(steps)
            if models:
                assert (tmp_path / 'executions.txt').read_text() == 'completed-once\n'
            message = {'role': 'assistant', 'content': 'read-recovery-complete'}
            if len(models) < len(steps):
                name, arguments = steps[len(models)]
                message.update(content=None, tool_calls=[{'id': f'recovery-{len(models)}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)}}])
            models.append(body)
            response = background_chat_response(body, message, len(models))
            self.reply(response.status_code, response.content, response.headers.get('Content-Type'))

    with diagnostic_relay(Edge) as (relay, _, diagnostics):
        profile = tmp_path / 'profile'
        profile.mkdir()
        monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'private-capability')
        config = hermes_config({'model': 'openai/gpt-6-astra'}, relay.url, tmp_path)
        config['mcp_servers']['workspace'].update(command=python, args=[str(ROOT / 'agent/tools/mcp_bridge.py')])
        config['agent'] = {'auto_recovery_cycles': 0}
        (profile / 'config.yaml').write_text(json.dumps(config))
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
        env.update(HERMES_HOME=str(profile), PYTHONPATH=source, HERMES_RUNTIME_DIR=str(tmp_path / 'runtimes'),
                   WORKSPACE_RUN_TOKEN='private-capability')
        result = subprocess.run([python, str(ROOT / 'tests/hermes_steering_probe.py'), 'read-recovery',
            '--broker-url', relay.url, '--model', config['model']['default']], env=env, cwd=tmp_path,
            text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        proof = json.loads(next(line.removeprefix('READ_RECOVERY_PROOF ') for line in result.stdout.splitlines()
                               if line.startswith('READ_RECOVERY_PROOF ')))
        assert proof['completed'] and proof['final_response'] == 'read-recovery-complete'
        assert len(models) == 5 and len(reads) == 2
        assert 'fixture-read-recovered' in json.dumps(models[-1]['messages'])
        assert (tmp_path / 'executions.txt').read_text() == 'completed-once\n'
        assert proof['completed_tools'].count('terminal') == proof['completed_tools'].count(tool) == 1
        assert not relay.last_error and relay.last_failure is None and not relay.uncertain_tool and not diagnostics


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
