# Working updates in webchat and Slack

Webchat expands working updates while a task runs. Public parent commentary,
grouped tool activity and named subagent updates appear in the conversation.
Open an agent's details to inspect its history or its interactive child chat.
Private reasoning, tool arguments and tool results are not part of this feed.

A **Send now** correction starts a new working section. Earlier sections stay
visible above that input. Only a saved, nonempty parent answer collapses the
answered section; stopped or failed work without an answer stays expanded.
Saved activity includes history continuations and remains available after reload.
Work resumed after a login or approval has its own activity section, even without
a new user message. The current action label follows the saved running action;
catching up on old streamed events does not replay them as current work.
Everyone who can read a conversation can see its activity, including live
updates, saved tool details and the original delegated assignments in shared
conversations. Losing conversation access also removes access to its activity.

Expand an action count, such as **11 actions**, then select a row to open its
input and output in the right panel. The selected action updates while it runs;
**Refresh action** reloads its saved details. The same panel opens actions from
subagents and side chats. Credential, memory and capability-loading payloads
remain private. Long inputs and outputs show a labelled preview of the first
100,000 characters.

The feed supports both historical SDK delegates and interactive child chats.
For a child chat it reads only the original delegated assignment and generated
answer continuations. Later unrelated private follow-ups are not copied into
the parent's feed. Opening a child's full chat still requires access to that
conversation. Activity is bounded and indicates when older entries were
omitted. Visible active conversations refresh persisted activity every four
seconds alongside the existing root text stream.

Slack posts its first bot reply only when an admitted input segment has real
commentary or an answer. Later public parent or named child updates edit that reply, coalesced to at most one progress revision
every 30 seconds. Steering preserves the prior slot, starts a slot after the new
input, and routes the final answer into that latest slot—even after accepted
guidance becomes included in answer settlement. Older queued inputs promoted
with **Send now** use admission order.

Slack's native **“Moyai is working…”** indicator replaces canned received, ready,
working and saving replies and the eyes reaction. It reflects queued, starting, running and saving work, refreshes every
minute, and is reasserted after attempted or confirmed bot replies. Steering
keeps the execution's indicator active while preserving the previous reply
slot. Completion, failure, Stop, pause, deletion and requests for user input
clear the working indicator even when no final text answer is produced.

The coordinator owns a separate bounded status loop, so a Slack status outage
does not block answer delivery. Its per-thread attempts and short claims are
durable: restart reconciles the current state, and ambiguous status calls may
retry without replaying chat messages. Destination and authorization are checked
again after refreshing the bot token. A removed or replaced bot connection
cannot send a cleanup request; Slack's two-minute expiration removes its status.
Unreachable destinations retire their saved status and retry metadata, so idle
threads leave the polling queue. Active work remains eligible when its connection
returns. Transient provider or database failures retain the saved retry state.
Only generic lifecycle text is sent, never private task focus or tool content.

Existing-product deployments require the additive
`2026-10-17-slack-activity-v19` application migration through the normal explicit
release workflow. It adds only the native status table and preserves every
earlier migration receipt. It does not change Temporal workflow commands.

Long Slack answers use multiple sections in the same message. Text beyond
32,000 characters links to the full saved answer in Moyai. Existing PR cards and
approval controls remain attached. Already queued overflow deliveries from an
older deployment retain their existing delivery behavior.

Stale unsent progress is suppressed. An external write with an unconfirmed
acknowledgement remains claimed until Slack reconciliation confirms it; it is
not reposted. Existing requester and installation checks still apply.

If an execution fails, Moyai attempts to save settled conversation and computer
state before releasing the workspace. A later authorized request can restore
that checkpoint. Unresolved external writes retain their review barrier. If the
checkpoint cannot be saved, the conversation shows a recovery warning.

Slack delivery notices distinguish failed file uploads from undelivered messages
and include a safe provider error code when available. The failure message does
not claim that a debug attachment was delivered before Slack confirms it.

Debug exports identify omitted teammate activity so their visible token totals
are not mistaken for the complete session.
