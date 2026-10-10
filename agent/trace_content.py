"""Bounded trace payloads, separate from public activity and private reasoning."""
import json
import os
import re

from agent.activity import public_text

PRIVATE_FIELDS = re.compile(r'(?i)(token|secret|password|authorization|api.?key|cookie|reasoning|thinking|instructions|system_prompt|^env$|^environment$)')


def private_tool(name):
    name = re.sub(r'^(?:mcp[_-]+)?(?:workspace|moyai)[_-]+', '', str(name))
    return name.startswith(('credentials_', 'skills_', 'memory_')) or name == 'call'


def trace_content(value, *, secrets=(), limit=16000):
    def clean(item, depth=0):
        if depth > 12:
            return '[depth limit]'
        if isinstance(item, dict):
            kind = item.get('type')
            if isinstance(kind, str) and kind in {'image_url', 'image', 'input_image', 'thinking', 'reasoning'}:
                return '[image or private reasoning omitted]'
            return {str(k)[:100]: '[redacted]' if PRIVATE_FIELDS.search(str(k)) else clean(v, depth + 1)
                    for k, v in list(item.items())[:100]}
        if isinstance(item, (list, tuple)):
            return [clean(v, depth + 1) for v in item[:100]]
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
                if isinstance(parsed, (dict, list)):
                    return clean(parsed, depth + 1)
            except (ValueError, RecursionError):
                pass
            text = item
            for secret in (*secrets, os.environ.get('WORKSPACE_RUN_TOKEN', '')):
                if secret:
                    text = text.replace(secret, '[redacted]')
            text = re.sub(r'data:[^\s"\']+', '[image omitted]', text)
            return public_text(text, limit)
        return item if item is None or isinstance(item, (bool, int, float)) else str(item)[:limit]
    cleaned = clean(value)
    text = cleaned if isinstance(cleaned, str) else json.dumps(cleaned, ensure_ascii=False)
    return text[:limit] + (' [truncated]' if len(text) > limit else '')
