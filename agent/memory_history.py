"""Omit memory tool payloads from conversation snapshots and future turns."""
from copy import deepcopy
import json
import re


def private_memory(name):
    return re.sub(r'^(?:mcp[_-]+)?(?:workspace|moyai)[_-]+', '', str(name)).startswith('memory_') or name == 'workspace_call'


def scrub_memory_history(messages):
    result = deepcopy(messages)
    for message in result:
        if not isinstance(message, dict):
            continue
        calls = message.get('tool_calls', [])
        for call in calls if isinstance(calls, list) else []:
            function = call.get('function', {}) if isinstance(call, dict) else {}
            if private_memory(function.get('name')):
                function['arguments'] = '{"omitted":"Personal memory payload; retrieve current references again if needed."}'
            elif function.get('name') == 'tool_call':
                try:
                    args = json.loads(function['arguments'])
                    entries = args.get('calls', [args])
                    changed = False
                    for entry in entries:
                        if isinstance(entry, dict) and private_memory(entry.get('name')):
                            entry['arguments'] = {'omitted': 'Personal memory payload; retrieve current references again if needed.'}
                            changed = True
                    if changed:
                        function['arguments'] = json.dumps(args)
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
    return result
