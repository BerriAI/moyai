# Slack and web sessions

With Slack identity linking enabled, native startup uses the same verified
Slack-to-organization-account relationship as the product account service.
The personal web sidebar includes native Slack sessions that the linked account
owns, and Participated includes sessions where that Slack account contributed.
Every result still passes its current session and Slack audience checks.

The native profile refresher also checks eligible, previously unlinked senders
whose status is `pending_profile`. Existing sessions can therefore appear after
startup without another Slack message. Manual, conflicting, or ambiguous account
relationships are not overwritten.

## Continue a web session in Slack

Open an owned web session's action menu, choose **Continue in Slack**, review the
title and one-line context, and select **Send me a DM**. Moyai resolves the Slack
recipient from the signed-in account's verified organization email. The browser
does not supply a recipient, email, or Slack channel.

Reply in the new DM thread to continue the same session. Its ID, privacy setting,
conversation history, and account selections remain in place. Subsequent web
messages and assistant answers are delivered to that thread. The initial DM
contains a title, a short context excerpt, and the web link; old transcript
messages are not backfilled. This action is available for individual owners'
main web conversations, excluding delegated and swarm member sessions.

Repeated clicks reuse the saved DM. If Slack may have accepted a send whose
response was lost, **Check existing DM** searches the latest 100 DM messages for
its bot-authored session receipt instead of sending another copy. A confirmed
provider rejection can be retried. A changed installation, revoked identity,
changed account relationship, or unavailable owner prevents continuation and
delivery; reconnecting those invalidated bindings is not currently exposed.

## Configuration and rollout

- Enable `SLACK_BOT_ENABLED`, `SLACK_IDENTITY_LINKING_ENABLED`,
  `SLACK_THREAD_CHAT_ENABLED`, and `SLACK_DM_ENABLED`; include the recipient in
  `SLACK_SESSION_USERS`.
- The bot needs `users:read`, `users:read.email`, `im:write`, `im:history`, and
  `chat:write`, as well as its existing message event subscriptions.
- Apply the registered native schema migration before starting the new version.
  It adds durable DM link receipts and signed Slack turn authority records.
  The same expansion is registered for online migrations.
- Identity refresh runs in the native Slack coordinator. Local combined
  `moyai dev` rehearsals must host that coordinator lifecycle as well as the
  web and execution services to test refresh and delivery.

Backend tests use disposable PostgreSQL and deterministic Slack HTTP boundaries.
Browser tests use the actual React app with synthetic API responses. Actual-app
before/after validation must separately exercise the running application and a
real model when checking conversation continuity.
