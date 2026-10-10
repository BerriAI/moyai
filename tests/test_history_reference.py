import json
from copy import deepcopy

import pytest

from agent.harnesses.harness_agent import TurnJournal
from agent.history_reference import HISTORY_BYTES, history_prompt


def reference(prompt):
    return prompt.rsplit('\n\nCURRENT REQUEST:\n', 1)[0]


def saved_history(tmp_path):
    return [json.loads(line) for line in (tmp_path / '.moyai-history.jsonl').read_text().splitlines()]


def test_small_history_and_current_request_remain_verbatim(tmp_path):
    history = [{'role': 'user', 'content': 'Keep keyboard navigation.'},
               {'role': 'assistant', 'content': 'Done.'}]
    current = 'Add search.\nKeep the existing selection.'
    assert history_prompt(current, [], cwd=tmp_path) == current
    prompt = history_prompt(current, history, cwd=tmp_path)
    assert json.dumps(history, ensure_ascii=False) in prompt
    assert prompt.endswith('\n\nCURRENT REQUEST:\n' + current)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('output', ['log line\n' * 40_000, '日志🔎\x00"\\' * 40_000,
                                  [{'type': 'image', 'data': 'A' * 400_000}]])
def test_large_tool_output_is_retrievable_without_growing_the_prompt(tmp_path, output):
    history = [{'role': 'user', 'content': 'Build a searchable model picker.'},
               {'role': 'assistant', 'tool_calls': [{'id': 'read1', 'function': {
                   'name': 'Read', 'arguments': '{"file_path":"build.log"}'}}]},
               {'role': 'tool', 'tool_call_id': 'read1', 'content': output},
               {'role': 'user', 'content': 'Do not publish it yet.'}]
    original = deepcopy(history)
    journal = TurnJournal(history, 'Continue the saved task.')
    prompt = journal.prompt('Continue the saved task.', history, cwd=tmp_path)
    assert len(reference(prompt).encode()) <= HISTORY_BYTES
    assert 'Build a searchable model picker.' in prompt
    assert 'Do not publish it yet.' in prompt
    assert 'History line 3' in prompt
    assert 'do not load the entire history' in prompt
    assert saved_history(tmp_path) == original
    assert history == original
    assert journal.messages == [*original, {'role': 'user', 'content': 'Continue the saved task.'}]
    assert (tmp_path / '.moyai-history.jsonl').stat().st_mode & 0o777 == 0o600


def test_many_rounds_retain_original_request_latest_correction_and_recent_receipts(tmp_path):
    history = [{'role': 'user', 'content': 'Original task: add model search.'},
               {'role': 'user', 'content': 'Latest correction: keep Escape support.'}]
    for i in range(500):
        history.extend([
            {'role': 'assistant', 'tool_calls': [{'id': f'call{i}', 'function': {
                'name': 'github_create_pull_request', 'arguments': json.dumps({'body': 'detail' * 1000})}}]},
            {'role': 'tool', 'tool_call_id': f'call{i}',
             'content': json.dumps({'number': i, 'url': f'https://example.test/pr/{i}'})}])
    prompt = history_prompt('Continue without repeating writes.', history, cwd=tmp_path)
    assert len(reference(prompt).encode()) <= HISTORY_BYTES
    assert 'Original task: add model search.' in prompt
    assert 'Latest correction: keep Escape support.' in prompt
    assert 'github_create_pull_request' in prompt and 'https://example.test/pr/499' in prompt
    assert 'Missing text does not mean an action was not performed' in prompt
    assert saved_history(tmp_path) == history


def test_repeated_checkpoints_keep_raw_receipts_without_nesting_prompt_references(tmp_path):
    history = [{'role': 'user', 'content': 'Original request'},
               {'role': 'tool', 'tool_call_id': 'old', 'content': 'verbose' * 30_000}]
    for index in range(4):
        journal = TurnJournal(history, f'Continue {index}')
        prompt = journal.prompt(f'Continue {index}', history, cwd=tmp_path)
        assert prompt.count('SAVED CONVERSATION REFERENCE:') == 1
        assert len(reference(prompt).encode()) <= HISTORY_BYTES
        assert saved_history(tmp_path) == history
        journal.tool_started(f'check{index}', 'Read', {'file_path': 'proof.txt'})
        journal.tool_finished(f'check{index}', 'Already created')
        history = journal.messages
    assert len(history) == 14
    assert len(list(tmp_path.iterdir())) == 1


def test_reference_uses_scrubbed_history_and_does_not_follow_an_existing_symlink(tmp_path):
    outside = tmp_path / 'untouched.txt'
    outside.write_text('do not overwrite')
    (tmp_path / '.moyai-history.jsonl').symlink_to(outside)
    history = [
        {'role': 'assistant', 'tool_calls': [{'id': 'memory', 'function': {
            'name': 'mcp__moyai__memory_save', 'arguments': '{"content":"private marker"}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'safe output' * 20_000}]
    prompt = history_prompt('Continue.', history, cwd=tmp_path)
    assert 'private marker' not in prompt
    assert 'private marker' not in json.dumps(saved_history(tmp_path))
    assert outside.read_text() == 'do not overwrite'
    assert not (tmp_path / '.moyai-history.jsonl').is_symlink()
