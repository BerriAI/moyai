# Your Dot

For broad discovery without per-item reminders, see [General proactive attention](dot-discovery.md).

**Your Dot** opens one private, persistent conversation per individual account.
Use it for ordinary work and ask it to keep track of a responsibility. Scheduled
attention is available in existing-product mode, using the same canonical
automation definitions, Temporal schedules, admission and model accounting as
other automations. Standalone installations provide the conversation and explain
that scheduled attention is unavailable.

## Attention and conversation

An attention definition has `kind: "attention"`, private visibility, a Dot
`session_id` as its **delivery destination**, explicit personal `connection_ids`,
calendar schedules and `environment_id: "none"`. It has no project, event trigger
or self-management authority. Existing `kind: "task"` definitions retain their
original digest and behavior. Native protocol 11 fences incompatible workers.

At enable/migration, the canonical service captures exact connection IDs and
revisions. Every occurrence executes in a fresh private automation session. The
observer receives the instructions, current time and the last 30 public Dot
messages (bounded to 3,000 characters each). It does not inherit the Dot's
computer, the accounts selected in a later reply, skills, memory-writing tools,
automation tools or delegation. Model execution is bounded to eight requests.

The observer returns a typed **quiet** or **notify** decision. Notify requires a
current connected-tool result with an exact account, source identity, provider
version or last-modified timestamp, and quotation from one structured record.
The host chooses a canonical URL/ID and version from that record and includes the
account in the deduplication key; changing a citation to an equivalent identity
or version field cannot bypass it. JSON decoding is limited to the tool payload
and supported MCP envelopes; text within provider records remains text.
Unsupported record shapes fail closed. Tool discovery results cannot serve as
evidence. Publication rechecks the owner, automation generation, original account
revisions and current private destination under the relevant transaction locks.
The assistant message and delivery receipt commit together. Repeated primary
account/source/version pairs are suppressed across occurrences; this is deterministic
item-version deduplication, not a claim of arbitrary semantic deduplication.

Quiet and duplicate checks leave the Dot's messages, activity, timestamps,
checkpoint and account selection unchanged. The observer's encrypted transcript
stays on its own execution turn. Normal replies use ordinary native admission
and action policies; generated notifications never become human authorization.
Delivery never mutates the saved SDK conversation or computer recovery digest.
It also leaves the computer revision unchanged, so delivery during a snapshot
cannot invalidate the capture. The message ID updates the transcript projection.
Pending notifications enter the next ordinary reply's prepared input. A cursor
saved atomically with each resulting checkpoint records which notifications it
contains, so overlapping deliveries survive a reply and enter the following
turn exactly once.

## Connections and permissions

Attention requires an enabled, owner-private, read-only native connection with a
nonempty explicit `allowed_tools` policy. Only allowlisted tools that also have
read-only annotations are exposed. Review the allowed operations and use
provider-enforced read-only endpoints/scopes where available. MCP annotations
alone are not treated as permission to expose an arbitrary tool catalog.

There is no first-party Gmail or Calendar adapter in this change. These sources
require an existing authenticated remote MCP connection with the above policy.
Ordinary chat connections and canonical organization connections do not
implicitly grant background monitoring access. Changing connection revisions
requires reviewing and enabling the schedule again. The same account may remain
selected for ordinary conversation without changing the captured observer scope.
Conversational setup preserves the requested account subset. With multiple
selected accounts it requires an explicit subset rather than defaulting to all.
Edits may retain existing accounts without selecting them again, but adding an
account requires that account's current turn grant.

## Verification

`test_personal_dot.py` covers identity, quiet checks, account isolation, source
validation, deduplication, overlapping replies, revocation and the actual Temporal
execution path. `test_dot_automations.py` exercises canonical admission and
captured grants on disposable PostgreSQL. Inference tests cover the new agent's
authorization and retry accounting. Browser checks exercise the real React app
against synthetic APIs, including 1440, 768 and 320 pixel layouts.

These tests supplement the separate baseline/final actual-app proof: a real
GitHub read-only MCP check posts into the Dot, a live model answers the user's
reply using that context, and a repeat check leaves the chat unchanged. Calendar
and email behavior must be verified with a suitable connected provider before
claiming those integrations work.
