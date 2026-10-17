# Smaller Temporal histories

This complements the checkpoint/Continue-As-New handoff. It reduces bytes and
events within each execution, without changing model context-compaction policy.

## Inline payload compression

`MOYAI_COMPRESS_TEMPORAL_PAYLOADS=true` enables lossless compression of suitable
inline payloads before encryption. Compression is attempted for payloads of at
least 1 KiB and retained only when it saves enough bytes to cover format overhead.
Small or high-entropy inputs retain the original encrypted-inline format.

The 64 KiB offload decision still uses the original serialized size. A large
payload continues to use an immutable encrypted object reference even if it
compresses well. This avoids replacing a small reference with a larger inline
blob and adds no object-store calls for smaller inline payloads.

The new format has an authenticated expected-length header and bounded decoding.
Readers accept old inline data, old object references, and the compressed format
regardless of whether compressed writing is enabled.

Deploy compatible readers to all API, coordinator, broker, and worker processes
before enabling compressed writes. For rollback, disable compressed writes but
retain the decoder: existing compressed histories still require it. No
production configuration change is performed by this PR.

## Fewer public-stream events

For histories marked `moyai-workflow-public-events-v1`, workflow-origin tool and
public capability events publish directly to the workflow's existing SDK
`WorkflowStream`. They no longer schedule a separate event-handler activity
which then signals the event back to the same workflow.

Model text still streams live from its model activity through the SDK's existing
100 ms batching. Before transport, the public projection excludes reasoning,
partial tool-argument events and internal events that the API already does not
render. Response boundaries and the existing root/child visibility checks remain.

This affects only the public projection. Original events still flow through the
agent SDK, and authoritative model/tool results, step checkpoints, effect
receipts, authority checks and cancellation boundaries remain intact.

The old activity registration and command path remain available for old-history
replay. Continue-As-New retains the same SDK stream offsets and publisher state.

## Verification

Compare identical workloads using:

- Serialized Temporal history bytes and event counts.
- Scheduled event-handler activities and model-stream signals.
- Object-store requests and encode/decode cost.
- Public text, response boundaries, tool-call/result pairs and ordering.
- Old-history replay, compressed/uncompressed decoding, child isolation, and
  checkpoint/Continue-As-New behavior.

Controlled model tests isolate the transport changes. Real-app before/after
acceptance uses the configured gateway and real sandbox separately; neither
sample establishes universal compression ratios or production capacity.
