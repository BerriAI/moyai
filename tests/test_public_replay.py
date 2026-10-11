import json

from agent.harnesses.replay import codex_items


def test_codex_replay_preserves_roles_phases_receipts_and_long_call_ids():
    identity = 'journal-namespace:' + 'c' * 80
    messages = [
        {'role': 'user', 'content': 'Original constraint: do not deploy.'},
        {'role': 'assistant', 'content': 'I will check CI.', 'phase': 'commentary'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identity, 'function': {
            'name': 'github_ci', 'arguments': '{"pr":127}'}}]},
        {'role': 'tool', 'tool_call_id': identity, 'content': 'Full evidence ' + '界' * 50000},
        {'role': 'assistant', 'content': 'CI passed. PR is ready.', 'phase': 'final_answer'},
        {'role': 'user', 'content': 'What did you verify?'},
    ]
    items = codex_items(messages)
    assert [i['type'] for i in items] == ['message', 'message', 'function_call', 'function_call_output', 'message', 'message']
    assert items[0]['role'] == 'user'
    assert items[1]['role'] == 'assistant' and items[1]['phase'] == 'commentary'
    assert items[4]['phase'] == 'final_answer'
    assert items[2]['call_id'] == items[3]['call_id'] and len(items[2]['call_id']) == 64
    assert items[3]['output'] == messages[3]['content']
    assert items[5]['role'] == 'user'


def test_unknown_outcomes_are_never_fabricated_or_replayed_as_live_calls():
    items = codex_items([
        {'role': 'assistant', 'tool_calls': [{'id': 'unsettled', 'function': {'name': 'publish', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'outside-summary', 'content': 'Historical result'},
    ])
    assert all(i['type'] == 'message' for i in items)
    assert 'UNKNOWN outcome' in json.dumps(items)
    assert 'Historical result' in json.dumps(items)
