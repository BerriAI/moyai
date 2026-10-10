# Sustained goals and reports

This describes the unreleased intelligence branch. Use `/goal <objective>` or
ask naturally: “Work on this for three hours, then report what you found.” Moyai
registers the original user request as a durable goal and continues across normal
final responses. The goal survives computer replacement independently of its files.

| Request | Meaning |
| --- | --- |
| “For three hours” | Work timebox, then report the result and remaining work. |
| “At least three hours” | Minimum active work before verified completion. |
| “Up to three hours” | Work cap; verified completion can end it earlier. |
| “By 5 pm” | Explicit clock deadline, interpreted with the user's timezone. |
| “Until the tests pass and the PR is ready” | Outcome goal; no invented time or continuation limit. |

The model interprets natural language through the shared `goal` tool. The harness
stores and enforces the resulting budget. Timeboxes and caps enter **Reporting**
at their boundary. Budget exhaustion does not mean the objective was completed.

## Control ongoing work

Use `/goal status` to inspect the registered objective and recorded active time.
Use `/goal pause` or the session's Stop control to stop work. `/goal resume`
continues an unfinished goal with its accrued time intact. Extending an exhausted
budget requires a new user directive describing the new allowance. `stop` and
`cancel` after `/goal` pause the goal. `/goal clear` cancels it so a new goal can
be created; the saved audit record is retained.

A queued command is not a registered goal. The status display reflects durable
state: Running, Reporting, Budget ended, Needs your input, Paused, Interrupted,
or independently verified Completed. Credential and child waits retain their
existing waiting display.

Commands read authenticated user input. Quoted Slack messages, tool output and
old history cannot create a control directive. Controls still use the current
session execution path; removing computer startup for these requests is separate
work. This is a conversation command, not a workspace-level Slack slash command.

## Completion and delivery

The worker saves progress, remaining requirements and original journal references
through `goal progress`. Completion requests and final-response checkpoints get a
fresh, tool-free review through the normal authenticated model gateway. That
reviewer can read original journal records, including evidence that contradicts
the worker's claim. Successful tool IDs alone do not establish completion.

If the evidence does not fit the selected model, or review is unavailable, the
goal remains incomplete. A new user input or new work during review invalidates
its completion decision. A persistent external blocker requires independent
review after a request to try another approach before it can await user input.

When the budget ends, the harness stops at a safe tool boundary and asks for a
report. It does not kill an in-flight tool or pretend an uncertain action failed.
New task inference is rejected while the goal is reporting. If final inference
fails, the coordinator builds a factual report from the objective, recorded time,
last saved progress, evidence references and remaining work. Agent progress is
labeled as reported; it is not promoted to independently verified success.

The existing saved-answer transaction also commits Slack delivery intent. Slack's
existing outbox reconciles uncertain sends. A transport outage can delay delivery;
the report remains accessible in the web session. The agent does not infer
successful Slack delivery from its own final text.

## Persistence and recovery

`session_goals` owns state; `goal_clock_receipts` records idempotent cumulative
clock updates. A computer sends active execution time, stopping its clock during
saved waits or downtime. Parallel children do not multiply the parent's time.
A crash can lose time since its last successful clock receipt; the normal receipt
interval is two seconds, and a broker outage can extend that gap. No unobserved
downtime is counted as work. This is active session time, including inference and
tool execution, not a measure of useful progress.

A legacy `goal.json` is imported once. Completed goals stay completed but are
labeled as legacy completion. Unfinished legacy goals load paused so their
original budget can be confirmed without granting a fresh allowance. The file
ceases to be authoritative. Normal new-format restart does not pause a goal.

Administrator time, iteration and spending policies still apply. They never
silently reset when the agent continues. Unrecoverable execution failure saves an
interruption report; it does not masquerade as a user pause or completed objective.

Follow [schema migration](schema-migrations.md) for schema revision 5. This change
requires coordinated runtime migration; it is not an API-only mixed-build update.

## Verification and remaining qualification

```sh
uv run --frozen pytest -q tests/test_goals.py tests/test_goal_status.py
uv run --frozen pytest -q tests/test_context_gateway.py tests/test_slack_chat.py
node --test tests/test_goal_status.cjs tests/browser/goals.cjs
```

The fake-clock contract suite covers the three-hour boundary, computer replacement
near that boundary, failed report inference, source-bound directives, preserved
elapsed time, independent evidence reads, stale review rejection and atomic report
settlement. Gateway integration checks verify accounting and tool-free review;
Slack integration checks verify report/send-intent commit and duplicate suppression.
These checks do not substitute for a real sustained three-hour deployment rehearsal.
The wider recovery, live model and release qualification remain tracked in the
[implementation ledger](intelligence-implementation.md).

The design follows [QM's goal controller at daeb2deb](https://github.com/yc-software/qm/blob/daeb2deb90383597a21b0a36bd15a45488d321af/src/harness/goal.ts):
model-owned interpretation, independent review, explicit floors/caps, user-owned
resume and a reporting phase when a budget ends. Moyai retains its own durable
runner, accounting, storage and delivery infrastructure.
