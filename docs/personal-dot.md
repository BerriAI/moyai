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

Personal GitHub, Linear, Notion, Gmail and Calendar OAuth accounts can opt into
automatic attention. Native MCP and LiteLLM MCP accounts can opt in while saving
the connection: the server discovers declared read-only tools before the write
transaction and saves the selected names as `attention_tools`. Initial consent
can select the current read catalog; later edits preserve or narrow that selection
without adding newly advertised tools. Discovery failure or an empty read catalog
fails the proactive save with a clear error.

Attention intersects those names with ordinary connection permissions and current
read-only tool annotations. It cannot execute writes, including previously
approved writes. Ordinary chat keeps its original grant, allowed tools and approval
policy; opting in never converts a writable chat connection into a read-only one.
Gmail and Calendar remain read-only. Historical manually configured read-only
connections with explicit `allowed_tools` retain their scope.

Personal Slack can opt in during OAuth, limited to search, channel and thread
reads. The one-use OAuth state binds the choice, and the callback enrolls only its
exact saved personal account. An omitted choice preserves existing consent without
opting in an unselected account. A separate `attention.consent_revision` fences
Slack consent updates and pending OAuth choices; clients send it as
`consent_revision` with attention edits or explicit OAuth choices. It does not
change the canonical grant revision. OAuth reconnect and re-enable preserve
previously narrowed read selections, including an empty selection after reduction.
Organization access never substitutes for a
revoked personal account. Shared workspace connections, including canonical
GitLab, are excluded. Existing accounts do not opt in automatically.

Consent changes persist with the existing automation's generation and sync intent.
Disable, disconnect and permission changes fence stale executions. Owner-paused
or retired attention stays paused or retired. Discovery can enroll up to 30
accounts in one schedule. Each occurrence checks at most four, rotating fairly
through the sorted pool using persisted admission history. Retries retain the
same selection; skipped admissions do not advance rotation, while failed admitted
checks do. Deferred accounts make a successful scan partial, and the limit remains
12 research tool calls per occurrence. Adding a fifth account does not pause the check.
Standalone installations report that scheduling is unavailable.

The persisted purpose scope requires native protocol 16 and is not compatible
with overlapping protocol-15 readers. Follow the maintenance compatibility and
rollback requirements in [deployment guidance](pydantic-deployment.md).

Conversational setup preserves the requested account subset. With multiple
selected accounts it requires an explicit subset rather than defaulting to all.
Edits may retain existing accounts without selecting them again, but adding an
account requires that account's current turn grant.

## Verification

`test_connection_attention.py` covers provider opt-in and revocation, unchanged
ordinary write approval, read-only execution, dynamic discovery and exact personal
Slack consent on disposable PostgreSQL. External provider implementations in that
suite are substituted; it is supplementary to actual-app verification.
`test_connection_attention_publication.py` exercises actual publication, listing,
owner controls and canonical Slack revocation on PostgreSQL. Canonical follow-ups
use the additive v35 table; native follow-ups keep their historical table and
foreign key. Neither storage path grants access without current owner consent.
`test_personal_dot.py` covers identity, quiet checks, account isolation, source
validation, deduplication, overlapping replies, revocation and the actual Temporal
execution path. `test_dot_automations.py` exercises canonical admission and
captured grants on disposable PostgreSQL. Inference tests cover the new agent's
authorization and retry accounting. Browser checks exercise the real React app
against synthetic APIs, including 1440, 768 and 320 pixel layouts.
`test_attention_batches.py` covers bounded rotation, retries and deferred-account
revocation. `test_dot_provider_records.py` checks native Slack/Notion response
shapes and verifies that quoted content stays with its own identity and revision.

For connection enrollment, run the same workflow through the baseline and final
applications with disposable PostgreSQL and Temporal. Enable proactive use on
saved personal OAuth accounts, then save a personal MCP connection whose catalog
offers both read and write tools. Check that one ready schedule is persisted,
ordinary chat retains its approved tools, and a real configured model can read
through the narrower attention grant without a write call. Adding a fifth account
must keep that schedule active and rotate saved admissions through every account.
Synthetic provider credentials and records verify enrollment and execution
boundaries; they do not verify a provider's OAuth exchange, real inbox contents
or external notification delivery.
