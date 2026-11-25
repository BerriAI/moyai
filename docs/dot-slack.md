# Your Dot in Slack

Open **Your Dot → Slack notifications → Enable Slack notifications** while
signed in with your individual account. Moyai verifies the Slack account against
that identity and opens a DM with the installed workspace bot.
Wait until opt-in reports success before sending the first DM; messages sent
during account verification can be rejected by the connection cutoff.
The verified private DM binding commits before the clock guard elapses, so a
message arriving during that wait cannot fall back to shared Slack chat.
Cancelling the HTTP request after that commit does not undo the setting; refresh
the settings to see its current state.

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

The verified DM destination stays pinned when identity linking is revoked or
disabled. New messages in that DM are rejected until its identity is restored;
they cannot fall back to ordinary shared Slack chat. Historical Slack receipts
and ownership checks run before creating a new Dot conversation. Signed edits
and deletions update the original owner-admitted turn; deleting queued input
stops that work.

## Deployment

Use the existing workspace bot with `chat:write`, `im:write`, `im:history`,
`users:read`, and `users:read.email`. Enable `SLACK_BOT_ENABLED`,
`SLACK_DM_ENABLED`, `SLACK_THREAD_CHAT_ENABLED`, and
`SLACK_IDENTITY_LINKING_ENABLED`; include the owner in `SLACK_SESSION_USERS`
and configure `MOYAI_PUBLIC_URL`. Normal Slack event subscriptions and signed
webhook routing must already be configured. The feature does not install an app
or change its OAuth scopes or webhook URL.

Native schema v32 adds the encrypted recipient preference, unique DM destination,
account binding timestamp, and the relation between each private thread and its
canonical Dot. Apply the registered additive
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
Dot reconciliation uses the existing `DeliverySchedule` for read throttling and
preserves the original dispatch claim timestamp.

An early thread reply can recover its exact root with one bounded history read.
Only a recent parent that plausibly belongs to an in-flight send can defer in
the existing signed `HookInbox`, for at most two minutes from the parent timestamp.
A visible unrelated parent, an expired unconfirmed parent, or a merely pending
outgoing message does not extend that window. An exact confirmed root can still
be reconciled later. An unconfirmed root never becomes a shared fallback
conversation.

The settings endpoint reports unavailable configuration, a stale identity,
pending delivery, uncertain delivery, rejection, and suppression distinctly.
Quiet or duplicate discovery results produce no outgoing message.

## Operator recovery after an account reassignment

A DM reserved for an old owner deliberately returns 409 when a newly verified
owner tries to opt in. Reassignment does not transfer private Moyai history.
Recovery requires an authorized operator:

1. Confirm the legitimate reassignment in the workspace's identity system.
   Have the new owner sign in and use normal notification opt-in; its verified
   account lookup must reach the specific “already connected to another Dot”
   conflict. Do not release a pin merely from a claimed email or Slack ID.
2. Follow [role quiescence](role-quiescence.md) to seal and drain **every
   coordinator**, including standbys, and prevent replacement/restart admission.
   Leave the API role available for verified opt-in. Wait for captured coordinator
   identities to report drained and for active HTTP sends and reconciliation
   calls to finish. An uncertain `sending` row is not necessarily an active call
   and must remain claimed. Keep coordinator admission sealed through the new
   owner's successful opt-in in step 4. Record the exact
   old owner ID, DM destination key, preference revision, and configured native
   schema. Do not print or decrypt recipient credentials.
3. In that native schema, lock the owner before the preference, matching normal
   opt-in, then conditionally release only the old preference.
   The following is a `psql` template using explicitly supplied variables:

   ```sql
   BEGIN;
   UPDATE :"native_schema".moyai_users
   SET auth_version = auth_version
   WHERE id = :'old_owner_id';
   -- Require exactly UPDATE 1. Otherwise ROLLBACK and investigate.

   SELECT owner_id, session_id, revision, destination_key
   FROM :"native_schema".moyai_dot_slack_preferences
   WHERE owner_id = :'old_owner_id'
     AND destination_key = :'destination_key'
   FOR UPDATE;

   UPDATE :"native_schema".moyai_dot_slack_preferences
   SET enabled = false, recipient = NULL, destination_key = NULL, started_ts = NULL,
       revision = revision + 1, updated_at = CURRENT_TIMESTAMP::text
   WHERE owner_id = :'old_owner_id'
     AND destination_key = :'destination_key'
     AND revision = :'observed_revision'::integer;
   -- Require exactly UPDATE 1. Otherwise ROLLBACK and investigate.

   UPDATE :"native_schema".moyai_deliveries
   SET status = 'suppressed'
   WHERE status = 'pending'
     AND identity = 'product-slack:' || turn_id || ':dot'
     AND session_id IN (
       SELECT c.session_id
       FROM :"native_schema".moyai_dot_slack_conversations c
       JOIN :"native_schema".moyai_dot_slack_preferences p
         ON p.session_id = c.dot_session_id
       WHERE p.owner_id = :'old_owner_id'
     );
   COMMIT;
   ```

   Preserve every session, conversation relation, Slack thread binding, and
   receipt. Leave `sending` rows claimed; never reset an uncertain send to
   `pending`. Record the recovery in the operator's normal audit log.
4. With Slack admission still paused, have the new owner opt in again through
   the normal API/UI, which freshly verifies the account and records new consent
   and a binding timestamp from Slack's HTTP Date, never the API host's clock.
   Opt-in adds a two-second guard to the provider's second-resolution clock and
   waits two seconds before returning success. This conservatively allows for
   small clock differences within Slack; send the first DM after success.
   Verify the destination is pinned to that owner before qualifying replacement
   coordinator capacity through the existing role-quiescence procedure; drained
   processes have no resume command. The timestamp rejects queued or late
   unbound messages from before this opt-in and survives pause/re-enable for the
   same account.
   A new DM must create a new private conversation for the new owner. Old Slack
   thread replies must remain rejected, and old Moyai conversations must remain
   owned by the original owner.
