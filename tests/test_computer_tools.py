import base64
import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from agent.tools import computer_tools as tools


def call(tmp_path, name, **args):
    return tools.call('computer_'+name, args, tmp_path)


def test_full_large_binary_and_unicode_file_pages(tmp_path):
    raw = ('🗿abc'*100000).encode()
    call(tmp_path, 'write_file', path='long.bin', data_base64=base64.b64encode(raw).decode())
    total = bytearray()
    while True:
        page = call(tmp_path, 'read_file', path='long.bin', offset=len(total), length=7341)
        total.extend(base64.b64decode(page['data_base64']))
        if page['next_offset'] is None:
            break
    assert total == raw
    with pytest.raises(FileExistsError):
        call(tmp_path, 'write_file', path='long.bin', text='replace')
    with pytest.raises(ValueError, match='workspace'):
        call(tmp_path, 'read_file', path='../private')


def test_live_command_yields_accepts_input_and_preserves_complete_output(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'DO-NOT-INHERIT')
    monkeypatch.setenv('OPENAI_API_KEY', 'DO-NOT-INHERIT-EITHER')
    script = "import sys,os; print('ready', flush=True); input(); print('x'*240000); print(os.environ.get('WORKSPACE_RUN_TOKEN', 'no-token')); print(os.environ.get('OPENAI_API_KEY', 'no-key'))"
    command = shlex.quote(sys.executable)+' -c '+shlex.quote(script)
    started = call(tmp_path, 'terminal', command=command, request_key='exact-launch', yield_ms=0)
    assert started['status'] == 'running'
    identity = started['process_id']
    assert call(tmp_path, 'terminal', command=command, request_key='exact-launch', yield_ms=0)['process_id'] == identity
    with pytest.raises(tools.ComputerToolError, match='different command'):
        call(tmp_path, 'terminal', command='echo no', request_key='exact-launch')
    assert call(tmp_path, 'terminal_input', process_id=identity, text='continue\n')['accepted']
    result = call(tmp_path, 'terminal_read', process_id=identity, yield_ms=3000)
    assert result['status'] == 'completed' and result['exit_code'] == 0
    chunks = bytearray()
    while True:
        page = call(tmp_path, 'terminal_read', process_id=identity, offset=len(chunks))
        chunks.extend(base64.b64decode(page['data_base64']))
        if page['next_offset'] is None:
            break
    assert chunks == b'ready\n'+b'x'*240000+b'\nno-token\nno-key\n'
    assert Path(result['output_path']).read_bytes() == chunks
    assert not Path(result['output_path']).is_relative_to(tmp_path)


def test_explicit_stop_and_lost_supervisor_never_relaunch(tmp_path):
    started = call(tmp_path, 'terminal', command='sleep 60', request_key='sleep', yield_ms=0)
    assert call(tmp_path, 'terminal_input', process_id=started['process_id'], stop=True)['accepted']
    result = call(tmp_path, 'terminal_read', process_id=started['process_id'], yield_ms=3000)
    assert result['status'] == 'completed' and result['exit_code'] != 0
    root = tools.directory(tmp_path, started['process_id'])
    (root/'done.json').unlink()
    assert call(tmp_path, 'terminal', command='sleep 60', request_key='sleep', yield_ms=0)['status'] == 'unknown'


def test_invalid_arguments_are_rejected_before_launch_or_file_changes(tmp_path):
    for args in ({'yield_ms': True}, {'length': -1}, {'request_key': ''}):
        with pytest.raises(tools.ComputerToolError):
            call(tmp_path, 'terminal', command='touch should-not-exist', **args)
    assert not (tmp_path/'.moyai-processes').exists()
    with pytest.raises(tools.ComputerToolError):
        call(tmp_path, 'write_file', path='invalid.txt', text='value', overwrite=1)
    assert not (tmp_path/'invalid.txt').exists()


def test_atomic_replacement_preserves_original_on_failure_and_file_mode(tmp_path, monkeypatch):
    target = tmp_path/'script.sh'
    target.write_text('original'); target.chmod(0o750)
    original = os.replace
    def lost_write(*args, **kwargs):
        raise OSError('Synthetic write failure')
    monkeypatch.setattr(os, 'replace', lost_write)
    with pytest.raises(OSError):
        call(tmp_path, 'write_file', path='script.sh', text='replacement', overwrite=True)
    assert target.read_text() == 'original'
    assert not list(tmp_path.glob('.moyai-write-*'))
    monkeypatch.setattr(os, 'replace', original)
    call(tmp_path, 'write_file', path='script.sh', text='replacement', overwrite=True)
    assert target.read_text() == 'replacement' and target.stat().st_mode & 0o777 == 0o750


def test_appending_output_preserves_the_observed_prefix(tmp_path, monkeypatch):
    log = tmp_path/'live.log'
    log.write_bytes(b'first page')
    fstat = os.fstat
    def append_between_observations(fd):
        observed = fstat(fd)
        with log.open('ab') as stream:
            stream.write(b'+later')
        return observed
    monkeypatch.setattr(os, 'fstat', append_between_observations)
    page = tools.read_file({'path': 'live.log'}, tmp_path, append_only=True)
    assert page['text'] == 'first page' and page['size'] == len(b'first page')
    assert log.read_bytes().startswith(base64.b64decode(page['data_base64']))


def test_force_stop_reaches_a_command_ignoring_term(tmp_path):
    script = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
    started = call(tmp_path, 'terminal', command=shlex.join([sys.executable, '-c', script]), yield_ms=0)
    try:
        deadline = time.monotonic()+5
        while 'ready' not in (call(tmp_path, 'terminal_read', process_id=started['process_id'])['output'] or ''):
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert call(tmp_path, 'terminal_input', process_id=started['process_id'], stop=True)['signal'] == 'SIGKILL'
        assert call(tmp_path, 'terminal_read', process_id=started['process_id'], yield_ms=3000)['status'] == 'completed'
    finally:
        try:
            call(tmp_path, 'terminal_input', process_id=started['process_id'], stop=True)
        except tools.ComputerToolError:
            pass


def test_stdin_backpressure_returns_exact_suffix_and_does_not_block_stop(tmp_path):
    started = call(tmp_path, 'terminal', command='sleep 60', yield_ms=0)
    identity = started['process_id']
    raw = ('🗿'*100000).encode()
    try:
        receipt = call(tmp_path, 'terminal_input', process_id=identity,
                       data_base64=base64.b64encode(raw).decode(), eof=True)
        assert not receipt['accepted'] and not receipt['eof_sent']
        assert 0 < receipt['accepted_bytes'] < len(raw)
        assert base64.b64decode(receipt['remaining_data_base64']) == raw[receipt['accepted_bytes']:]
        assert call(tmp_path, 'terminal_input', process_id=identity, stop=True)['accepted']
        assert call(tmp_path, 'terminal_read', process_id=identity, yield_ms=3000)['status'] == 'completed'
    finally:
        try:
            call(tmp_path, 'terminal_input', process_id=identity, stop=True)
        except tools.ComputerToolError:
            pass
