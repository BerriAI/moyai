import json
import os
import shlex
import sys

import pytest

from sandbox import credential_tools

REQUEST = 'a' * 32
SECRET = 'synthetic-credential-for-execution'


def command(source):
    return shlex.join([sys.executable, '-c', source])


def fixture(monkeypatch, *, format='env', value=None, status='ready'):
    monkeypatch.setattr(credential_tools, 'ensure_tools', lambda command: None)
    calls = []
    binding = {'request_id': REQUEST, 'revision': 3, 'format': format, 'env_var': 'KUBECONFIG',
               'value': value if value is not None else json.dumps({'TEST_ACCESS_KEY': SECRET})}
    def broker(path, body):
        calls.append((path, body))
        if path == '/credentials/materialize':
            return {'status': status, 'bindings': [dict(binding)]} if status == 'ready' else {
                'status': status, 'moyai_wait_credential': REQUEST}
        return {'status': 'pending', 'moyai_wait_credential': REQUEST}
    return broker, calls


def test_command_uses_env_and_redacts_before_return_without_changing_agent_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'synthetic-platform-capability')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-platform-capability')
    broker, calls = fixture(monkeypatch)
    result = credential_tools.run({'request_ids': [REQUEST], 'command': command(
        "import os,json,base64; value=os.environ['TEST_ACCESS_KEY']; "
        "assert 'WORKSPACE_RUN_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ; "
        "print(json.dumps({'value':value})); print(base64.b64encode(value.encode()).decode())")}, broker)
    assert result['exit_code'] == 0 and SECRET not in str(result)
    assert result['output'].count('[credential redacted]') == 2
    assert result['credentials'] == [{'request_id': REQUEST, 'revision': 3}]
    assert 'TEST_ACCESS_KEY' not in os.environ and list(tmp_path.iterdir()) == []
    assert calls == [('/credentials/materialize', {'request_ids': [REQUEST]})]


def test_pending_access_and_invalid_arguments_never_execute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    broker, calls = fixture(monkeypatch, status='pending')
    args = {'request_ids': [REQUEST], 'command': 'touch must-not-exist'}
    assert credential_tools.run(args, broker)['status'] == 'pending'
    for change in [{'request_ids': [{}]}, {'request_ids': [REQUEST, REQUEST]}, {'timeout': True},
                   {'timeout': 601}, {'value': SECRET}]:
        assert 'error' in credential_tools.run({**args, **change}, broker)
    assert list(tmp_path.iterdir()) == [] and len(calls) == 1


@pytest.mark.parametrize('message,kind', [
    ('ExpiredToken: session token has expired', 'expired'),
    ('InvalidClientTokenId', 'invalid'), ('Forbidden: insufficient_scope', 'permission'),
    ('Network unreachable', 'unknown'), ('Rate limit exceeded', 'unknown'),
])
def test_failure_classification_recovery_and_no_command_replay(monkeypatch, message, kind):
    broker, calls = fixture(monkeypatch)
    result = credential_tools.run({'request_ids': [REQUEST],
        'command': command(f'import sys; print({message!r}); sys.exit(1)')}, broker)
    assert result['failure'] == kind and result['exit_code'] == 1
    if kind != 'unknown':
        assert calls[-1] == ('/tools/call', {'name': 'credentials_report_failure', 'arguments': {
            'request_id': REQUEST, 'revision': 3, 'failure': kind}})
        assert result['moyai_wait_credential'] == REQUEST
    else:
        assert len(calls) == 1


def test_timeout_and_output_limit_stop_process_and_hide_partial_secret(monkeypatch):
    broker, calls = fixture(monkeypatch)
    result = credential_tools.run({'request_ids': [REQUEST], 'timeout': 1,
                                   'command': command('import time; time.sleep(30)')}, broker)
    assert result['timed_out'] and result['exit_code'] != 0 and len(calls) == 1
    monkeypatch.setattr(credential_tools, 'MAX_OUTPUT', 32)
    result = credential_tools.run({'request_ids': [REQUEST], 'command': command(
        "import os; print('x'*25+os.environ['TEST_ACCESS_KEY']+'y'*50000)")}, broker)
    assert result['output_limited'] and SECRET[:8] not in result['output']
    assert credential_tools.redact('prefix ' + SECRET[:20], [SECRET]) == 'prefix [credential redacted]'


@pytest.mark.skipif(not hasattr(os, 'memfd_create'), reason='Production credential files use Linux memfd')
def test_multiline_file_is_memory_only_0600_and_descriptor_closed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    value = 'apiVersion: v1\nusers:\n- user:\n    token: ' + SECRET + '\n'
    broker, calls = fixture(monkeypatch, format='file', value=value)
    result = credential_tools.run({'request_ids': [REQUEST], 'command': command(
        "import os,stat; path=os.environ['KUBECONFIG']; "
        "assert stat.S_IMODE(os.stat(path).st_mode)==0o600; "
        "print(open(path).read()); print('fd='+path)")}, broker)
    assert result['exit_code'] == 0 and SECRET not in str(result)
    fd = int(result['output'].split('fd=/proc/self/fd/')[1].strip())
    with pytest.raises(OSError):
        os.fstat(fd)
    assert list(tmp_path.iterdir()) == [] and len(calls) == 1
