# Your Dot in Slack

Open **Your Dot → Slack notifications → Enable Slack notifications** while
signed in with your individual account. Moyai verifies the Slack account against
that identity and opens a DM with the installed workspace bot.

Each new published Dot finding starts a separate Slack thread and a private Moyai
conversation. Reply in that thread to continue its conversation. A new top-level
DM starts another private conversation. The canonical **Your Dot** chat remains
the place for proactive findings and follow-up controls.

Only the published finding enters the new conversation. The observer's workspace,
transcript, and connection grants are not copied. Replies use the owner's normal
admission and permission checks, retain the signed Slack sender's provenance, and
can use the owner's private preferences and bounded history recall. Linking one
thread does not give other Slack conversations access to private history.

**Pause notifications** cancels unsent proactive messages and stops staging new
ones. Existing conversations and new owner DMs remain available. Pausing cannot
recall a message already in flight. Changes to the owner, verified account,
installation, or private session invalidate the corresponding routes.
Re-enabling does not backfill old findings.

## Deployment

Use the existing workspace bot with `chat:write`, `im:write`, `im:history`,
`users:read`, and `users:read.email`. Enable `SLACK_BOT_ENABLED`,
`SLACK_DM_ENABLED`, `SLACK_THREAD_CHAT_ENABLED`, and
`SLACK_IDENTITY_LINKING_ENABLED`; include the owner in `SLACK_SESSION_USERS`
and configure `MOYAI_PUBLIC_URL`. Normal Slack event subscriptions and signed
webhook routing must already be configured. The feature does not install an app
or change its OAuth scopes or webhook URL.

Native schema v32 adds the encrypted recipient preference and the relation
between each private thread and its canonical Dot. Apply the registered additive
expansion with the existing migration tooling. Native protocol 13 fences older
pollers that cannot interpret top-level Dot deliveries; drain them before this
cutover and before any rollback.

The explicit loopback-only combined local rehearsal runs the existing Slack
inbox, delivery, and profile services alongside its native worker. Distributed
deployments continue to start those services under `CoordinatorHandoff`.

## Delivery and recovery

Publication and outbox insertion share one database transaction. `Delivery`
remains the only outgoing queue. Its existing claim, rate-limit schedule, and
uncertain-send reconciliation own retries.

A successful post and a reconciled post use the same transaction to save the
receipt and bind the Slack root. Reconciliation requires the exact expected bot
and delivery metadata. Missing or ambiguous history never causes a repost.

An early thread reply can recover its exact root with one bounded history read.
Until delivery is confirmed, the existing signed `HookInbox` retains the reply
for retry and applies its normal expiry. An unconfirmed root never becomes a
shared fallback conversation.

The settings endpoint reports unavailable configuration, a stale identity,
pending delivery, uncertain delivery, rejection, and suppression distinctly.
Quiet or duplicate discovery results produce no outgoing message.
