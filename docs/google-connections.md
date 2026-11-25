# Personal Google connections

Users open **Settings → Connections → Your apps**, click **Connect** for Gmail
or Google Calendar, choose their Google account and approve read-only access.
Personal Gmail and Google Workspace accounts use the same flow. Workspace
administrators can separately restrict third-party app access.

**Use proactively** is visible before consent and defaults on. After connection,
Moyai creates or updates the owner's existing Dot discovery automation. It
preserves existing priorities, repository restrictions, schedule and explicit
pauses and retirement. Connecting or disconnecting an account does not restart
retired attention; the owner must explicitly migrate and activate it again.
A new attention automation checks hourly, with its first scheduled
minute approximately one minute after connection. Synchronization is durable
but asynchronous; Settings reports pending, active, paused or unavailable.

The same proactive accounts are available when the owner talks directly to
Your Dot, including private Dot Slack threads. Normal chats still select
accounts explicitly. Generated jobs and delegated agents retain their exact
admitted grants. Turning proactive use off removes the default Dot selection
and scheduled access; an explicit selection can still read the connected
account in a private chat.

Disconnect removes saved tokens, cancels unfinished consent and advances
connection/schedule authority. In-flight results must recheck authority before
release. Google grants do not authorize shared Slack conversations.
Revocation commits even when remaining accounts exceed the discovery limit or
the saved conversation is unavailable; attention pauses for repair.

Private read-only Google OAuth credentials stay on the trusted worker; they are
never installed in the computer. Adding, replacing, opting out of or disconnecting
these accounts therefore preserves the same saved computer and its files, for
both automatic Dot selections and explicit private-chat selections. This follows
the existing separation between computer identity and host-executed account
access. Other selected accounts still retain their computer authority checks.

Tool access, private conversation history and memory remain bound to every
selected account and its current revision. Reusing the computer cannot revive
an old grant or release an in-flight read after revocation. Disconnecting an
account does not delete information already saved into the owner's files.
The Google computer exemption does not apply to writable connections, shared
execution authority or Slack channel authority; private Dot Slack threads use
the owner's private web authority.
Existing computers without Google grants keep their exact scope key. This
change does not rewrite stored computer keys from earlier Google preview builds
or reset their files; those preview keys require explicit compatibility handling.

## One-time deployment setup

A user does **not** create a Google Cloud project. The operator registers Moyai
once, as the OAuth application requesting access. This is independent of the
Google application used to sign into Moyai.

1. In an operator-owned Google Cloud project, enable Gmail API and Google
   Calendar API. Configure the Google Auth Platform audience/branding for Moyai.
   Use an external audience when personal Google accounts should connect.
2. Create an OAuth **Web application** client. Register these exact redirect
   URLs, using the deployment's `MOYAI_PUBLIC_URL` including its path prefix:
   - `<public-url>/oauth/gmail/callback`
   - `<public-url>/oauth/google_calendar/callback`
3. Configure `GOOGLE_APPS_CLIENT_ID` and `GOOGLE_APPS_CLIENT_SECRET` through the
   installation's existing secret/configuration mechanism. Never put the secret
   in browser assets, source control, a chat message or a user's settings form.
4. Configure the requested scopes and Google test users while testing. Google
   determines verification requirements for a public launch; Gmail read-only is
   a restricted scope. Testing-mode refresh-token lifetime can require renewed
   consent. Complete the applicable Google review before relying on public,
   long-lived access.
5. Exercise both consent callbacks against the real application and verify
   reads, refresh, proactive enrollment, account replacement and disconnect.
   Missing app registration leaves Connect disabled with deployment status.

Requested grants are OpenID identity/email plus:

| Connection | Data scopes |
| --- | --- |
| Gmail | `https://www.googleapis.com/auth/gmail.readonly` |
| Google Calendar | `https://www.googleapis.com/auth/calendar.calendarlist.readonly`, `https://www.googleapis.com/auth/calendar.events.readonly` |

Moyai uses `google-auth-oauthlib` for S256 PKCE, code exchange and offline token
refresh, and the official Google API Python client for reads. The existing
owner-scoped OAuth store encrypts tokens; authorization state is single-use and
fenced by owner/session/client/revision. Pending authorization has a separate
generation from the working connection: starting or cancelling a reconnect
preserves the current grant, while a superseded consent response cannot replace
newer credentials. Only a successful replacement advances live authority.
Verified Google subject plus client ID
identifies the account, so refresh preserves attention history while switching
accounts does not inherit another account's tracked items.

## Runtime and limits

There is one canonical discovery automation per owner. Google accounts join
that automation through the existing product writer, captured account context
and Temporal reconciliation. No provider-specific scheduler or token database
is introduced. A second Google connection updates the same definition. Enrollment
uses the automation service's shared definition and binding transitions, including
context capture, generation fencing and durable synchronization intent.

The initial implementation performs scheduled bounded reads. It does not use
Gmail push, Calendar watch channels or incremental sync tokens. Email bodies,
pagination and Calendar time windows preserve explicit incomplete coverage.
Missing items never prove that a commitment is resolved. Background research
has read-only tools and cannot send email or edit meetings.

Existing owners without an activated attention binding must activate it before
proactive use starts. Installations without the automation service support
private chat reads but show background attention as unavailable. Attention
delivery still uses the existing Dot conversation/notification configuration.

This release advances schema to v34 with an additive, nullable pending-consent
generation on OAuth credentials, and advances native execution protocol to 15.
Drain incompatible participants before admission; do not overlap older workers
with Google grants. Follow the installation's existing release controller.

## Adapter choice

Google's official SDKs own OAuth, API transport and bounded transient-read
retries. Maintained Beautiful Soup/html5lib parsing handles email HTML; Moyai
owns only its account authority, bounded tool results and evidence normalization.
Authentication failures are distinguished from temporary provider failures.

Google also offers remote Gmail and Calendar MCP servers. They are not a drop-in
replacement for this version's evidence contract: the documented Gmail
`get_thread` tool has no bounded continuation input or thread history revision,
and Calendar `get_event` does not document an ETag output. Revisit the transport
when those contracts support the bounded reads and exact source revisions Dot
uses. MCP still requires operator Google Cloud/OAuth configuration and user
consent; it does not remove the application registration prerequisite.

Local lifecycle proof can seed synthetic connected accounts and exercise the
real application endpoints without exchanging credentials with Google. This
does not verify live Google consent, API reads, token refresh or Google-driven
proactive delivery; those require a configured OAuth application and real consent.

See [Gmail adapter](gmail.md), [Calendar adapter](google-calendar.md), and
[Dot discovery](dot-discovery.md) for evidence bounds.

## Provider setup references

- [Create OAuth credentials](https://developers.google.com/workspace/guides/create-credentials)
- [Configure OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Google web-server authorization](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [OAuth best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)
- [Gmail MCP configuration](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server)
- [Gmail MCP get_thread](https://developers.google.com/workspace/gmail/api/reference/mcp/get_thread)
- [Calendar MCP get_event](https://developers.google.com/workspace/calendar/api/reference/mcp/get_event)
