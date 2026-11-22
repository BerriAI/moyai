# In-chat connection guidance

Ask a private chat: “Which connected apps or accounts are available, including
ones not selected for this message? Which are selected, and how do I enable one?”
The read-only `connection_list` tool returns saved account labels, provider,
read-only and current-turn selection status, plus fixed first-party Settings links.
It does not test provider health.

To use an account, open **Settings → General preferences → Connections**, choose
**Choose connections**, check the account, and send a new message. Add or reconnect
accounts in **Settings → Connections**. Organization setup may require an
administrator. Disabled, disconnected, revoked, or ineligible accounts are omitted.

## Access boundary

- Both before the read and before release, the tool checks persisted current
  execution authority and the owner's private web session, human-origin evidence,
  and canonical individual identity. Shared-password identities are excluded.
- Shared, Slack, automated, delegated, swarm, and goal execution cannot enumerate
  accounts. Persisted delegation, swarm, goal, and Slack records also fence old
  payloads whose scope flags omitted provenance. These contexts get only generic
  guidance to open the user's own private web chat.
- Unselected accounts are re-resolved against current eligibility and revision
  before labels are released. A concurrent disable, disconnect, ownership or
  revision change returns a refresh instruction without account metadata.
- The canonical catalog uses its existing grant eligibility, with provider
  metadata refresh disabled. Native metadata queries and canonical grant
  generation are bounded; GitHub label queries target only the bounded IDs.
  Saved encrypted provider configuration still has to be read and parsed.
  Results contain at most 50 accounts and disclose truncation.
- Credentials, connection IDs, owners, endpoints, policies, and unselected tool
  schemas are excluded from the typed result. Labels remain untrusted data.
  No selection, connection, provider request, approval bypass, automation authority,
  or delegation authority is created.

## Reproduce on the actual application

1. Use the repository's `moyai dev` entrypoint on the baseline and candidate,
   each with an isolated product/native PostgreSQL database and Temporal storage.
   Configure a real development gateway/model and authenticated individual login.
2. Through Settings (or its authenticated `POST /api/connections` API), add a
   personal read-only MCP connection named **Orchid Research (synthetic MCP)**.
   Use safe synthetic test metadata and an operator-approved loopback origin.
   Listing saved metadata does not require a working MCP server.
3. Set **Choose connections** with no accounts checked, then ask the question
   above in a private chat. Baseline cannot see the unselected account; candidate
   identifies it as unselected and explains the Settings steps.
4. Ask the same question in a shared chat and as another authenticated individual
   in their own private chat. Neither answer may reveal the owner's personal label.
5. Disable the account through Settings and ask in a fresh owner private chat.
   The account is omitted and the response explains connection setup/recovery.
6. Confirm message admissions still contain `connections: []`, preferences and
   connection revisions are unchanged by discovery, and the configured test
   endpoint receives no requests.

The delivery recording and screenshot show the actual app with a real model.
Synthetic identities and connection metadata, plus local Moto object storage,
are test substitutes. This proof does not establish OAuth or live MCP/provider
operation. Unit/Temporal/PostgreSQL tests supplement the actual-app proof.
