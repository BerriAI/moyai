import json
import re
from pathlib import Path

import pytest

from agent.context_views import current_view, history_view, size


def saved(view):
    return Path(re.search(r'(/[^\s]+\.jsonl)', view[0]['content'])[1].rstrip(','))


def test_complete_long_history_is_retrievable_at_a_model_sized_view(tmp_path):
    history = [{'role': 'user', 'content': 'Keep the original task. Do not deploy.'},
               {'role': 'assistant', 'tool_calls': [{'id': 'call', 'function': {'name': 'read', 'arguments': '{}'}}]},
               {'role': 'tool', 'tool_call_id': 'call', 'content': '日志🔎'*100000},
               {'role': 'user', 'content': 'Preserve keyboard access.'}]
    view = history_view(history, directory=tmp_path, budget=4000)
    assert size(view) <= 4000
    assert [json.loads(line) for line in saved(view).read_text().splitlines()] == history
    assert history[0] in view and history[-1] in view
    assert any(m.get('tool_call_id') == 'call' and 'line 3' in m['content'] for m in view)
    assert '日志🔎'*100000 == history[2]['content']


def test_references_are_immutable_and_native_seeds_can_read_old_revisions(tmp_path):
    first = [{'role': 'user', 'content': 'first'*10000}]
    earlier = history_view(first, directory=tmp_path, budget=3000)
    old = saved(earlier)
    newer = history_view([*first, {'role': 'assistant', 'content': 'second'*10000}], directory=tmp_path, budget=3000)
    assert saved(newer) != old
    assert json.loads(old.read_text()) == first[0]
    assert saved(history_view(first, directory=tmp_path, budget=3000)) == old


def test_oversized_current_request_is_saved_and_requires_reading_before_action(tmp_path):
    prompt = 'Complete requirement: 🗿\n'*100000
    result = current_view(prompt, directory=tmp_path, budget=5000)
    assert len(result.encode()) < 6000 and 'Read the saved user record before acting' in result
    record = json.loads(next(tmp_path.glob('*.jsonl')).read_text())
    assert record == {'role': 'user', 'content': prompt}


def test_references_scrub_private_payloads_and_reject_modified_or_symlinked_files(tmp_path):
    history = [{'role': 'assistant', 'tool_calls': [{'id': 'memory', 'function': {
        'name': 'mcp__moyai__memory_save', 'arguments': '{"content":"private marker"}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'public'*10000}]
    view = history_view(history, directory=tmp_path, budget=3000)
    path = saved(view)
    assert 'private marker' not in path.read_text()
    path.write_text('modified')
    with pytest.raises(ValueError, match='changed'):
        history_view(history, directory=tmp_path, budget=3000)
    path.unlink()
    outside = tmp_path/'outside'; outside.write_text('keep')
    path.symlink_to(outside)
    with pytest.raises(OSError):
        history_view(history, directory=tmp_path, budget=3000)
    assert outside.read_text() == 'keep'


def test_fresh_child_discards_inherited_parent_reference_files(tmp_path):
    from agent.context_store import open_context
    view = history_view([{'role': 'user', 'content': 'Parent conversation '*10000}], directory=tmp_path, budget=3000)
    original = saved(view)
    unrelated = tmp_path/'project-note.txt'; unrelated.write_text('workspace evidence')
    store = open_context(tmp_path, {'run_id': 'child', 'fresh_child': True})
    try:
        assert not original.exists() and store.history() == []
        assert unrelated.read_text() == 'workspace evidence'
    finally:
        store.close()
