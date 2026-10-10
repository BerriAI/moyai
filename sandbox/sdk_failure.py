"""Body-free SDK diagnostics; never publish exception text or native stderr."""
import re

try:
    from .broker_failure import safe_id, safe_error, error_summary, CLAUDE_ERRORS, CODEX_ERRORS, CLAUDE_RESULTS, CLAUDE_TERMINAL_REASONS
except ImportError:
    from broker_failure import safe_id, safe_error, error_summary, CLAUDE_ERRORS, CODEX_ERRORS, CLAUDE_RESULTS, CLAUDE_TERMINAL_REASONS


def status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def exception_details(exc):
    def name(error):
        value = type(error).__name__
        return value if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value) else 'Exception'
    result = {'source': 'exception', 'exception_type': name(exc)}
    if exc.__cause__ is not None:
        result['cause_type'] = name(exc.__cause__)
    for field in ('exit_code', 'errno'):
        value = getattr(exc, field, None)
        if type(value) is int:
            result[field] = value
    if (code := status(getattr(exc, 'status_code', None))) is not None:
        result['http_status'] = code
    return result


def codex_details(error, *, will_retry=None):
    result = {'source': 'native_error'}
    info = error.get('codexErrorInfo') if isinstance(error, dict) else None
    code = next(iter(info), '') if isinstance(info, dict) else info
    if isinstance(code, str) and code in CODEX_ERRORS:
        result['code'] = code
        # Pinned Codex reports clean HTTP EOF without response.completed as
        # `other`. Recognize only this exact protocol failure, never arbitrary
        # native error text (which can contain private provider payloads).
        if (code == 'other' and error.get('message') ==
                'stream disconnected before completion: stream closed before response.completed'):
            result['code'] = 'responseStreamDisconnected'
        value = info.get(code) if isinstance(info, dict) else None
        if isinstance(value, dict) and (http := status(value.get('httpStatusCode'))) is not None:
            result['http_status'] = http
    if type(will_retry) is bool:
        result['will_retry'] = will_retry
    return result


def claude_details(result):
    details = {'source': 'native_result',
               'native_status': result.subtype if result.subtype in CLAUDE_RESULTS else 'unknown',
               'is_error': bool(result.is_error)}
    if (http := status(getattr(result, 'api_error_status', None))) is not None:
        details['http_status'] = http
    reason = getattr(result, 'terminal_reason', None)
    if reason in CLAUDE_TERMINAL_REASONS:
        details['terminal_reason'] = reason
    errors = getattr(result, 'errors', None)
    if isinstance(errors, list):
        details['error_count'] = len(errors)
    return details


def failure_summary(diagnostic):
    label = 'Codex' if diagnostic['sdk'] == 'codex' else 'Claude Agent SDK'
    reason = diagnostic.get('boundary_reason') or diagnostic.get('code') or diagnostic.get('exception_type')
    if not reason:
        reason = diagnostic.get('terminal_reason') or diagnostic.get('native_status')
        if reason in {None, 'success', 'completed'}:
            reason = 'API error' if diagnostic.get('is_error') else 'incomplete turn'
    if diagnostic.get('error_code') not in {None, 'unknown'} or diagnostic.get('sdk_error'):
        return error_summary(diagnostic, technical=False) + ' Saved tool receipts are preserved.'
    parts = [reason]
    if (http := safe_error(diagnostic).get('http_status')) is not None:
        parts.append('HTTP ' + str(http))
    if diagnostic.get('exit_code') is not None:
        parts.append('exit code ' + str(diagnostic['exit_code']))
    if diagnostic['pending_tools']:
        parts.append(str(diagnostic['pending_tools']) + ' unresolved tool(s)')
    return label + ' stopped (' + ', '.join(parts) + '). Saved tool receipts are preserved.'


def failure_diagnostic(agent, sdk, details):
    diagnostic = {'version': 1, 'sdk': sdk, 'source': 'incomplete_turn', **details,
                  'pending_tools': len(agent.journal.pending),
                  'boundary_failed': bool(getattr(agent, 'boundary_failed', False))}
    if getattr(agent, 'boundary_reason', ''):
        diagnostic['boundary_reason'] = agent.boundary_reason
    if hasattr(agent, 'model_calls'):
        diagnostic['model_calls'] = agent.model_calls
    broker = getattr(agent.context.relay, 'last_failure', None)
    if isinstance(broker, dict):
        for key, value in safe_error(broker).items():
            if (key not in {'request_id', 'http_status'} and details.get('http_status') is not None
                    and details['http_status'] == broker.get('http_status')):
                diagnostic.setdefault(key, value)
        if request_id := safe_id(broker.get('request_id')):
            diagnostic['broker_request_id'] = request_id
        if (http := status(broker.get('http_status'))) is not None:
            diagnostic.setdefault('http_status', http)
    diagnostic.update(safe_error({'http_status': diagnostic.pop('http_status', None)}))
    return diagnostic


def report_failure(agent, result):
    # Only the recovery owner knows whether an attempt is terminal.
    diagnostic = result.get('sdk_failure') if isinstance(result, dict) else None
    reporter = getattr(agent.context.activity, 'failure', None)
    if diagnostic and reporter is not None:
        reporter(failure_summary(diagnostic), diagnostic)
    return result
