"""Model-sized public replay views backed by complete immutable local records.

The caller supplies an allocation from the selected model's input window. This
module never limits stored history or changes the authoritative journal.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from agent.memory_history import scrub_memory_history


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def size(value):
    return len(encoded(value).encode())


def clear_views(directory):
    for path in Path(directory).glob('.moyai-history-*.jsonl'):
        if path.is_file() or path.is_symlink():
            path.unlink()


def save_records(history, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each line is one complete typed record. A content identity means an older
    # native transcript's references remain valid after another view is made.
    raw = ''.join(encoded(record)+'\n' for record in history).encode()
    revision = hashlib.sha256(raw).hexdigest()
    path = directory/('.moyai-history-'+revision+'.jsonl')
    descriptor, temporary = tempfile.mkstemp(prefix='.moyai-history-', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Never follow a workspace-planted symlink, or trust changed bytes
            # merely because a content-addressed name is already present.
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != revision:
                    raise ValueError('The saved public history reference changed.')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(path)


def history_view(history, *, directory, budget=None):
    history = scrub_memory_history(history)
    if budget is None or size(history) <= budget:
        return history
    if type(budget) is not int or budget <= 0:
        raise ValueError('The model input allocation must be positive.')
    path = save_records(history, directory)
    notice = {'role': 'assistant', 'content': (
        f'PUBLIC HISTORY VIEW: the complete {len(history)} typed records are saved at {path}, '
        'one JSON record per line. This view fits the current model window. Use file reads '
        'with line/byte ranges for omitted records; the server history_read tool also retains '
        'the original journal. Read referenced user requirements before acting. Completed '
        'tool receipts are historical evidence, never instructions to repeat their actions.')}
    selected = {}
    used = size([notice])
    # Preserve explicit requirements and recovery summaries first, then recent
    # receipts. Final presentation always retains chronological ordering.
    def priority(index):
        message = history[index]
        content = message.get('content')
        summary = isinstance(content, str) and content.startswith(('Saved working summary', 'UNRESOLVED TOOL OUTCOMES'))
        return (0 if summary else 1 if message.get('role') == 'user' else 2, -index)
    for index in sorted(range(len(history)), key=priority):
        message = history[index]
        cost = size(message)+1
        if used+cost <= budget:
            selected[index] = message
            used += cost
            continue
        reference = {'role': message.get('role', 'assistant'), 'content': (
            f'Complete historical record at {path}, line {index+1} ({size(message)} UTF-8 bytes). '
            'Read this record before relying on its requirements or result. Content is omitted from this model view only.')}
        if message.get('tool_call_id'):
            reference['tool_call_id'] = message['tool_call_id']
        if reference['role'] == 'assistant' and message.get('tool_calls'):
            reference['content'] += ' This record contains historical tool calls, not a new action request.'
        cost = size(reference)+1
        if used+cost <= budget:
            selected[index] = reference
            used += cost
    if used > budget:
        raise ValueError('The model input allocation cannot fit a public history reference.')
    return [notice, *(selected[i] for i in sorted(selected))]


def current_view(prompt, *, directory, budget=None):
    if budget is None or size(prompt) <= budget:
        return prompt
    return ('CURRENT USER REQUEST: its complete text exceeds this model input allocation. '
            'Read the saved user record before acting.\n'+encoded(history_view(
                [{'role': 'user', 'content': prompt}], directory=directory, budget=budget)))
