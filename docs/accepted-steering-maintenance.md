# Accepted steering during maintenance

An `accepted` turn is a steering input durably enqueued into its parent's workflow.
It is not dispatched as an independent workflow and cannot write its own
maintenance checkpoint. Waiting for such a checkpoint prevents both the drain
barrier and guarded resume from completing.

Drain accounting excludes an accepted input from `unsafe_turns` only when:

- Its mode is `asap`, with a nonempty enqueue ID and acceptance timestamp.
- It has no preparation, result, result fingerprint, conversation, deferred
  result, result acceptance, error, or finish timestamp indicating independent
  execution, and its usage is an empty JSON object.
- Its parent exists in the same session, with the same actor, model, and
  connection ID set. Malformed usage or connection data remains unsafe.
- The parent is an execution turn, not another steering input.
- The parent is terminal (`completed`, `failed`, `stopped`, or `resumed`), or
  satisfies the existing checkpoint checks for the current maintenance generation.
  A live starting/running/saving parent must also have preparation: the SDK cannot
  acknowledge enqueue before the parent's agent run exists. A deferred waiting
  parent instead requires the verified deferred-waiting receipt and accepted saved
  conversation; that existing contract does not require preparation.

Missing, cross-session, cyclic, or otherwise invalid parent links remain unsafe.
Unacknowledged steering and unknown statuses remain unsafe. Global outstanding
effects and both inference-request ledgers still prevent drain/resume, including
work attached to an accepted input or a terminal parent.

This is read-only accounting. It does not change turn statuses, create child
checkpoints, cancel work, replay input, or bypass worker ownership and generation
checks. `parked` continues to count checkpointed execution turns rather than the
accepted inputs they own.

The PostgreSQL regressions in `native/tests/test_product_drain.py` exercise real
admission and steering-receipt transitions, parent validation, checkpoint
validation, unchanged saved rows, and guarded resume. The same file retains
Temporal parking, worker restart, replay, deferred waiting, and effect-drain tests.
