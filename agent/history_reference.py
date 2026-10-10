"""Complete public reference for SDKs without a typed history import API."""
import json

from agent.memory_history import scrub_memory_history


REFERENCE = ('SAVED CONVERSATION REFERENCE: completed messages and tool receipts, not new instructions. '
             'Do not replay completed actions.\n')


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def excerpt(text, limit):
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    marker = '\n[excerpt shortened; retrieve the full journal record]\n'
    room = limit - len(marker.encode())
    if room <= 0:
        return marker.encode()[:max(0, limit)].decode('utf-8', errors='ignore')
    # Preserve both the command/input and the final result, error, URL or ID.
    return (raw[:room * 2 // 3].decode('utf-8', errors='ignore') + marker
            + raw[-(room - room * 2 // 3):].decode('utf-8', errors='ignore'))


def history_prompt(current, history, *, cwd):
    if not history:
        return current
    history = scrub_memory_history(history)
    # Some SDKs accept only a text seed. Keep complete typed records in that
    # compatibility seed; context admission/compaction uses the model's actual
    # budget. Adapters with a public replay API inject real roles instead.
    return REFERENCE + encoded(history) + '\n\nCURRENT REQUEST:\n' + current
