# Working updates in webchat and Slack

Webchat expands working updates while a task runs. Public parent commentary,
grouped tool activity and named subagent updates appear in the conversation.
Open an agent's details to inspect its history or its interactive child chat.
Private reasoning, tool arguments and tool results are not part of this feed.

A **Send now** correction starts a new working section. Earlier sections stay
visible above that input. Only a saved, nonempty parent answer collapses the
answered section; stopped or failed work without an answer stays expanded.
Saved activity remains available after reload. The feed belongs to the original
requester, including in shared conversations.

The feed supports both historical SDK delegates and interactive child chats.
For a child chat it reads only the original delegated assignment and generated
answer continuations. Later unrelated private follow-ups are not copied into
the parent's feed. Activity is bounded and indicates when older entries were
omitted. Visible active conversations refresh persisted activity every four
seconds alongside the existing root text stream.

Slack reserves one bot reply for each admitted input segment. Public parent or
named child updates edit that reply, coalesced to at most one progress revision
every 30 seconds. Steering preserves the prior slot, starts a slot after the new
input, and routes the final answer into that latest slot—even after accepted
guidance becomes included in answer settlement. Older queued inputs promoted
with **Send now** use admission order.

Long Slack answers use multiple sections in the same message. Text beyond
32,000 characters links to the full saved answer in Moyai. Existing PR cards and
approval controls remain attached. Already queued overflow deliveries from an
older deployment retain their existing delivery behavior.

Stale unsent progress is suppressed. An external write with an unconfirmed
acknowledgement remains claimed until Slack reconciliation confirms it; it is
not reposted. Existing requester and installation checks still apply.
