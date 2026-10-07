import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from sandbox.broker_relay import BrokerRelay
from sandbox.context_recovery import run_with_context_recovery
from sandbox.context_store import ContextStore, ContextUnavailable
from sandbox.harness_agent import TurnJournal


def runtime(tmp_path):
    store = ContextStore(tmp_path / 'context.db', 'run')
    store.initialize([])
    summaries = []
    def compact(summary, entries, **kwargs):
        summaries.append(entries)
        return {'summary': 'Do not deploy. Completed receipts remain in the journal.',
                'through_seq': entries[-1]['seq']}
    relay = SimpleNamespace(context_required=None, compact=compact)
    agent = SimpleNamespace(context=SimpleNamespace(relay=relay, spec={}, cwd=str(tmp_path),
        activity=SimpleNamespace(commentary=lambda text: None)), context_store=store,
        stopped=threading.Event(), pending_text=[], journal=TurnJournal([], 'Do not deploy.', store))
    return agent, summaries


def test_long_task_can_compact_repeatedly_without_replaying_tools(tmp_path):
    agent, summaries = runtime(tmp_path)
    calls = []
    prompts = []
    def invoke(prompt):
        prompts.append(prompt)
        assert 'Do not deploy.' in prompt
        assert len(prompt.encode()) < 16000
        if len(calls) == 30:
            return {'completed': True, 'final_response': 'done'}
        index = len(calls)
        agent.journal.tool_started(str(index), 'write', {'number': index})
        calls.append(index)
        agent.journal.tool_finished(str(index), 'receipt ' + str(index) + 'x' * 25000)
        agent.context.relay.context_required = {'input_tokens': 30000, 'input_budget': 16000}
        return {'failed': True}
    try:
        assert run_with_context_recovery(agent, 'Do not deploy.', [], invoke)['completed']
        assert calls == list(range(30))
        assert len(summaries) >= 30 and len(prompts) == 31
        assert agent.context_store.state()['cursor'] == 61
        # No duplicated current requests or synthetic recovery errors in receipts.
        rows = agent.context_store.db.execute('SELECT message FROM journal').fetchall()
        assert len(rows) == 61 and sum(json.loads(r[0])['role'] == 'user' for r in rows) == 1
    finally:
        agent.context_store.close()


def test_immutable_oversized_input_stops_when_rebuild_does_not_reduce_request(tmp_path):
    agent, summaries = runtime(tmp_path)
    attempts = []
    def invoke(prompt):
        attempts.append(1)
        agent.pending_text.append('SDK ERROR: prompt too long')
        agent.context.relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
        raise RuntimeError('SDK rejected context')
    try:
        with pytest.raises(ContextUnavailable, match='still exceed'):
            run_with_context_recovery(agent, 'Required input ' * 2000, [], invoke)
        assert len(attempts) == 2 and len(summaries) == 1
        assert agent.context_store.state()['cursor'] == 1
        assert 'SDK ERROR' not in agent.context_store.history()[0]['content']
    finally:
        agent.context_store.close()


@pytest.mark.parametrize('pending,stopped', [(True, False), (False, True)])
def test_pending_tool_or_user_stop_blocks_automatic_restart(tmp_path, pending, stopped):
    agent, summaries = runtime(tmp_path)
    def invoke(prompt):
        if pending:
            agent.journal.tool_started('unknown', 'publish', {})
        if stopped:
            agent.stopped.set()
        agent.context.relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
        return {'interrupted': True}
    try:
        if pending:
            with pytest.raises(ContextUnavailable, match='pending'):
                run_with_context_recovery(agent, 'task', [], invoke)
        else:
            assert run_with_context_recovery(agent, 'task', [], invoke)['interrupted']
        assert summaries == []
    finally:
        agent.context_store.close()


def test_failed_compaction_keeps_cursor_and_receipts_across_cold_restore(tmp_path):
    agent, _ = runtime(tmp_path)
    agent.journal.tool_started('one', 'write', {})
    agent.journal.tool_finished('one', 'durable receipt')
    def interrupted(summary, entries, **kwargs):
        raise RuntimeError('process failed before commit')
    try:
        with pytest.raises(ContextUnavailable):
            agent.context_store.compact(interrupted, force=True)
    finally:
        agent.context_store.close()
    restored = ContextStore(tmp_path / 'context.db', 'run')
    assert restored.state()['cursor'] == 0 and not restored.pending
    assert 'durable receipt' in restored.history()[0]['content']
    restored.close()


@pytest.mark.parametrize('managed', [True, False], ids=['fresh-session-adapter', 'hermes-native'])
def test_relay_preserves_context_signal_and_never_replays_rejected_request(managed):
    seen = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers['Content-Length'])))
            raw = json.dumps({'detail': {'code': 'context_compaction_required',
                'input_tokens': 20000, 'input_budget': 10000}}).encode()
            self.send_response(409)
            self.send_header('X-Moyai-Context', 'compact')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{edge.server_port}/broker/run', 'cap').start()
    relay.context_recovery = managed
    try:
        with httpx.Client(base_url=relay.url) as client:
            response = client.post('/v1/chat/completions', json={'messages': []}, headers={'Authorization': 'Bearer cap'})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'context_length_exceeded'
            assert bool(relay.context_required) == managed
            assert not relay.last_error and len(seen) == 1
            if managed:
                client.post('/v1/chat/completions', json={'messages': []}, headers={'Authorization': 'Bearer cap'})
                assert len(seen) == 1
    finally:
        relay.close()
        edge.shutdown()
        edge.server_close()
