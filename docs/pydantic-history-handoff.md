# Agent history handoff

Native agent turns continue into a new Temporal execution before their history
fills up. The session, turn, and workflow ID stay the same; Temporal assigns a
new execution run ID. The SDK uses a new agent run ID within that logical turn.

The worker checks Temporal's Continue-As-New suggestion and its recorded history
size and event count at a settled `ModelRequestNode` boundary. Defaults are 8 MiB
and 8,000 events. `MOYAI_HISTORY_CONTINUE_BYTES` and
`MOYAI_HISTORY_CONTINUE_EVENTS` override those budgets; an activity records the
values so workflow replay does not read mutable process configuration.

## What crosses the boundary

The checkpoint contains the current SDK conversation, including the normalized
results of the completed tool batch, cumulative usage, retry budgets, step
number, admitted execution scope, and existing workspace reference. It is saved
in the existing immutable encrypted object store. The continuation input carries
its reference and the SDK's Workflow Streams state, including stream offsets and
publisher deduplication state.

The original prompt and deferred replies are not submitted again. Native
capabilities and tool discovery reconstruct their state from the continued
conversation. Root-only product permissions, inference accounting, saved
activity, and public progress distinguish SDK segments from delegated runs.

The checkpoint must be accepted while the agent iterator and stream pollers are
still alive. A refused checkpoint or storage failure leaves the agent running in
the current execution; queued guidance is delivered there, and a later boundary
can try again. Authorization loss and Stop remain terminal.

After acceptance, the agent iterator is closed before Continue-As-New. No model
request or tool batch is interrupted to make a checkpoint. Pending model input
is drained before choosing the boundary. Guidance arriving during the handoff
is carried to the next execution in receipt order without falsely acknowledging
that the old agent consumed it. Stop remains terminal, including during
checkpoint persistence and restoration.

Existing Temporal histories retain their original command sequence through the
`moyai-agent-history-handoff-v1` patch marker.

## Checkpoints, compaction, and payload offloading

These mechanisms serve different purposes:

- Harness step snapshots preserve application messages; they do not erase old
  Temporal events.
- LLM context compaction reduces subsequent model inputs. It cannot replace
  uncompacted payloads already recorded in an execution history.
- The payload codec offloads individual serialized payloads of 64 KiB or more.
  Smaller encrypted activity arguments, results, updates, and signals still
  accumulate in the history.
- Continue-As-New starts a fresh execution history. Previously stored execution
  histories remain available under Temporal's retention policy.

Lowering the offload threshold or batching presentation events can reduce
history growth further, but this change keeps the existing codec and event
delivery behavior.

## Boundaries and validation

This is a cooperative handoff, not automatic recovery of an interrupted tool.
Unanswered tool calls and uncertain effect receipts cannot cross it. A single
unusually large or long-running step can still grow before a settled boundary;
the early budget leaves headroom but is not a replacement for bounding
individual activity payloads and streaming volume. A workspace that expires
during the handoff must still satisfy the normal workspace availability checks.

Real Temporal regression tests cover repeated history rollover, one execution
of each file-appending action, cumulative usage, loaded capabilities, persistent
delegation, stream offsets, replay, no-computer turns, refused/failed checkpoint
saves, and Stop/guidance during checkpoint commit.
Scripted models in those tests supplement the baseline/final real-app acceptance
run with the configured gateway and Modal.

References:

- https://docs.temporal.io/cloud/limits
- https://docs.temporal.io/develop/python/continue-as-new
- The pinned Pydantic AI Harness `step_persistence/README.md`, especially
  “Earlier checkpoints and notifications.”
