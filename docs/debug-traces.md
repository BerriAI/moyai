# Debug traces

Moyai can export a session history as an ATIF v1.8 JSON trajectory. The trace
includes user messages, requester-visible reasoning, tool calls and their
results, along with timing and token metrics.

Open the session's Activity panel and select **Get debug trace**. The same
download is available from the session actions menu for native sessions.

Each turn's input step includes an `extra.activity` tree. Runs contain an ordered
timeline of model responses (preserving text/reasoning/tool part order), tool
calls and results, run/model/tool lifecycle events, and recorded commentary.
Subagent runs and delegated assignments are nested below their parent; delegated
work includes only assignment-generated turns, never subsequent private chats.
Code-mode tool results include their recorded subtools recursively. Calls and
results retain their IDs, and native provider tools are included as well.

Requester-owned turns include recorded reasoning. Teammate turns include the
same public commentary and tool details available in Activity, with
`activity_scope: "public"` and `reasoning_omitted: true`. Their private reasoning,
model context, and raw provider metadata remain hidden. Private memory,
capability and credential tool arguments and results are labeled in place with explanatory redaction text and redacted
metadata, rather than appearing as empty data. Treat the file as sensitive
because it can include prompts, reasoning, and non-private tool inputs and
outputs.

A run's `snapshot_status` distinguishes `complete`, `interrupted`, and
`unavailable` snapshots. Lifecycle events remain visible even without a snapshot;
a started tool with no recorded result is not reported as successful. All saved
continuation segments and retained snapshots are inspected, including earlier
snapshots from before model-history compaction. Repeated responses and parts are
deduplicated. If no retained snapshot contains older content, only its lifecycle
records can be exported; missing reasoning or tool output is never reconstructed.
Code-mode subtool start times are not stored separately, so their timestamps are
labeled `timestamp_source: "parent_call"`; result timestamps are the recorded
tool-return times. Results retain their exact outcome, including denied and
interrupted calls.

The existing ATIF model steps remain available for compatible importers. Nested
activity is additional detail within a step, not extra top-level token usage.

To report a problem, attach the JSON file to Moyai and describe what you expected
instead. Point to the relevant step when possible so the trace can be used to
identify where the interaction went wrong.
