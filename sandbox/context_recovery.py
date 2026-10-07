"""Restart a native session only after a confirmed context rejection and receipts."""
import time
try:
    from .context_store import ContextUnavailable
except ImportError:
    from context_store import ContextUnavailable


def run_with_context_recovery(agent, prompt, history, invoke):
    ctx, store = agent.context, agent.context_store
    directory = ctx.spec.get('history_reference_dir', ctx.cwd)
    current = agent.journal.prompt(prompt, history, cwd=directory)
    previous = None
    attempts = 0
    timeout = ctx.spec.get('timeout')
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        agent.context_timeout = max(0, deadline - time.monotonic()) if deadline else None
        if deadline and not agent.context_timeout:
            raise ContextUnavailable('The task deadline elapsed during context recovery. Saved receipts are preserved.')
        ctx.relay.context_required = None
        try:
            result = invoke(current)
        except Exception:
            if not getattr(ctx.relay, 'context_required', None):
                raise
            result = None
        pressure = getattr(ctx.relay, 'context_required', None)
        if not pressure:
            return result
        # User stop/steering/credentials/rotation win over maintenance.
        if agent.stopped.is_set():
            return result or {'completed': False, 'interrupted': True,
                             'messages': agent.journal.messages, 'final_response': ''}
        if store is None:
            raise ContextUnavailable('This runtime needs a saved context journal before it can compact. No task actions were retried.')
        if store.pending or agent.journal.pending:
            raise ContextUnavailable('Context needs compaction, but tool outcomes are pending. Verify their receipts before resuming.')
        progress = agent.journal.completed_tools
        size = pressure['input_tokens']
        attempts = attempts + 1 if previous and previous[0] == progress else 1
        if attempts > 3 or (previous and previous[0] == progress and size >= previous[1]):
            raise ContextUnavailable('The current request, instructions or tools still exceed this model\'s input budget after compaction. '
                'Saved records are preserved. Use a model with more input room or reduce the required input.')
        previous = (progress, size)
        # Text emitted by a rejected SDK request may be an SDK error. Completed
        # commentary/tool receipts were already journaled by the public hooks.
        pending_text = getattr(agent, 'pending_text', [])
        pending_text.clear()
        target = max(512, min(6000, pressure['input_budget'] // (8 * attempts)))
        ctx.activity.commentary('Compacting saved context before continuing. Completed tool receipts are preserved.')
        store.compact(ctx.relay.compact, force=True, summary_bytes=target)
        history = store.history()
        current = agent.journal.prompt(
            'Continue the unfinished task from the saved receipts. This is a context handoff, not a new request. '
            'Do not repeat completed actions.\n\nOriginal current request:\n' + prompt,
            history, cwd=directory)
