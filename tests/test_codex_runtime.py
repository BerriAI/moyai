"""Exercise process reuse with pinned Codex, real MCP, and scripted inference."""
import json
import os
from pathlib import Path
import tempfile
import time

import pytest

from sandbox.codex_harness import CodexAgent
from sandbox.codex_runtime import RuntimeLease, discard_orphan
from test_codex_tool_readiness import readiness_case
from test_codex_tool_search import search_case
from test_workspace import workspace as broker_workspace  # noqa: F401


@pytest.fixture
def runtime_root():
    # Unix control socket paths have a small platform-dependent limit.
    with tempfile.TemporaryDirectory(prefix='moyai-warm-', dir='/tmp') as directory:
        yield Path(directory)


def reuse_case(tmp_path, monkeypatch, root, *, scope='same-session', progress=lambda value: None, **options):
    proof = {}

    class WarmAgent(CodexAgent):
        def __init__(self, **kwargs):
            self.lease = RuntimeLease(scope, 5, root=root)
            kwargs['relay'].codex_runtime = self.lease
            super().__init__(**kwargs)

        def close(self):
            proof.update(self.lease.info or {})
            proof['clean'] = self.lease.clean
            super().close()
            self.lease.close()

    started = time.monotonic()
    result, requests, outcome = readiness_case(tmp_path, monkeypatch, delay=0,
        agent_class=WarmAgent, progress=progress, **options)
    proof.update(result)
    proof['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
    return proof, requests, outcome


def test_completed_runs_reuse_process_but_not_thread_credentials_or_history(tmp_path, monkeypatch, runtime_root):
    first, requests, outcome = reuse_case(tmp_path, monkeypatch, runtime_root)
    assert first['completed'] and first['clean'], outcome
    second, next_requests, outcome = reuse_case(tmp_path, monkeypatch, runtime_root,
        capability='rotated-capability', tool_name='fresh_echo')
    assert second['completed'] and second['clean'], outcome
    assert first['pid'] == second['pid'] and second['reused'] and not first['reused']
    assert first['model_started_after_catalog'] and second['model_started_after_catalog']
    assert first['tool_calls'] == ['echo'] and second['tool_calls'] == ['fresh_echo']
    # A new thread must not carry the earlier tool call/output into its request.
    assert 'ready-before-inference' not in json.dumps(next_requests[0]['body']['input'])
    assert 'ready-before-inference' in json.dumps(requests[-1]['body']['input'])


def test_new_scope_restarts_runtime(tmp_path, monkeypatch, runtime_root):
    first, _, _ = reuse_case(tmp_path, monkeypatch, runtime_root)
    second, _, result = reuse_case(tmp_path, monkeypatch, runtime_root, scope='different-requester')
    assert second['completed'] and second['clean'], result
    assert second['pid'] != first['pid'] and not second['reused']


def test_idle_expiry_and_unclean_release_discard_runtime(runtime_root):
    lease = RuntimeLease('session', .05, root=runtime_root)
    first = lease.ready()
    assert first
    lease.close()  # No successful cleanup receipt.
    second = RuntimeLease('session', .05, root=runtime_root)
    info = second.ready()
    assert info and info['pid'] != first['pid'] and not info['reused']
    second.clean = True  # No thread was ever started.
    second.close()
    deadline = time.monotonic() + 8
    while (runtime_root / 'lease.sock').exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert not (runtime_root / 'lease.sock').exists()
    assert not (runtime_root / 'home').exists()


def test_concurrent_lease_falls_back_without_disrupting_owner(runtime_root):
    first = RuntimeLease('session', .1, root=runtime_root)
    info = first.ready()
    try:
        second = RuntimeLease('session', .1, root=runtime_root)
        try:
            assert second.ready() is None
        finally:
            second.close()
        os.kill(info['pid'], 0)
    finally:
        first.close()


def test_owner_disconnect_discards_even_a_previously_clean_runtime(runtime_root):
    owner = RuntimeLease('session', .1, root=runtime_root)
    info = owner.ready()
    assert info
    owner.clean = True
    owner.info = None  # Simulate EOF without sending a clean-release message.
    owner.close()
    replacement = RuntimeLease('session', .1, root=runtime_root)
    try:
        next_info = replacement.ready()
        assert next_info and next_info['pid'] != info['pid'] and not next_info['reused']
    finally:
        replacement.close()


def test_unavailable_lease_keeps_cold_execution_working(tmp_path, monkeypatch, runtime_root):
    blocked = runtime_root / 'not-a-directory'
    blocked.touch()
    proof, requests, result = reuse_case(tmp_path, monkeypatch, blocked)
    assert proof['completed'] and proof['model_started_after_catalog'], result
    assert 'pid' not in proof and len(requests) == 2 and proof['tool_calls'] == ['echo']


def test_failed_required_mcp_discards_warm_process_without_inference(tmp_path, monkeypatch, runtime_root):
    failed, requests, result = reuse_case(tmp_path, monkeypatch, runtime_root, fail_tools=True)
    assert not failed['completed'] and not failed['clean'] and result['failed']
    assert not requests and not failed['tool_calls']
    recovered, _, result = reuse_case(tmp_path, monkeypatch, runtime_root)
    assert recovered['completed'] and recovered['pid'] != failed['pid'], result


def test_prewarm_does_not_inherit_provider_or_broker_secrets(monkeypatch):
    from sandbox.codex_runtime import clean_env
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'private-capability')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-provider-key')
    monkeypatch.setenv('CODEX_HOME', '/untrusted')
    assert not {'WORKSPACE_RUN_TOKEN', 'OPENAI_API_KEY', 'CODEX_HOME'} & clean_env().keys()


def test_snapshot_cleanup_removes_orphans_but_preserves_live_owner(runtime_root):
    home = runtime_root / 'home'
    home.mkdir()
    (home / 'private-native-state').write_text('cloned state')
    discard_orphan(runtime_root)
    assert not home.exists()
    owner = RuntimeLease('session', .1, root=runtime_root)
    try:
        info = owner.ready()
        assert info
        discard_orphan(runtime_root)
        assert home.exists()
        os.kill(info['pid'], 0)
    finally:
        owner.close()


def test_reused_runtime_native_search_and_real_broker(tmp_path, monkeypatch, runtime_root, broker_workspace):
    info = []

    class WarmAgent(CodexAgent):
        def __init__(self, **kwargs):
            self.lease = RuntimeLease('search-session', 5, root=runtime_root)
            kwargs['relay'].codex_runtime = self.lease
            super().__init__(**kwargs)

        def close(self):
            info.append({**self.lease.info, 'clean': self.lease.clean})
            super().close()
            self.lease.close()

    for _ in range(2):
        with monkeypatch.context() as turn_patch:
            proof, _, _ = search_case(tmp_path, turn_patch, broker_workspace, agent_class=WarmAgent)
        assert all(proof.values())
    assert info[1]['reused'] and info[0]['pid'] == info[1]['pid']
    assert all(value['clean'] for value in info)
