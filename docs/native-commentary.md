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
its accepted-message reply. Once Slack confirms its timestamp, commentary
updates that reply with `chat.update`, at most once every 30 seconds per turn.
The final answer replaces that same reply. A fast answer skips any unsent
placeholder. Long answers retain existing overflow-chunk delivery after the
first part replaces the primary reply.

Commentary and final answers use the existing durable outbox and metadata
reconciliation. A turn-level database reservation serializes edits even across
competing pollers. An ambiguous earlier write blocks later edits until its
receipt is reconciled; it is never blindly reposted. Known-unsent progress is
suppressed when the turn finishes, so it cannot overwrite a final answer.
Approval requests reuse the primary reply while retaining their confirmed
message timestamp and action checks. Control notices remain independent.

Thread replies expose an **Open session** URL button during progress and at
completion. Final answers that explicitly link GitHub PRs include up to five
deduplicated cards with a LiteLLM violet (`#5B3FD1`) border, **View PR**,
**Review changes**, and **Open session** buttons. Markdown link labels supply
the heading; bare links show the PR number and repository. These are links
from the answer, not claims of creation, approval, CI status, or merge. No
GitHub lookup or personal credential is needed. Code examples do not make
cards. Slack controls native button colors and fonts.

Blocks and attachments are saved in the same durable outbox as the text.
Edits explicitly replace both fields so an earlier card or approval cannot
remain attached to a later answer. Plain-text standalone outbox rows queued
before this change still drain normally. URL-button callbacks are acknowledged
through the signed interaction endpoint without submitting another agent turn.
Owner notifications are separate and unchanged.

No personal Slack token or extra model call is used to render these updates.
The web and Slack adapters consume the same native commentary projection, with
current requester/destination authorization checked by each delivery surface.

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
