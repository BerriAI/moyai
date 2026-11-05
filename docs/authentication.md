# Sign-in and user roles

Local trials use native accounts. Set `WORKSPACE_PASSWORD` before first start,
sign in as `admin`, and manage users in Settings. Passwords are hashed; role and
enabled-state changes invalidate prior authorization. The final enabled
administrator cannot be removed or demoted.

Existing-product mode uses the canonical Moyai identity directory, signing
secret, cookie and role records. It cannot bootstrap a replacement native admin.
Historical private ownership, personal credentials and Slack identity links keep
their original account IDs. Native projections are created only after canonical
authentication succeeds.

## Google and Cloudflare

Keep existing Google OIDC settings and `PUBLIC_URL/auth/google/callback` registered
at the provider. Google verifies issuer, audience, signature, expiry, nonce,
verified email and allowed hosted domain. `GOOGLE_ADMIN_EMAILS` supplies defaults
for identities without an explicit saved role; stored role assignments win.
Use the canonical Users page for role changes, which checks optimistic revisions,
last-admin protection and the current actor within the write transaction.

If Cloudflare Access is enabled, retain its existing audience, team and identity
policy. With `CLOUDFLARE_ACCESS_LOGIN=true`, Moyai verifies the employee assertion
on each request and maps it to the canonical identity. A plain forwarded-email
header or machine token cannot establish an employee session. See
[the edge boundary](cloudflare-access.md).

## Direct Okta sign-in

Existing-product deployments can also authenticate directly with Okta, including
when the UI uses `CLOUDFLARE_ACCESS_UI_MODE=private-network`. This does not change
Cloudflare routes or policies. Create an **OIDC Web Application** in Okta with the
Authorization Code grant and client-secret authentication (`client_secret_basic`).
Enable PKCE and assign only the intended users or groups. Register the exact
`PUBLIC_URL/auth/okta/callback` redirect URI, including any deployment base path.
Moyai requests only `openid email profile`; it does not need Okta API scopes,
refresh tokens, a browser client secret, or the implicit grant.

Set these values in the deployment's existing runtime secret:

```text
OKTA_ISSUER=https://your-company.okta.com
OKTA_CLIENT_ID=<web application client ID>
OKTA_CLIENT_SECRET=<web application client secret>
OKTA_ALLOWED_DOMAINS=example.com
OKTA_ADMIN_EMAILS=admin@example.com
```

The org issuer is sufficient for sign-in; a configured custom authorization
server issuer (`https://your-company.okta.com/oauth2/<server-id>`) also works.
Use the exact issuer without a trailing slash. The server must reach that
issuer's HTTPS discovery, token, JWKS and userinfo endpoints. All discovered
endpoints must use the issuer's origin. Tokens are signature/issuer/audience/
expiry/nonce checked, and userinfo must match the verified subject and contain a
verified email in the allowed domains. State is single-use, browser-bound and
bound to the Okta issuer/client; token exchange also uses PKCE.

The Google and Okta adapters use one OIDC flow for state, PKCE, token validation,
account resolution and session creation. Provider-specific settings select the
issuer, client credentials, endpoints and verified email claims.

Both buttons can open **the same Moyai account**. After signing in with an existing
Google or Okta account, open **Settings → Sign-in** and choose **Link Google** or
**Link Okta**. The existing sign-in must be at most five minutes old; sign out and
sign in again if prompted. Complete the other provider's sign-in using the same
verified work email in the same browser. Linking requires the original Moyai
session throughout the callback, a single-use browser-bound state, and the second
provider's verified identity. Shared passwords cannot link personal accounts.

A provider's issuer and subject are pinned to the existing account ID. Roles,
private conversations, personal credentials, memory, and usage remain on that ID;
no ownership rows or saved roles are rewritten. Subsequent sign-ins through either
provider resolve through the same account service. Saved email roles still win
over configured admin defaults. Linking changes the account's authority version,
so already-admitted work may require fresh authorization.

A matching email alone never links accounts. A first login that matches another
account asks the user to sign in through their existing method and link it. Changed
emails/subjects, different verified emails, ambiguous accounts, or providers that
already belong to another account require administrator review. Existing duplicate
accounts are not merged or deleted by this flow. No unlink or administrative merge
API is introduced. Legacy provider-specific IDs remain valid; they are not renamed.

The canonical source schema advances to revision 23 for pinned OIDC identities
and link intent on login states. Use the reviewed offline migration/upgrade flow
before starting this release; there is no new online migration path. Native
application tables and existing ownership references are unchanged. Cloudflare
Access mappings and its external app/policy configuration are untouched.

Logout ends the Moyai session, not the organization-wide provider session. Provider
unassignment does not immediately revoke an existing Moyai cookie; the existing
12-hour maximum session lifetime and local account/role checks still apply.

Keep password login enabled until a real Okta administrator login and access to
the required workspace data have been verified. This configuration does not
disable the password automatically.

After confirming a real administrator login, an operator may disable shared
password login with `PASSWORD_LOGIN_ENABLED=false`. Preserve `SESSION_SECRET`,
`ENCRYPTION_KEY` and existing data; do not rotate them as part of consolidation.
Shared passwords do not identify an individual for personal data access.

Authorization is checked at admission and again at model/tool boundaries.
Approval does not override revoked permissions. Personal memory and credentials
remain private to their owner even when a viewer is an administrator. UI tests
use synthetic accounts; validate the actual identity provider during deployment.
