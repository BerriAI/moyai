import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import agent as lifecycle
from sandbox.context_store import ContextStore, ContextUnavailable, open_context
from sandbox.harness_agent import TurnJournal
from test_runner import runner


def test_runner_uses_checkpoint_without_serializing_all_chat_again(runner, monkeypatch):
    run = runner.store.create_run('task', '', 'modal', [], chat_enabled=True, harness='claude-agent-sdk')
    runner.store.update_run(run['id'], snapshot_id='saved-filesystem')
    monkeypatch.setattr(runner.store, 'messages', lambda *a: pytest.fail('Loaded entire chat on checkpoint resume'))
    spec = runner.spec(runner.store.run(run['id']))
    assert spec['context_checkpoint'] and spec['history_fallback'] == []


def test_missing_checkpoint_context_stops_instead_of_forgetting_history(tmp_path):
    with pytest.raises(ContextUnavailable, match='missing its saved conversation'):
        open_context(tmp_path, {'run_id': 'run', 'context_checkpoint': True})
    assert not (tmp_path / 'context.sqlite3').exists()


@pytest.mark.parametrize('harness', ['claude-agent-sdk', 'codex', 'opencode', 'deepagents', 'tool-loop'])
def test_durable_harness_starts_new_turn_with_unknown_tool_outcome(tmp_path, monkeypatch, harness):
    from sandbox.harness_registry import create_agent
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'tool_calls': [{'id': 'old', 'function': {'name': 'Edit'}}]}])
    agent = create_agent(harness, spec={}, relay=SimpleNamespace(compact=lambda *_: 'Summary'),
                         config={}, activity=None, step=lambda: None, cwd=str(tmp_path), context_store=store)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    async def respond(prompt, system_message):
        return prompt
    monkeypatch.setattr(agent, '_run', respond)
    try:
        prompt = agent.run_conversation('What happened?', conversation_history=[], system_message='Investigate')
        assert 'UNRESOLVED TOOL OUTCOMES' in prompt and 'CURRENT REQUEST:\nWhat happened?' in prompt
        assert store.pending == {'old'} and not agent.journal.pending
    finally:
        agent.close()
        store.close()


@pytest.mark.parametrize('summary_failure', [False, True])
def test_production_lifecycle_restores_store_and_saves_only_new_events(tmp_path, monkeypatch, summary_failure):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'cap')
    real_path = Path
    monkeypatch.setattr(lifecycle, 'Path', lambda p: real_path(tmp_path / str(p).lstrip('/')) if str(p).startswith('/') else real_path(p))
    monkeypatch.setattr(lifecycle, 'prepare_attachments', lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, 'prepare_project', lambda *a: None)
    monkeypatch.setattr(lifecycle, 'computer_request', lambda *a, **k: {})
    monkeypatch.setattr(lifecycle, 'collect_archive', lambda *a: None)
    class Steering:
        requested = False
        message_id = None
        def __init__(self, *a, **kw): pass
        def listen(self, *a): pass
        def close(self): pass
        def can_continue(self, *a): return False
    monkeypatch.setattr(lifecycle, 'ActiveTurnSteering', Steering)
    monkeypatch.setattr('sandbox.continuation.AgentSteer', Steering)
    events, prompts, actions = [], [], []
    monkeypatch.setattr(lifecycle, 'emit', lambda kind, message, *a, **kw: events.append((kind, message, kw)))
    maintenance = []
    def maintain(snapshot, ack):
        maintenance.append(snapshot)
        if summary_failure: raise TimeoutError('untrusted provider error')
        return None
    relay = SimpleNamespace(url='http://relay.test', maintain=maintain,
        compact=lambda *a: pytest.fail('Reply must not wait for compaction'), last_error='', wait_group='', wait_credential='')
    class Adapter:
        compaction_window = 128_000
        def __init__(self, context_store): self.store = context_store
        def validate(self): pass
        def close(self): pass
        def run_conversation(self, prompt, *, conversation_history, system_message):
            self.store.maintain(relay, input_budget=self.compaction_window)
            history = self.store.history()
            journal = TurnJournal(history, prompt, self.store)
            prompts.append(journal.prompt(prompt, history, cwd=tmp_path))
            number = len(actions)
            journal.tool_started(f'call{number}', 'echo', {'number': number})
            actions.append(number)
            journal.tool_finished(f'call{number}', f'receipt-{number}')
            journal.finish('Done')
            return {'completed': True, 'messages': journal.messages, 'final_response': 'Done'}
    monkeypatch.setattr('sandbox.harness_registry.create_agent', lambda *a, **kw: Adapter(kw['context_store']))
    session = tmp_path / 'session'
    session.mkdir()
    legacy = [{'role': 'user', 'content': 'Keep Escape support; do not deploy.'},
              {'role': 'assistant', 'tool_calls': [{'id': 'call0', 'function': {'name': 'Edit', 'arguments': '{}'}}]},
              *[{'role': 'assistant', 'content': 'old log' * 1000} for i in range(80)]]
    legacy_path = session / 'conversation.json'
    legacy_path.write_text(json.dumps(legacy))
    original = legacy_path.read_bytes()
    spec = {'run_id': 'run', 'prompt': 'Continue', 'repo_url': '', 'model': 'configured-model', 'broker_url': 'http://relay.test',
            'harness': 'claude-agent-sdk', 'chat_enabled': True}
    assert lifecycle.run_agent(spec, relay) == 0
    assert len(maintenance) == 2 and all(snapshot for snapshot in maintenance)
    assert legacy_path.read_bytes() == original
    store = ContextStore(session / 'context.sqlite3', 'run')
    count = store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
    assert count == len(legacy) + 4
    store.close()
    # Corrupt the obsolete legacy file: the resumed production path must ignore it.
    legacy_path.write_text('must never be read again')
    assert lifecycle.run_agent({**spec, 'continuation': True, 'context_checkpoint': True}, relay) == 0
    assert actions == [0, 1]
    assert 'receipt-0' in prompts[1]
    assert all('Keep Escape support' in prompt and 'old log' in prompt for prompt in prompts)
    assert all('UNRESOLVED TOOL OUTCOMES' in prompt for prompt in prompts)
    store = ContextStore(session / 'context.sqlite3', 'run')
    assert store.pending == {'call0'}
    assert store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == count + 4
    assert 'SAVED WORKING CONTEXT' not in ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
    store.close()


@pytest.mark.parametrize('harness', ['claude-agent-sdk', 'codex', 'opencode', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('outage', [False, True])
def test_every_durable_harness_answers_with_full_tail_while_maintenance_pending(tmp_path, monkeypatch, harness, outage):
    from sandbox.harness_registry import create_agent
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'content': f'receipt-{i:03d} ' + 'log ' * 500} for i in range(60)])
    submitted = []
    def maintain(snapshot, ack):
        submitted.append(snapshot)
        if outage:
            raise TimeoutError('summary service unavailable')
        return {'id': 'pending', 'snapshot': snapshot, 'status': 'running'}
    relay = SimpleNamespace(maintain=maintain, context_window=lambda: {'input_budget': 100_000},
                            compact=lambda *a, **k: pytest.fail('Summary inference blocked the reply'))
    agent = create_agent(harness, spec={}, relay=relay, config={}, activity=None,
                         step=lambda: None, cwd=str(tmp_path), context_store=store)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    async def respond(prompt, system_message):
        assert 'receipt-000' in prompt and 'receipt-059' in prompt
        agent.journal.finish('Quick TLDR')
        return {'completed': True, 'final_response': 'Quick TLDR'}
    monkeypatch.setattr(agent, '_run', respond)
    try:
        result = agent.run_conversation('tldr', conversation_history=[], system_message='Answer')
        assert result['completed'] and len(submitted) == 1 and submitted[0]
        assert store.state()['cursor'] == 0 and 'Quick TLDR' in store.history()[0]['content']
    finally:
        agent.close()
        store.close()
