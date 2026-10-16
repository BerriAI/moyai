# Native progress and final answers

Moyai uses Pydantic AI's `TextPart` events and Responses `phase` metadata for
commentary and final answers. The agent still uses the SDK model/tool loop,
Workflow Streams, retry budget, and conversation serialization.

The `Commentary` capability uses the public `after_model_request` hook to
reject a commentary-only closeout through `ModelRetry`. It uses
`after_output_process` to exclude commentary from the final plain-text output
when both phases appear in one response. The original model messages remain
unchanged in native history. Unphased providers retain their existing text/tool
behavior. The Temporal patch marker preserves recorded completion decisions
when replaying workflows that started before this change.

Public root commentary is saved through a native `durable_operation` into the
existing encrypted application event table. Its turn/step/part identity makes
retries idempotent. This small presentation record survives model-context
compaction without reading every historical snapshot. Older turns fall back to
the commentary still present in their latest native snapshot.

## Web

While running, a turn shows elapsed work time and its latest commentary.
Expanding the work section shows earlier updates. When the turn finishes, the
section collapses and the independently saved final answer stays visible.
Expanding a completed turn loads its saved progress on demand.

The progress endpoint enforces the same requester-only boundary as live working
activity. It excludes child-agent text and thinking parts, and redacts configured
gateway and standalone connection secrets. Shared final answers do not confer
access to another requester's working transcript.

## Slack

The signed bot transport remains separate from personal Slack agent tools.
Standalone mode reserves one working post; the existing-product adapter reuses
its existing running notice. Once Slack confirms its timestamp, commentary
updates that post with `chat.update`, at most once every 30 seconds per turn.
Completion changes the working post to a short terminal status. The final
answer and approval requests continue through the existing durable outbox.

Progress never uses a personal Slack token, posts token-by-token updates, or
reposts a message whose original send acknowledgement was lost. Updates check
current destination authority and use a compare-and-set reservation. A lost
update acknowledgement leaves the original post receipt intact; retrying an
edit at its known timestamp cannot duplicate the final answer.

## Verification

```sh
uv run --frozen --directory native pytest -q \
  tests/test_commentary.py tests/test_slack_progress.py \
  tests/test_slack_delivery.py tests/test_product_slack.py \
  tests/test_product_slack_authority.py tests/test_product_slack_contention.py \
  tests/test_live_events.py tests/test_live_stream.py
npm test
npm run typecheck
npm run build
npm run test:ui
```

Set `MOYAI_TEST_POSTGRES` to a disposable local PostgreSQL database for the
product contention cases. Browser and Slack transport fixtures are regression
tests, not evidence of live model or Slack-provider behavior.

For actual-app before/after verification, run both revisions with `moyai dev`
against separate local PostgreSQL databases and the same real configured
gateway/model. Ask each to create and complete two native planning checklist
items with brief progress messages before the tool calls. Confirm that the
baseline leaves commentary expanded, while this revision folds it above the
final answer; reload and expand to verify saved progress. Check the native plan
and turn records to confirm that real tools ran and both items completed.
No Modal computer is needed for this planning-only scenario.
