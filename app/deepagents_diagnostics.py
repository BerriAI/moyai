"""Body-free diagnostics from the failed workflow's actual exception chain."""
from temporalio.exceptions import ActivityError, ApplicationError

from sandbox.broker_failure import safe_error


ACTIVITY_TYPES = {
    'deepagents.invoke_model', 'deepagents.invoke_tool', 'deepagents_prepare_v1',
    'deepagents_boundary_v1', 'deepagents_publish_v1', 'deepagents_computer_v1',
}


def workflow_failure(exc):
    diagnostic = {'version': 1, 'sdk': 'deepagents-temporal', 'source': 'workflow_failure'}
    seen = set()
    error = exc
    cause = exc
    while isinstance(cause, BaseException) and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ActivityError) and cause.activity_type in ACTIVITY_TYPES:
            diagnostic['activity_type'] = cause.activity_type
        error = cause
        cause = getattr(cause, 'cause', None)
    exception_type = error.type if isinstance(error, ApplicationError) else type(error).__name__
    diagnostic['exception_type'] = safe_error({'exception_type': exception_type}).get(
        'exception_type', 'ApplicationError' if isinstance(error, ApplicationError) else 'Exception')
    if isinstance(error, ApplicationError):
        diagnostic['non_retryable'] = bool(error.non_retryable)
        if error.details and isinstance(error.details[-1], dict):
            if recovery := safe_error(error.details[-1]).get('recovery'):
                diagnostic['recovery'] = recovery
                if recovery['method'] == 'catalog_read':
                    diagnostic['action'] = {'dispatched': False, 'outcome': 'rejected'}
    if isinstance(error, ApplicationError) and error.type == 'BrokerRejected' and error.details:
        if len(error.details) > 1:
            diagnostic.update(safe_error(error.details[1]))
        diagnostic.update(safe_error({'http_status': error.details[0]}))
    if isinstance(error, ApplicationError) and error.type == 'ModelRequestFailed' and error.details:
        diagnostic.update(safe_error(error.details[0]))
    if isinstance(error, ApplicationError) and error.type == 'ToolNotDispatched' and error.details:
        detail = safe_error(error.details[0])
        if detail.get('outcome_type') == 'ToolNotDispatched':
            diagnostic.update(detail)
            diagnostic['action'] = {'dispatched': False, 'outcome': 'rejected', 'retryable': False}
    if isinstance(error, ApplicationError) and error.type == 'UncertainToolOutcome' and error.details:
        # These structured details were captured before suppressing the unsafe
        # exception chain; never deserialize messages or Temporal stack strings.
        detail = safe_error(error.details[0])
        if detail.get('failure_stage'):
            diagnostic.update(detail)
            diagnostic['outcome_type'] = 'UncertainToolOutcome'
    return diagnostic
