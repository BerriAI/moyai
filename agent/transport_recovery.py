"""Resume from public receipts, never by resending a failed HTTP operation."""
from agent.context_store import ContextUnavailable


MAX_TRANSPORT_ATTEMPTS = 3


def retryable_failure(failure, *, live=False):
    return (isinstance(failure, dict) and failure.get('version') == 1
            and failure.get('route') in {'/v1/messages', '/v1/responses', '/v1/chat/completions'}
            and failure.get('transient') is True
            and (failure.get('response_started') is False
                 or (live and failure.get('transport_interrupted') is True))
            and not failure.get('uncertain_tool'))


def valid_retry(marker):
    if not isinstance(marker, dict) or marker.get('version') != 1:
        return False
    failure, checkpoint = marker.get('failure'), marker.get('checkpoint')
    return (retryable_failure(failure)
            and isinstance(checkpoint, dict) and isinstance(checkpoint.get('epoch'), str)
            and bool(checkpoint['epoch']) and type(checkpoint.get('seq')) is int and checkpoint['seq'] > 0)


def recovery_marker(agent, result):
    """Called only after the SDK invocation has exited and stopped its tools.

    Historical unresolved receipts remain reference data; current unknown tool
    outcomes cannot authorize automatic continuation. This does not promise
    exactly-once semantics for arbitrary external writes or model billing.
    """
    store, journal = getattr(agent, 'context_store', None), getattr(agent, 'journal', None)
    if store is None or journal is None:
        return None
    relay = agent.context.relay
    if (journal.context_store is not store
            or journal.pending or getattr(relay, 'uncertain_tool', False)
            or getattr(agent, 'boundary_failed', False) or agent.stopped.is_set()
            or any(result.get(key) for key in ('completed', 'interrupted', 'partial'))
            or result.get('failed') is not True):
        return None
    marker = {'version': 1, 'failure': getattr(relay, 'last_failure', None), 'checkpoint': store.checkpoint()}
    return marker if valid_retry(marker) else None


def validate_recovery(store, marker):
    if not valid_retry(marker) or store is None or store.checkpoint() != marker['checkpoint']:
        raise ContextUnavailable('The saved recovery checkpoint could not be verified. '
                                 'No actions were replayed; inspect the saved receipts before continuing.')
