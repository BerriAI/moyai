"""Typed public replay for native APIs; never import private provider reasoning.

QM uses the same Responses item contract (thread/inject_items). Tool calls are
replayed only with real receipts. Unknown outcomes stay explicit reference
material rather than fabricating a successful tool response.
"""
import hashlib
import json


def call_id(value):
    value = str(value)
    return value if len(value) <= 64 else hashlib.sha256(value.encode()).hexdigest()


def codex_items(messages):
    calls = {c['id'] for m in messages for c in m.get('tool_calls', [])}
    results = {m.get('tool_call_id') for m in messages if m.get('role') == 'tool'}
    paired = calls & results
    items = []
    for message in messages:
        role, content = message.get('role'), message.get('content')
        if role == 'tool':
            if message.get('tool_call_id') in paired:
                items.append({'type': 'function_call_output', 'call_id': call_id(message['tool_call_id']),
                              'output': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            else:
                items.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text',
                    'text': 'Historical tool receipt (its call is outside this replay window): ' + json.dumps(message, ensure_ascii=False)}]})
            continue
        if content:
            parts = ([{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': content}]
                     if isinstance(content, str) else content)
            item = {'type': 'message', 'role': role, 'content': parts}
            if role == 'assistant' and message.get('phase') in {'commentary', 'final_answer'}:
                item['phase'] = message['phase']
            items.append(item)
        for call in message.get('tool_calls') or []:
            if call['id'] in paired:
                items.append({'type': 'function_call', 'call_id': call_id(call['id']),
                              'name': call['function']['name'], 'arguments': call['function'].get('arguments', '{}')})
            else:
                items.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text',
                    'text': 'Historical tool call with UNKNOWN outcome; verify before repeating: ' + json.dumps(call, ensure_ascii=False)}]})
    return items
