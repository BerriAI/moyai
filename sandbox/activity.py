"""Public work updates. Never forward arbitrary tool arguments or result bodies."""
import json
import os
import re
import threading
import time
import uuid


def public_text(value, limit=2000):
    text = str(value or '')
    for name in ('WORKSPACE_ACCESS_CLIENT_ID', 'WORKSPACE_ACCESS_CLIENT_SECRET'):
        secret = os.environ.get(name, '')
        if secret:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1>|$)', '', text, flags=re.I | re.S)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', '[redacted]', text, flags=re.S)
    text = re.sub(r'(?i)\b(?:sk-[\w-]{8,}|xox[baprs]-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+)', '[redacted]', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*[\"\']?(?:bearer|basic)\s+)[^\s\"\']+', r'\1[redacted]', text)
    text = re.sub(r'(?i)(\b(?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*)[\"\']?\s*[:=]\s*)(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;&]+)', r'\1[redacted]', text)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def focus_text(value):
    """A short public description, never a command/Markdown preview."""
    text = public_text(value, 500).strip()
    if not text or re.search(r'[<>`{}\[\]\\/]|https?:|\b(?:curl|git|pytest|npm|python)\s', text, re.I):
        return ''
    return ' '.join(text.split())[:120]


def split_focus(text):
    """Read only an explicit leading envelope; remove malformed tags as well."""
    match = re.match(r'^\s*<status>\s*(.*?)\s*</status>', text, re.S | re.I)
    focus = focus_text(match[1]) if match else ''
    prose = re.sub(r'<status\b[^>]*>.*?(?:</status>|$)', '', text, flags=re.S | re.I)
    return focus, prose.strip()


def result_status(result):
    """Read explicit machine status only; tool output may mention unrelated errors."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return 'completed', None
    if not isinstance(result, dict):
        return 'completed', None
    if result.get('status') == 'yielded_to_background':
        return 'backgrounded', None
    code = result.get('exit_code', result.get('returncode'))
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    failed = (bool(result.get('error')) or result.get('isError') is True or
              result.get('success') is False or result.get('status') in ('error', 'failed') or
              (code is not None and code != 0))
    for block in result.get('content', []) if isinstance(result.get('content'), list) else []:
        if isinstance(block, dict) and block.get('type') == 'text':
            nested, nested_code = result_status(block.get('text'))
            failed = failed or nested == 'error'
            code = code if code is not None else nested_code
    return ('error' if failed else 'completed'), code


def tool_summary(name, arguments):
    args = arguments if isinstance(arguments, dict) else {}
    tool = re.sub(r'^(?:mcp[_-]+(?:workspace|moyai)[_-]+|moyai[_-]+)', '', str(name))
    data = {'tool': public_text(tool, 100), 'category': 'tool'}
    if tool == 'terminal':
        data.update(category='command', label='Run command', command=public_text(args.get('command')))
    elif tool in {'read_file', 'write_file', 'patch', 'search_files', 'list_directory'}:
        labels = {'read_file': 'Read file', 'write_file': 'Write file', 'patch': 'Edit file',
                  'search_files': 'Search files', 'list_directory': 'List files'}
        data.update(category='file', label=labels[tool])
        path = args.get('path') or args.get('file_path') or args.get('target_file')
        if isinstance(path, str):
            data['path'] = public_text(path, 300)
    else:
        # Connector requests can carry keys, personal skill definitions or page
        # bodies. The tool name is enough context; no arbitrary payload preview.
        data['label'] = public_text(tool.replace('__', ' ').replace('_', ' ').capitalize(), 120)
    return data


class ActivityReporter:
    def __init__(self, emit, *, tracing=False, omit_private_tool_payloads=False):
        self.emit = emit
        self.prefix = uuid.uuid4().hex
        self.starts = {}
        self.lock = threading.Lock()
        self.tracing = tracing
        self.omit_private_tool_payloads = omit_private_tool_payloads
        self.trace_starts = {}

    def start(self, call_id, name, args):
        data = tool_summary(name, args)
        with self.lock:
            self.starts[str(call_id)] = (time.monotonic(), data)
            if self.tracing:
                self.trace_starts[str(call_id)] = time.time_ns()
        self.emit('tool', data['label'], {**data, 'activity_version': 1,
                  'call_id': self.prefix + ':' + str(call_id), 'phase': 'started'})

    def complete(self, call_id, name, args, result):
        with self.lock:
            started, data = self.starts.pop(str(call_id), (None, tool_summary(name, args)))
        phase, code = result_status(result)
        data = {**data, 'activity_version': 1, 'call_id': self.prefix + ':' + str(call_id), 'phase': phase}
        if started is not None:
            data['duration_ms'] = round((time.monotonic() - started) * 1000)
        if code is not None:
            data['exit_code'] = code
        self.emit('tool', data['label'], data)
        if self.tracing:
            # Separate events never enter the public activity stream. Capture
            # only after the tool completed; observation must not break work.
            try:
                try:
                    from .trace_content import private_tool, trace_content
                except ImportError:
                    from trace_content import private_tool, trace_content
                with self.lock:
                    start = self.trace_starts.pop(str(call_id), time.time_ns())
                private = self.omit_private_tool_payloads and private_tool(data['tool'])
                self.emit('trace', '', {'tool': data['tool'], 'call_id': data['call_id'],
                          'start_ns': start, 'end_ns': time.time_ns(), 'status': phase,
                          'input': '[private tool payload omitted]' if private else trace_content(args),
                          'output': '[private tool payload omitted]' if private else trace_content(result)})
            except Exception:
                pass

    def failure(self, message, diagnostic):
        # Only the SDK diagnostic builder supplies this body-free record.
        self.emit('error', message, {'activity_version': 1, 'phase': 'sdk_failure', **diagnostic})

    def commentary(self, text, *args, **kwargs):
        # Hermes' interim callback contains public assistant text, never its
        # separate reasoning_callback. Strip tagged reasoning defensively too.
        if isinstance(text, str):
            focus, prose = split_focus(text)
            prose = public_text(prose, 3000).strip()
            if focus:
                self.emit('status', focus, {'activity_version': 1, 'phase': 'focus'})
            if prose:
                self.emit('message', prose, {'activity_version': 1, 'phase': 'commentary'})
