# Cloudflare Access boundary

Existing-product mode retains Moyai's canonical Cloudflare Access verification
and optional employee identity handoff. Preserve the deployed tunnel, routing,
audiences and provider policies when upgrading the runtime. The application
release does not provision Cloudflare resources or move the origin.

Employees reach Access, then the existing tunnel and private API origin. Moyai
verifies the signed `Cf-Access-Jwt-Assertion` at the origin; the tunnel alone is
not identity verification. The private origin must not have a public bypass.
Keep its configured public HTTPS origin in `MOYAI_PUBLIC_URL`.

| Setting | Purpose |
| --- | --- |
| `CLOUDFLARE_ACCESS_TEAM_DOMAIN` | Trusted team's issuer and signing-key source |
| `CLOUDFLARE_ACCESS_AUDIENCE` | Employee application audience |
| `CLOUDFLARE_ACCESS_BROKER_AUDIENCE` | Separate machine/broker audience |
| `CLOUDFLARE_ACCESS_CLIENT_ID`, `CLOUDFLARE_ACCESS_CLIENT_SECRET` | Existing machine service credential |
| `CLOUDFLARE_ACCESS_LOGIN` | Optional verified employee identity handoff |
| `TUNNEL_TOKEN` | Connector credential, on the tunnel service only |

Retain the company identity-provider restriction and allowed-domain policy.
Machine assertions cannot become employee accounts. With identity handoff,
Moyai preserves an unambiguous existing account and pins the verified issuer and
subject. Conflicting identities fail closed. Saved roles remain authoritative;
CSRF, current authorization and session visibility checks still apply.

Keep webhook exceptions exact. Existing-product Slack retains `/hooks/slack`,
`/hooks/slack/events` and `/hooks/slack/interactions`; requests still require
Slack signature verification. Automation exceptions apply only to configured
individual webhook paths. Never bypass Access for all `/api/*` or `/hooks/*`.
Provider OAuth callbacks and all new paths need their actual deployed policies.

During the maintenance release, preserve both source and candidate routing.
After activation verify an approved employee reaches the same account, a missing
or wrong-audience assertion is rejected, an unapproved identity is blocked,
and historical sessions and authorized downloads still open. Verify webhook
and machine callers independently of browser login. A valid browser session
does not prove service-token routing.

The local product authentication suite uses synthetic signed identities. It does
not certify a live Cloudflare policy or tunnel. Follow [production deployment](production-deploy.md)
for application upgrades and Cloudflare's [Access documentation](https://developers.cloudflare.com/cloudflare-one/access-controls/)
for edge changes. Rotate connector and application service credentials separately;
do not regenerate product encryption keys when rotating edge credentials.
