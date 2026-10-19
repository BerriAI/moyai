# tools/

`tool_caller.py` is a core reliability primitive. Never delete it, inline it, or route tool calls around it.

- Every model tool call goes through `ToolCaller.call`, from exactly one place: `Authority.wrap_tool_execute` in `agent.py`. Don't call tool handlers directly or add a second wrapper.
- It decides what a tool failure means for the turn. Transient errors on read-only tools are retried. Cancellation, auth denial and workspace loss escalate to the workflow. Every other error goes back to the model as `ToolFailed`. Without it, one tool error kills the whole turn with "The agent could not complete this turn".
- To change the behavior, edit `RetryPolicy` or `ToolCaller.outcome`, and keep `tests/test_tool_caller.py` and `test_non_retryable_tool_failure_completes_real_temporal_turn` passing.
