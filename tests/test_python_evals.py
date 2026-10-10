import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
import pytest

from evals import agent_worker
from evals.agent import AgentRunError, MoyaiAgent

BUILD = 'a' * 40
ENV = {
    'LITELLM_API_BASE': 'https://gateway.example', 'LITELLM_API_KEY': 'model-secret',
    'AGENT_MODEL': 'openai/gpt-6.1-sol', 'LITELLM_TRACE_ENDPOINT': 'https://lens.example/v1/traces',
    'LITELLM_TRACE_API_KEY': 'trace-secret', 'LENS_VERSION': BUILD,
}


def test_should_require_the_exact_checkout_revision(monkeypatch, tmp_path):
    monkeypatch.setattr('evals.agent.source_revision', lambda root: BUILD)
    agent = MoyaiAgent.from_env(workspace=tmp_path / 'case', environ=ENV)
    assert agent.version == BUILD
    assert 'model-secret' not in repr(agent)
    assert 'trace-secret' not in repr(agent)
    with pytest.raises(ValueError, match='checked-out Moyai commit'):
        MoyaiAgent.from_env(workspace=tmp_path / 'case', environ={**ENV, 'LENS_VERSION': 'b' * 40})


@pytest.mark.parametrize('missing', ENV)
def test_should_reject_missing_runtime_configuration(monkeypatch, tmp_path, missing):
    monkeypatch.setattr('evals.agent.source_revision', lambda root: BUILD)
    env = {**ENV, missing: ''}
    with pytest.raises(ValueError, match=missing):
        MoyaiAgent.from_env(workspace=tmp_path / 'case', environ=env)


def test_should_reject_nonempty_workspaces(monkeypatch, tmp_path):
    monkeypatch.setattr('evals.agent.source_revision', lambda root: BUILD)
    agent = MoyaiAgent.from_env(workspace=tmp_path, environ=ENV)
    with pytest.raises(FileExistsError):
        agent.run(input='Use the terminal to test a function.')


def test_should_send_secrets_over_stdin_not_environment_or_argv(monkeypatch, tmp_path):
    monkeypatch.setattr('evals.agent.source_revision', lambda root: BUILD)
    monkeypatch.setenv('GITHUB_TOKEN', 'github-secret')
    monkeypatch.setenv('LENS_API_KEY', 'lens-secret')
    monkeypatch.setenv('LITELLM_API_KEY', 'model-secret')
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

        def communicate(self, value, timeout):
            payload = json.loads(value)
            assert payload['model_api_key'] == 'model-secret'
            Path(payload['result']).write_text(json.dumps({'completed': True, 'result': {
                'output': 'verified', 'trace_id': '1' * 32, 'session_id': 'session',
                'agent_version': BUILD, 'model_calls': 1, 'tool_calls': 1,
            }}))

    monkeypatch.setattr('evals.agent.subprocess.Popen', Process)
    result = MoyaiAgent.from_env(workspace=tmp_path / 'case', environ=ENV).run(input='Implement it.')
    assert result.output == 'verified'
    args, options = calls[0]
    assert not {'GITHUB_TOKEN', 'LENS_API_KEY', 'LITELLM_API_KEY'} & options['env'].keys()
    assert all(secret not in repr(args) for secret in ('github-secret', 'lens-secret', 'model-secret'))


@pytest.fixture
def worker(monkeypatch, tmp_path):
    app_factory = agent_worker.create_app
    state = {}
    exported = []

    def receiver(request):
        assert request.headers['authorization'] == 'Bearer trace-secret'
        body = ExportTraceServiceRequest.FromString(request.content)
        for resource in body.resource_spans:
            for scope in resource.scope_spans:
                exported.extend(scope.spans)
        return httpx.Response(200)

    def create_app(settings):
        app = app_factory(settings)
        for outbox in app.state.tracing.outboxes:
            outbox.client = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        state['app'] = app
        return app

    class Harness:
        def __init__(self, **kwargs):
            self.context = kwargs

        def run_conversation(self, prompt, **kwargs):
            import time
            app = state['app']
            store = app.state.store
            run = store.rows('SELECT * FROM runs')[0]
            store.execute('UPDATE runs SET model_calls=1 WHERE id=?', (run['id'],))
            app.state.tracing.model(run, 'request-1', time.time_ns(), [{'role': 'user', 'content': prompt}],
                                    {'choices': [{'message': {'content': 'done'}}]}, 'completed')
            activity = self.context['activity']
            activity.start('tool-1', 'terminal', {'command': 'python solution.py'})
            activity.complete('tool-1', 'terminal', {}, {'exit_code': 0, 'stdout': 'passed'})
            return state.get('result', {'completed': True, 'final_response': 'Tests passed.'})

        def close(self):
            state['closed'] = True

    monkeypatch.setattr(agent_worker, 'create_app', create_app)
    monkeypatch.setattr(agent_worker, 'create_agent', lambda harness, **kwargs: Harness(**kwargs))
    monkeypatch.setattr(agent_worker, 'source_revision', lambda root: BUILD)
    payload = {
        'input': 'Implement and test.', 'workspace': str(tmp_path / 'workspace'),
        'state': str(tmp_path / 'state'), 'model': ENV['AGENT_MODEL'],
        'model_base_url': ENV['LITELLM_API_BASE'], 'model_api_key': ENV['LITELLM_API_KEY'],
        'trace_endpoint': ENV['LITELLM_TRACE_ENDPOINT'], 'trace_api_key': ENV['LITELLM_TRACE_API_KEY'],
        'version': BUILD, 'harness': 'codex', 'timeout': 20, 'max_iterations': 2,
    }
    Path(payload['workspace']).mkdir()
    return payload, state, exported


def test_should_export_linked_agent_model_and_tool_spans_before_returning(worker):
    payload, state, exported = worker
    result = agent_worker.execute(payload)
    assert result['output'] == 'Tests passed.'
    assert result['agent_version'] == BUILD
    assert result['model_calls'] == result['tool_calls'] == 1
    assert state['closed']
    assert len(exported) == 3
    assert {span.trace_id.hex() for span in exported} == {result['trace_id']}
    attrs = [{item.key: item.value.string_value for item in span.attributes} for span in exported]
    assert {item['openinference.span.kind'] for item in attrs} == {'AGENT', 'LLM', 'TOOL'}
    assert all(item['agent.version'] == BUILD and item['deployment.environment'] == 'lens-eval' for item in attrs)
    root = next(span for span, attributes in zip(exported, attrs) if attributes['openinference.span.kind'] == 'AGENT')
    assert all(span.parent_span_id == root.span_id for span in exported if span != root)


@pytest.mark.parametrize('result', [
    {'completed': False, 'final_response': 'incomplete'},
    {'completed': True, 'failed': True, 'final_response': 'failure'},
    {'completed': True, 'interrupted': True, 'final_response': 'interrupted'},
    {'completed': True, 'final_response': ''},
])
def test_should_reject_incomplete_or_empty_outputs_and_close_resources(worker, result):
    payload, state, _ = worker
    state['result'] = result
    with pytest.raises(AgentRunError):
        agent_worker.execute(payload)
    assert state['closed']
    messages = state['app'].state.store.rows("SELECT status FROM messages WHERE role='user'")
    assert messages == [{'status': 'failed'}]


def test_should_fail_when_lens_does_not_acknowledge_spans():
    store = SimpleNamespace(rows=lambda *args: [{'span_id': 'root', 'delivered_at': None, 'last_error': 'HTTP 401'}])
    with pytest.raises(AgentRunError, match='acknowledge'):
        agent_worker.wait_for_traces(store, 'trace', {'root': 'AGENT', 'llm': 'LLM', 'tool': 'TOOL'}, timeout=0)


def test_should_fail_when_a_trace_kind_is_missing():
    with pytest.raises(AgentRunError, match='all required'):
        agent_worker.wait_for_traces(None, 'trace', {'root': 'AGENT', 'tool': 'TOOL'}, timeout=0)


def test_should_reject_uncommitted_production_source(monkeypatch, tmp_path):
    from evals.agent import source_revision
    monkeypatch.setattr('evals.agent.subprocess.run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match='Commit changes'):
        source_revision(tmp_path)


@pytest.mark.parametrize(('name', 'solution'), [
    ('stable-deduplication', 'def stable_unique(values):\n    return list(dict.fromkeys(values))\n'),
    ('merge-intervals', 'def merge_intervals(intervals):\n    result = []\n    for start, end in sorted(intervals):\n        if result and start <= result[-1][1]:\n            result[-1][1] = max(end, result[-1][1])\n        else:\n            result.append([start, end])\n    return result\n'),
    ('iterable-chunking', 'def chunked(items, size):\n    if size <= 0:\n        raise ValueError()\n    items = list(items)\n    return [items[i:i + size] for i in range(0, len(items), size)]\n'),
    ('strict-boolean-parsing', "def parse_bool(value):\n    values = {'true': True, 'yes': True, '1': True, 'false': False, 'no': False, '0': False}\n    try:\n        return values[value.strip().lower()]\n    except KeyError:\n        raise ValueError() from None\n"),
])
def test_should_verify_each_coding_case_and_reject_broken_implementations(tmp_path, name, solution):
    from evals.test_moyai import verification_for, verify_solution
    fixture = Path(__file__).resolve().parents[1] / 'evals' / 'coding_cases.json'
    case = next(case for case in json.loads(fixture.read_text())['cases'] if case['meta']['name'] == name)
    (tmp_path / 'solution.py').write_text(solution)
    code = verification_for(case['input'])
    assert verify_solution(tmp_path, code)['passed']
    (tmp_path / 'solution.py').write_text('raise RuntimeError("broken implementation")\n')
    assert not verify_solution(tmp_path, code)['passed']


def test_should_reject_unrecognized_saved_input():
    from evals.test_moyai import verification_for
    with pytest.raises(ValueError, match='exactly match'):
        verification_for('A prompt that is not in the versioned regression fixture.')


@pytest.mark.parametrize(('name', 'solution'), [
    ('stable-deduplication', 'def stable_unique(values):\n    seen, result = set(), []\n    for value in values:\n        if str(value) not in seen:\n            seen.add(str(value))\n            result.append(value)\n    return result\n'),
    ('merge-intervals', 'def merge_intervals(intervals):\n    result = []\n    for start, end in sorted(intervals):\n        if start == end:\n            continue\n        if result and start <= result[-1][1]:\n            result[-1][1] = max(end, result[-1][1])\n        else:\n            result.append([start, end])\n    return result\n'),
    ('iterable-chunking', 'def chunked(items, size):\n    if size <= 0:\n        raise ValueError()\n    items = list(items)\n    if size == 1:\n        return items\n    return [items[i:i + size] for i in range(0, len(items), size)]\n'),
    ('strict-boolean-parsing', "def parse_bool(value):\n    value = value.strip().lower()\n    if value in {'true', 'yes'}:\n        return True\n    if value in {'false', 'no'}:\n        return False\n    number = float(value)\n    if number not in (0, 1):\n        raise ValueError()\n    return bool(number)\n"),
])
def test_should_reject_contract_defects_that_escaped_the_original_checks(tmp_path, name, solution):
    from evals.test_moyai import verification_for, verify_solution
    fixture = Path(__file__).resolve().parents[1] / 'evals' / 'coding_cases.json'
    case = next(case for case in json.loads(fixture.read_text())['cases'] if case['meta']['name'] == name)
    (tmp_path / 'solution.py').write_text(solution)
    assert not verify_solution(tmp_path, verification_for(case['input']))['passed']


@pytest.fixture
def passing_lens_report(monkeypatch, tmp_path):
    import sys
    from evals import test_moyai as driver

    fixture = Path(__file__).resolve().parents[1] / 'evals' / 'coding_cases.json'
    case = SimpleNamespace(input=json.loads(fixture.read_text())['cases'][0]['input'], followups=[])
    state = {'records': [], 'errors': [], 'events': [], 'solution':
             'def stable_unique(values):\n    return list(dict.fromkeys(values))\n'}

    class Report:
        def write_json(self, path):
            state['events'].append('write_report')
            path.write_text(json.dumps({'gate_passed': True, 'cases': len(state['records'])}))

        def assert_passed(self):
            state['events'].append('remote_passed')

    class Evaluation:
        cases = [case]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def record(self, case, **kwargs):
            state['records'].append(kwargs)

        def record_error(self, case, error):
            state['errors'].append(error)

        def finish(self):
            state['events'].append('finish')
            return Report()

    class Agent:
        def __init__(self, workspace):
            self.workspace = workspace

        def run(self, *, input):
            if state.get('execution_error'):
                raise AgentRunError('Moyai stopped before completing the task or settling all tool calls.')
            self.workspace.mkdir()
            (self.workspace / 'solution.py').write_text(state['solution'])
            return SimpleNamespace(output='The requested task is complete and tests passed.', trace_id='1' * 32)

    monkeypatch.setitem(sys.modules, 'lens', SimpleNamespace(
        Lens=lambda **kwargs: SimpleNamespace(evals=SimpleNamespace(test=lambda *args, **kwargs: Evaluation()))))
    monkeypatch.setattr(driver.MoyaiAgent, 'from_env', lambda *, workspace: Agent(workspace))
    monkeypatch.setattr(driver, 'execution_metadata', lambda: None)
    monkeypatch.setenv('LENS_BASE_URL', 'https://lens.example')
    monkeypatch.setenv('LENS_API_KEY', 'test-key')
    artifact = tmp_path / 'report.json'
    monkeypatch.setenv('LENS_REPORT_PATH', str(artifact))
    return driver, state, artifact


def test_should_fail_failed_independent_checks_even_when_lens_reports_success(passing_lens_report, tmp_path):
    driver, state, artifact = passing_lens_report
    state['solution'] = 'def stable_unique(values):\n    return sorted(set(values))\n'
    with pytest.raises(AssertionError, match='Independent verification failed for 1 coding case'):
        driver.test_moyai(tmp_path)
    assert json.loads(state['records'][0]['output'])['verification']['passed'] is False
    assert state['records'][0]['trace_id'] == '1' * 32
    assert state['events'] == ['finish', 'write_report', 'remote_passed']
    assert json.loads(artifact.read_text())['gate_passed'] is True


def test_should_accept_correct_code_after_recording_results_and_report(passing_lens_report, tmp_path):
    driver, state, artifact = passing_lens_report
    driver.test_moyai(tmp_path)
    assert json.loads(state['records'][0]['output'])['verification']['passed'] is True
    assert state['events'] == ['finish', 'write_report', 'remote_passed']
    assert artifact.is_file()


def test_should_fail_incomplete_execution_even_when_lens_reports_success(passing_lens_report, tmp_path):
    driver, state, artifact = passing_lens_report
    state['execution_error'] = True
    with pytest.raises(AssertionError, match='did not complete; evaluation is inconclusive'):
        driver.test_moyai(tmp_path)
    assert len(state['errors']) == 1
    assert state['records'] == []
    assert state['events'] == ['finish', 'write_report', 'remote_passed']
    assert artifact.is_file()


def test_should_distinguish_baseline_and_candidate_execution_metadata(monkeypatch):
    pytest.importorskip('lens')
    from evals.test_moyai import execution_metadata
    for key, value in {'LENS_EVAL_BRANCH': 'main', 'LENS_EVAL_ROLE': 'baseline', 'LENS_VERSION': BUILD,
                       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                       'GITHUB_REPOSITORY': 'BerriAI/moyai'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('LENS_EVAL_PR', raising=False)
    baseline = execution_metadata()
    assert baseline.branch == 'main' and baseline.pr is None
    assert baseline.version == BUILD
    assert baseline.ci_url == 'https://github.com/BerriAI/moyai/actions/runs/123'
    monkeypatch.setenv('LENS_EVAL_BRANCH', 'feature')
    monkeypatch.setenv('LENS_EVAL_ROLE', 'candidate')
    monkeypatch.setenv('LENS_EVAL_PR', '300')
    candidate = execution_metadata()
    assert candidate.pr == 300 and candidate.branch == 'feature'
    assert candidate.identity != baseline.identity
