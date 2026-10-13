# Recorded failure explanations

The conversation shows a concise expandable explanation for a failed response,
a failed tool operation, a missing final answer, an attachment-read failure, or
a capture-copy warning. A successful later response keeps its status and answer.
Failed operations remain in the existing collapsible work history, including
after reload and deferred-history loading. Failed tool inputs, output and raw
event prose are not rendered in that row.

The API computes `failure_explanation` from typed event fields at read time.
`workspace_diagnostics` and session diagnostics use the same projection and
retain the original `turn_id`; absent attribution stays unknown. The projection
contains component, observed cause, stage, dispatch/outcome evidence, retryability,
recorded recovery, next action and safe support IDs. Observed exception/status
is not a confirmed root cause. Missing details explicitly remain unknown.
There is no database migration or dependency on the Slack recovery change.

Terminal explanations use only the saved answer's `response_to_id` and matching
terminal `sdk_failure`. Recovered broker errors, later errors and other turns
cannot replace that cause. Earlier saved recovery decisions may be displayed
separately as recovery in the same turn. At most three are included; this is a
bounded view, not a complete execution trace.

## Evidence and recovery

- Tool result fields `dispatched`, `outcome`, `outcome_uncertain`, `retryable`,
  `status_code` and the existing sanitized `diagnostic` are projected separately
  from input/output. A trusted computer receipt can preserve uncertainty.
- A successful tool result followed by a journal/persistence error stays
  confirmed. An unknown dispatched outcome never receives automatic replay
  guidance. This feature does not change execution or retry policy.
- DeepAgents catalog records contain actual read-attempt counts. Native
  controller records contain **scheduled retry** counts from saved state;
  native SDK records use their own recovery counter. Missing records mean
  “No recovery attempt recorded”, not proof that none happened.
- Gateway model-metadata failures keep the API's 503 wrapper separately from
  the factual upstream status, safe code and correlation IDs. A recorded
  upstream 401/403 is terminal for preparation/startup recovery. The explanation
  identifies metadata access and asks an administrator to verify credentials;
  it does not substitute context-limit configuration advice for that rejection.
- `phase=slack_attachment_read` is optional. Its allowlisted code/stage,
  byte counts and bounded `attempts` are accepted without a producer import.
  `attempts` is a **read-attempt index, not an HTTP-request count**. Missing
  observed bytes are never replaced with zero. Producer prose and raw history
  are not copied into the explanation.
- Existing `stage=capture_sync,status=unconfirmed` means a capture completed
  but copying it into app storage was not confirmed. Copy recovery must not
  repeat the capture. New records retain the operation's original turn.
- A completed result with empty final text produces a typed
  `response_completion/missing_final_answer` observation at receipt time;
  native results are checked before fallback prose. This preserves the
  existing lifecycle outcome and does not rewrite historical “Ready” sessions
  by matching their text.

## Coverage of the twelve observed families

| Family | What can be established |
| --- | --- |
| Startup/connection/continuation | Recorded exception, stage, bounded recovery decision. Generic HTTP/timeout cannot establish infrastructure cause. |
| No final answer | Future completed-empty results are observed before fallback text; old untyped records remain unknown. |
| GitHub checkout | Known tool boundary and sanitized exception; `ValueError` is not a diagnosed checkout root cause. |
| Slack delivery | A failed `slack_send` receipt identifies delivery work; successful receipt is not proof of complete destination read-back. No partial-delivery inference from assistant prose. |
| Artifact paths | Known artifact operation; no guessed workspace-path violation from a generic error. |
| Capture persistence | Confirmed capture plus unconfirmed copy are separate outcomes. |
| Attachment reads | Optional typed Slack contract, validated byte facts and read-attempt index; size disagreement does not establish which source was wrong. |
| Private export | Typed existing export guard. Private export cannot be granted; a guard reached during ordinary answer saving needs product investigation. |
| Durable import | Typed missing-original-checkpoint guard. Do not clear an incomplete import. |
| Invalid provider request | SDK/provider category only. The rejected field is unknown unless separately recorded. |
| Provider policy | Structured policy code only; no policy inference from arbitrary provider prose and no bypass. |
| GitHub access | GitHub operation plus recorded 401/403; no guessed missing scope or permission grant. |

## Validation and real-app proof

Projection, native adapter, DeepAgents receipt, turn/privacy, and UI controller
tests are supplementary regression coverage. They do not prove live provider,
Modal, or production recovery.

For the service-level before/after proof, run the actual baseline and final app
with the scoped E2E launcher. One normal-app scenario uses a deliberately invalid
synthetic gateway key: the real gateway rejects `/model/info` with 401, while the
broker keeps its 503 contract. Before, terminal details lose the 401; after,
metadata stage and both statuses remain visible without revealing the key or
provider body. A separate healthy run with the configured gateway verifies the
normal path. Keep both runs isolated and use real gateway responses.

An optional guard scenario, if a real private-session identity is available,
uses an authenticated active private session to call
the actual `/broker/{run_id}/tools/call` endpoint for `media_share` with that
session's valid capability and request identity. Both versions must deny export.
The final version additionally records `PrivateExportDenied`, original turn,
tool stage, and rejected/non-dispatched action, visible in Activity and agent
diagnostics. No export or permission fallback should occur. Invoking the actual
service guard does not depend on model behavior; a model-initiated demonstration
must use a real configured gateway/model.

The parent delivery task owns this baseline/final local deployment, recording
and screenshot. A seeded local UI or scripted model is supplementary only.
