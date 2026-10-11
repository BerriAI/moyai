"""Pinned Pi CLI + LiteLLM + real broker/MCP; only provider inference is scripted.

Set MOYAI_REQUIRE_PI_RUNTIME=1 in the runtime CI job to make missing dependencies
fail, rather than silently skipping this contract suite.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

from test_workspace import workspace
from test_codex_sdk_transport import background_gateway
from test_harnesses import background_chat_response


@pytest.fixture(autouse=True)
def pinned_runtime():
    try:
        import litellm
        from litellm.harness import PiOptions
        from sandbox.harness_dependencies import LITELLM_REVISION, PI_VERSION, runtime_version
        assert shutil.which('pi'), 'Pi is not installed'
        assert runtime_version('pi') == PI_VERSION
        revision = subprocess.check_output(['git', '-C', str(Path(litellm.__file__).parent.parent),
                                           'rev-parse', 'HEAD'], text=True).strip()
        assert revision == LITELLM_REVISION
    except (ImportError, AssertionError, subprocess.SubprocessError) as error:
        if os.environ.get('MOYAI_REQUIRE_PI_RUNTIME') == '1':
            pytest.fail(f'Required Pi runtime is unavailable: {error}')
        pytest.skip(f'Optional pinned Pi runtime: {error}')


def worker():
    """A fresh interpreter and native process for every invocation, including resume."""
    from types import SimpleNamespace
    import litellm
    from sandbox.broker_relay import BrokerRelay
    from agent.context_store import ContextStore
    from agent.harnesses.harness_registry import create_agent
    job = json.load(sys.stdin)
    os.environ['WORKSPACE_RUN_TOKEN'] = job['capability']
    directory = Path(job['directory'])
    events, sessions = [], []
    for name in ('aagent_session', 'aagent_resume'):
        original = getattr(litellm, name)
        def capture(*args, _factory=original, _name=name, **kwargs):
            sessions.append(_name)
            return _factory(*args, **kwargs)
        setattr(litellm, name, capture)
    relay = BrokerRelay(job['remote'], job['capability']).start()
    store = ContextStore(directory.parent / 'pi-context.sqlite3', job['run_id'])
    store.initialize([])
    agent = create_agent(job.get('harness', 'pi'), spec={'model': job['model'], 'timeout': job.get('timeout', 40),
        'max_iterations': job.get('max_iterations', 20)}, relay=relay,
        config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'agent/tools/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': job['capability']}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(['start', *args]),
            complete=lambda *args: events.append(['complete', *args]), commentary=lambda text: None),
        step=lambda: agent.interrupt() if job.get('stop_after_tool') and agent.journal.completed_tools else None,
        cwd=str(directory), context_store=store)
    proof = {}
    try:
        proof['result'] = agent.run_conversation(job.get('prompt', 'Perform each requested action once.'),
            conversation_history=[], system_message='Preserve completed receipts and the original constraints.')
    except Exception as error:
        proof['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        proof.update(events=events, sessions=sessions, pending=bool(store.pending or agent.journal and agent.journal.pending),
                     history=store.history(), native_reason=agent.native.reason if agent.native else None,
                     staged=bool(agent.native and agent.native.staged), calls=agent.model_calls,
                     relay_error=relay.last_error, uncertain_tool=relay.uncertain_tool)
        agent.close()
        proof['native_removed'] = not (directory.parent / '.native-sdk').exists()
        store.close()
        relay.close()
    print('PI_PROOF ' + json.dumps(proof))


def invoke(state, directory, **options):
    directory.mkdir(exist_ok=True)
    job = {'directory': str(directory), 'remote': state.relay.remote, 'capability': state.capability,
           'model': state.app.state.settings.agent_model, 'run_id': state.run['id'], **options}
    script = 'import sys; sys.path.insert(0, ' + repr(str(Path(__file__).parent)) + '); from test_pi_runtime import worker; worker()'
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(job),
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(next(line.removeprefix('PI_PROOF ') for line in result.stdout.splitlines()
                            if line.startswith('PI_PROOF ')))
    assert proof['native_removed'], proof
    return proof


def answer(body, state, text='pi-runtime-ok'):
    return background_chat_response(body, {'role': 'assistant', 'content': text}, len(state.requests))


def call(body, state, name, args):
    return background_chat_response(body, {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': f'pi-call-{len(state.requests)}', 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(args)}}]}, len(state.requests))


def saved_native(state):
    row = state.app.state.store.rows('SELECT encrypted FROM native_sessions WHERE run_id=?', (state.run['id'],))[0]
    return json.loads(state.app.state.security.decrypt(row['encrypted'])) if row['encrypted'] else None


def test_pi_native_files_and_real_authorized_mcp(workspace, tmp_path, monkeypatch):
    def upstream(body, state):
        names = {tool['function']['name'] for tool in body['tools']}
        step = len(state.requests)
        if step == 1:
            assert {'write', 'read', 'bash'} <= names
            return call(body, state, 'write', {'path': 'receipt.txt', 'content': 'native-file-receipt'})
        if step == 2:
            return call(body, state, 'read', {'path': 'receipt.txt'})
        if step == 3:
            name = next(name for name in names if name.endswith('skills_search'))
            return call(body, state, name, {'query': 'nonexistent-pi-fixture'})
        if step == 4:
            name = next(name for name in names if name.endswith('skills_load'))
            return call(body, state, name, {'skill_id': 'unavailable-fixture'})
        assert 'native-file-receipt' in json.dumps(body)
        return answer(body, state)
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        proof = invoke(state, tmp_path / 'project')
        assert proof.get('result', {}).get('completed'), proof
        assert len(state.requests) == 5 and proof['sessions'] == ['aagent_session']
        assert (tmp_path / 'project/receipt.txt').read_text() == 'native-file-receipt'
        starts = [event for event in proof['events'] if event[0] == 'start']
        receipts = [event for event in proof['events'] if event[0] == 'complete']
        assert len(starts) == len(receipts) == 4 and not proof['pending']
        assert [event[1] for event in starts] == [event[1] for event in receipts]
        assert receipts[-1][-1]['isError'] is True, receipts[-1]
        assert proof['staged'] and saved_native(state)


@pytest.mark.parametrize('harness', ['pi', 'opencode', 'deepagents', 'tool-loop'])
def test_native_tool_read_recovery_preserves_completed_write(workspace, tmp_path, monkeypatch, harness):
    """Each real adapter consumes the actual catalog and continues after an edge524."""
    from sandbox.broker_transport import unseal
    app, client = workspace
    writes, reads = [], []
    async def ensure(): return {}
    async def refresh(): pass
    async def send(name, arguments, **kwargs):
        writes.append(name)
        return {'receipt': 'completed-write-once'}
    monkeypatch.setattr(app.state.connectors.github, 'ensure_connection', ensure)
    monkeypatch.setattr(app.state.connectors.github, 'refresh_connection', refresh)
    monkeypatch.setattr(app.state.connectors.github, 'repository_options',
                        lambda: [{'id': 42, 'name': 'read-recovered'}])
    monkeypatch.setattr(app.state.connectors, 'call', send)
    for provider in ('github', 'slack'):
        app.state.connectors.save(provider, {'access_token': 'fixture'}, 'Controlled provider')

    def upstream(body, state):
        names = {tool['function']['name'] for tool in body.get('tools', [])}
        step = len(state.requests)
        if step == 1 and 'workspace_tools' in names:
            return call(body, state, 'workspace_tools', {})
        if not writes:
            name, arguments = 'slack_send', {'channel': 'me', 'text': 'controlled proof'}
        elif not reads:
            assert 'completed-write-once' in json.dumps(body)
            name, arguments = 'github_repositories', {}
        else:
            recovered = 'read-recovered' in json.dumps(body)
            return answer(body, state, 'read-recovery-complete' if recovered else 'read-unrecovered')
        if 'workspace_call' in names:
            return call(body, state, 'workspace_call', {'name': name, 'arguments_json': json.dumps(arguments)})
        return call(body, state, next(tool for tool in names if tool.endswith(name)), arguments)

    with background_gateway(tmp_path, monkeypatch, workspace, harness, upstream) as state:
        app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?',
                                (json.dumps(['github', 'slack']), state.run['id']))
        request = client.request
        def edge(method, url, **options):
            if method == 'POST' and url.endswith('/tools/call'):
                body = json.loads(unseal(state.capability, '/tools/call', options['content']))
                if body['name'] == 'github_repositories':
                    reads.append(body)
                    if len(reads) == 1:
                        return httpx.Response(524, json={'error': 'controlled edge timeout'})
            return request(method, url, **options)
        monkeypatch.setattr(client, 'request', edge)
        proof = invoke(state, tmp_path / 'project', harness=harness, timeout=60)
        assert proof.get('result', {}).get('completed'), proof
        assert proof['result']['final_response'] == 'read-recovery-complete'
        assert writes == ['slack_send'] and len(reads) == 2
        assert proof['sessions'] == ['aagent_session']
        assert not proof['pending'] and not proof['relay_error'] and not proof['uncertain_tool']
        assert 'completed-write-once' in json.dumps(proof['history'])


@pytest.mark.parametrize('failure', ['unauthorized', 'unavailable', 'truncated'])
def test_pi_model_failure_is_not_success_or_automatically_retried(workspace, tmp_path, monkeypatch, failure):
    def upstream(body, state):
        if len(state.requests) == 1:
            return call(body, state, 'bash', {'command': 'printf once >> executions.txt; printf completed-receipt'})
        if failure == 'truncated':
            return httpx.Response(200, content='data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n',
                                  headers={'Content-Type': 'text/event-stream'})
        return httpx.Response(401 if failure == 'unauthorized' else 503,
                              json={'error': {'message': 'scripted failure', 'code': failure}})
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        proof = invoke(state, tmp_path / 'project')
        assert not proof.get('result', {}).get('completed'), proof
        assert len(state.requests) == 2, 'Pi retried failed inference'
        assert (tmp_path / 'project/executions.txt').read_text() == 'once'
        assert 'completed-receipt' in json.dumps(proof['history'])
        assert not proof['pending'] and not proof['staged'] and saved_native(state) is None


@pytest.mark.parametrize('boundary', ['limit', 'stop'])
def test_pi_stop_and_budget_do_not_repeat_completed_tool(workspace, tmp_path, monkeypatch, boundary):
    def upstream(body, state):
        return call(body, state, 'bash', {'command': 'printf once >> executions.txt; printf completed-receipt'})
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        proof = invoke(state, tmp_path / 'project', max_iterations=1 if boundary == 'limit' else 20,
                       stop_after_tool=boundary == 'stop')
        assert not proof.get('result', {}).get('completed'), proof
        assert len(state.requests) == 1
        assert (tmp_path / 'project/executions.txt').read_text() == 'once'
        assert not proof['staged'] and not proof['pending']
        assert saved_native(state) is None


def test_pi_confirmed_overflow_rebuilds_from_receipts_once(workspace, tmp_path, monkeypatch):
    def upstream(body, state):
        if len(state.requests) == 1:
            return call(body, state, 'bash', {'command': 'printf once >> executions.txt; printf completed-receipt'})
        if len(state.requests) == 2:
            return httpx.Response(400, json={'error': {'code': 'context_length_exceeded', 'message': 'context too long'}})
        assert 'Original current request' in json.dumps(body) and 'completed-receipt' in json.dumps(body)
        return answer(body, state)
    def summarize(body, state):
        assert 'completed-receipt' in json.dumps(body)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': 'The shell already wrote executions.txt once. completed-receipt. Do not repeat it.'}}]})
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream,
                            on_public_summary=summarize) as state:
        proof = invoke(state, tmp_path / 'project')
        assert proof.get('result', {}).get('completed'), proof
        assert len(state.requests) == 3 and proof['sessions'] == ['aagent_session', 'aagent_session']
        assert (tmp_path / 'project/executions.txt').read_text() == 'once'
        assert not proof['pending'] and state.public_summaries == 1


@pytest.mark.parametrize('tamper', [False, True])
def test_pi_cold_resume_uses_fresh_capability_and_validates_snapshot(workspace, tmp_path, monkeypatch, tamper):
    import base64
    from app.security import digest
    def upstream(body, state):
        text = json.dumps(body)
        if len(state.requests) == 1:
            return answer(body, state, 'first-turn-native-receipt')
        assert 'first-turn-native-receipt' in text
        assert 'follow-up-request' in text
        return answer(body, state, 'resumed-turn-ok')
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        directory = tmp_path / 'project'
        first = invoke(state, directory)
        assert first.get('result', {}).get('completed') and first['staged'], first
        saved = saved_native(state)
        assert saved
        old_state = json.loads(saved['state']['state'])
        files = saved['state']['files']
        contents = '\n'.join(base64.b64decode(value).decode() for value in files.values())
        assert state.capability not in contents and state.relay.url not in contents
        assert all(name.startswith('pi/sessions/') for name in files)
        if tamper:
            saved['state']['files']['../escape.txt'] = base64.b64encode(b'invalid').decode()
            state.app.state.store.execute('UPDATE native_sessions SET encrypted=? WHERE run_id=?',
                (state.app.state.security.encrypt(json.dumps(saved)), state.run['id']))
        run = state.app.state.store.run(state.run['id'])
        state.app.state.store.finish_message(run['id'], run['active_message_id'], first['result']['final_response'])
        state.app.state.store.enqueue_message(run['id'], 'follow-up-request', 'pi-followup',
            model=run['model'], user_id=run['active_user_id'])
        state.app.state.store.claim_message(run['id'])
        state.capability = 'renewed-pi-fixture-capability'
        state.app.state.store.update_run(run['id'], status='running', token_hash=digest(state.capability))
        second = invoke(state, directory, prompt='follow-up-request')
        assert second.get('result', {}).get('completed'), second
        assert second['sessions'] == ['aagent_session' if tamper else 'aagent_resume'], second
        current = json.loads(saved_native(state)['state']['state'])
        assert (current['native_session_id'] == old_state['native_session_id']) is not tamper
        assert not (tmp_path / 'escape.txt').exists()
        assert not second['pending'] and second['staged']


def test_pi_failed_compaction_does_not_replay_work(workspace, tmp_path, monkeypatch):
    def upstream(body, state):
        if len(state.requests) == 1:
            return call(body, state, 'bash', {'command': 'printf once >> executions.txt; printf completed-receipt'})
        return httpx.Response(400, json={'error': {'code': 'context_length_exceeded'}})
    def failed_summary(body, state):
        return httpx.Response(503, json={'error': {'message': 'summary unavailable'}})
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream,
                            on_public_summary=failed_summary) as state:
        proof = invoke(state, tmp_path / 'project')
        assert 'ContextUnavailable' in proof.get('error', ''), proof
        assert len(state.requests) == 2 and proof['sessions'] == ['aagent_session']
        assert (tmp_path / 'project/executions.txt').read_text() == 'once'
        assert 'completed-receipt' in json.dumps(proof['history'])
        assert not proof['pending'] and not proof['staged'] and saved_native(state) is None


def test_pi_deadline_keeps_unfinished_tool_outcome_unresolved(workspace, tmp_path, monkeypatch):
    def upstream(body, state):
        return call(body, state, 'bash', {'command': 'printf once >> executions.txt; sleep 20'})
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        proof = invoke(state, tmp_path / 'project', timeout=4)
        assert not proof.get('result', {}).get('completed'), proof
        assert len(state.requests) == 1 and proof['sessions'] == ['aagent_session'], (proof,
            [body['messages'][-1] for body in state.requests])
        assert (tmp_path / 'project/executions.txt').read_text() == 'once'
        assert proof['pending'] and not proof['staged'] and saved_native(state) is None


def test_pi_project_extensions_do_not_run_at_startup(workspace, tmp_path, monkeypatch):
    directory = tmp_path / 'project'
    extension = directory / '.pi/extensions/untrusted.ts'
    extension.parent.mkdir(parents=True)
    marker = directory / 'extension-executed'
    extension.write_text('import {writeFileSync} from "node:fs";\n'
                         'export default function () { writeFileSync(' + json.dumps(str(marker)) + ', "executed"); }\n')
    def upstream(body, state):
        return answer(body, state)
    with background_gateway(tmp_path, monkeypatch, workspace, 'pi', upstream) as state:
        proof = invoke(state, directory)
        assert proof.get('result', {}).get('completed'), proof
        assert not marker.exists() and len(state.requests) == 1
