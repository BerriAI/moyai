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

After confirming a real administrator login, an operator may disable shared
password login with `PASSWORD_LOGIN_ENABLED=false`. Preserve `SESSION_SECRET`,
`ENCRYPTION_KEY` and existing data; do not rotate them as part of consolidation.
Shared passwords do not identify an individual for personal data access.

Authorization is checked at admission and again at model/tool boundaries.
Approval does not override revoked permissions. Personal memory and credentials
remain private to their owner even when a viewer is an administrator. UI tests
use synthetic accounts; validate the actual identity provider during deployment.
