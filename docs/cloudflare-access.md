# Cloudflare Access and a private Render origin

This is an opt-in rollout guide, not a claim that an existing deployment is protected. The normal `render.yaml` is unchanged. Review and apply `deploy/render-private.yaml` as a separate, staged replacement; it creates a private service with a new disk and a tunnel worker. Review current Render pricing before creating either service.

## Request boundaries

Employees reach Cloudflare Access, then an outbound Cloudflare Tunnel, then Moyai on Render's private network. The private service has no public `onrender.com` endpoint. Sandboxes reach the same hostname using a broker-only Cloudflare service token plus Moyai's existing per-run bearer token. No VPN client is needed for browser access.

Moyai also verifies the signed `Cf-Access-Jwt-Assertion` at the origin. A public-origin URL, forged email header, or existing Moyai login cookie cannot bypass this check. By default, Access is an additional gate before Moyai sign-in. With `CLOUDFLARE_ACCESS_LOGIN=true`, the verified employee assertion signs the person into Moyai directly. Roles, run ownership, Slack signatures, and run-token expiration remain enforced. It does not restrict sandbox outbound networking, remove the app's ability to decrypt stored credentials, or prevent an authorized agent from misusing a granted tool. Keep those credentials scoped and budget-limited.

## 1. Prepare Cloudflare without changing the live origin

Use the account that owns the chosen domain. Enable Zero Trust, review its terms and any billing authorization, and choose the team's `*.cloudflareaccess.com` hostname. The example application hostname below is `moyai.litellm-sandbox.ai`; replace it consistently if another hostname is chosen.

Use company Google SSO with a dedicated OAuth web client in a company-owned Google Cloud project whose audience is **Internal**. Add the team's `https://<team>.cloudflareaccess.com` origin and exact `/cdn-cgi/access/callback` redirect, store the client secret only in Cloudflare's identity-provider configuration, and enable PKCE. Cloudflare's Google integration supports sign-in without directory access; its Google Workspace integration additionally requires administrator authorization for group membership. Select only this Google provider on the employee application, keep the company-email policy, and retain Moyai's allowed-domain and administrator settings. Enable the optional identity handoff below to avoid a second Google sign-in. Confirm MFA enforcement in Google Workspace separately before describing the rollout as enforcing MFA. If One-time PIN is used as an interim login method, an email code alone is not an independent MFA factor.

Company-wide MFA verification is outside this rollout's scope and is not a cutover prerequisite. Leave any existing Google MFA policy unchanged; this rollout does not assert or enforce MFA for every employee.

Create these self-hosted Access applications **before** publishing the DNS/tunnel route:

| Application | Host and path | Policy | Origin check |
| --- | --- | --- | --- |
| Moyai employees | `moyai.litellm-sandbox.ai` (all paths) | Allow approved company Google identities; 8-hour session. No Everyone or Bypass rule. | Employee application audience; optional verified identity handoff, then Moyai authorization |
| Moyai broker | `moyai.litellm-sandbox.ai/broker/*` | **Service Auth**, include only the dedicated Moyai broker service token | Distinct broker application audience plus per-run bearer |
| Slack events | `moyai.litellm-sandbox.ai/hooks/slack/events` | Bypass Everyone for this path only | POST only; existing Slack signing-secret verification |
| Slack interactions | `moyai.litellm-sandbox.ai/hooks/slack/interactions` | Bypass Everyone for this path only | POST only; existing Slack signing-secret verification |

More-specific Access applications replace the parent application policy; they do not inherit it. Never put the broker service token in the employee policy. Record the two different application AUD values. Set a finite lifetime on the dedicated service token and record a rotation owner/date. Its secret goes only in Render's encrypted environment settings; it must not appear in repository files, CLI arguments, task specs, or logs. Sandboxes receive it through their execution environment; the token alone cannot authorize any Moyai run or open the dashboard.

Only when an automation actually uses an inbound webhook, add a separate Access bypass for that exact `/hooks/automations/<32-hex-id>` path (and exact named trigger path if used), and add it to `CLOUDFLARE_ACCESS_WEBHOOK_PATHS` as a JSON array. Keep the existing automation webhook authentication enabled. Never exempt `/hooks/*`, `/api/*`, or all traffic. OAuth callbacks remain under the employee application; add the new callback URLs at each provider before cutover.

## 2. Stage the private Render replacement

1. Review `deploy/render-private.yaml`. Keep it in the current Render workspace and Oregon region. The app uses one instance, 1 CPU / 2 GB RAM, and a 10 GB disk at `/var/data`; the separate tunnel worker starts at 0.5 CPU / 512 MB. Check its actual memory/throughput after rollout. The workspace dashboard showed $25/month for the app compute and $7/month for that worker size on October 8, 2026; disks, usage and temporary overlap with the old service are additional. Confirm the creation screen's actual total before provisioning.
2. Copy the current service's environment settings through the protected Render workflow. Preserve `ENCRYPTION_KEY`, `SESSION_SECRET`, Google configuration, sandbox credentials, Slack signing secret, object storage, model gateway, and Temporal settings exactly. If keys were generated as files, migrate those files too. Do not regenerate them.
3. Leave `RENDER_MIGRATION_STAGE=true`. Leave `BOOTSTRAP_MODAL_VOLUME` empty: the old Modal checkpoint is stale and must not replace current Render data. Keep auto-deploy disabled.
4. Set these values in the **private app** environment:

   ```dotenv
   MOYAI_PUBLIC_URL=https://moyai.litellm-sandbox.ai
   CLOUDFLARE_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
   CLOUDFLARE_ACCESS_AUDIENCE=<employee-application-AUD>
   CLOUDFLARE_ACCESS_BROKER_AUDIENCE=<broker-application-AUD>
   CLOUDFLARE_ACCESS_CLIENT_ID=<broker-service-token-client-id>
   CLOUDFLARE_ACCESS_CLIENT_SECRET=<broker-service-token-secret>
   ```

   Outside Render, use `PUBLIC_URL` instead of `MOYAI_PUBLIC_URL`. The three identity settings must be configured together; partial configuration refuses startup. Never enable the gate before all callers have their matching policies and credentials.
5. Deploy the reviewed commit to staging. Keep the Docker command empty for the app; its entrypoint prepares disk ownership and keeps the empty-database guard. The production Temporal worker must not start during staging. The new service initially serves only maintenance responses.

## Optional: one employee sign-in

After the employee Access application is restricted to the company Google provider and company emails, set `CLOUDFLARE_ACCESS_LOGIN=true` on the private app. Keep `GOOGLE_ALLOWED_DOMAINS` and `GOOGLE_ADMIN_EMAILS` configured; existing database role assignments remain authoritative. Separate Moyai Google OAuth credentials are no longer needed for login, but may be retained for rollback.

Moyai checks the signature, issuer, employee audience, expiry, subject, and allowed email domain on **every request**. It never trusts a plain forwarded-email header. Machine assertions, health checks and webhook exemptions cannot create employee sessions. Old Moyai cookies cannot override the current Access identity. Normal CSRF, origin checks and live role changes still apply.

At first login, the authoritative signed company email is matched exactly to one existing person account, preserving its ID, history, preferences, personal credentials and Slack links. The Access issuer and subject are then pinned to that account. An ambiguous email, changed email, or conflicting subject fails closed and needs administrator review. New employees receive distinct `cloudflare:` accounts. This relies on the company-only Google Access policy; do not add OTP, service tokens or unrelated identity providers to that application without reviewing the enrollment trust model.

Sign out clears the Moyai cookie and opens Cloudflare's logout endpoint. The next visit uses Access again; Google may reuse its existing session. Moyai does not add a second Google account picker. Access's eight-hour expiry still limits access even if a Moyai cookie has a longer lifetime.

To roll back the handoff alone, disable `CLOUDFLARE_ACCESS_LOGIN` and retain the origin gate and Google settings. Existing Google account IDs remain intact. Accounts created only through Access have distinct IDs; review their ownership before moving them to direct Google login.

## 3. Connect the tunnel

Create a named, remotely managed tunnel dedicated to Moyai. Store its connector token as `TUNNEL_TOKEN` on **moyai-tunnel only**; that worker needs no Moyai database, provider secrets, or application service token. Deploy the pinned official cloudflared image and confirm the connector is healthy.

Publish one route for `moyai.litellm-sandbox.ai` to `http://<private-service-internal-host>:10000`, using the exact internal hostname shown by Render's Connect menu. Set the origin HTTP Host Header to `moyai.litellm-sandbox.ai` for Moyai's host validation. Do not route to the old public Render URL. Keep the tunnel's default unmatched-route 404.

The Cloudflare edge-to-connector connection is encrypted. The final connector-to-app hop is HTTP on Render's private network in the same workspace/region; use an origin TLS proxy if your policy also requires TLS on that hop. Access policies protect the public hostname and Moyai validates the appropriate signed assertion per path. A blanket tunnel-level “Protect with Access” check for only the employee audience would break broker and Slack requests; do not enable that blanket setting for this mixed route.

## 4. Migrate current data and cut over

Plan a short maintenance window. Do not run two production app/Temporal workers against a copied database or the same live task queue.

1. Drain active tasks and inbound automation; stop new work. On a Temporal deployment, deploy this code to the old service with `MAINTENANCE_DRAIN=true` first. Active execution keeps running through its normal checkpoint; new launches and continuations wait at saved boundaries. Queued messages remain durable. Wait until every session is in `idle`, `warm`, `prepare`, `provision`, `waiting_environment`, `install`, `startup_wait`, `transport_wait`, `waiting_children`, or `waiting_credential` before stopping the old writer. No `launch`, `monitor`, `save`, `checkpointed`, or cleanup step may remain. Set `MAINTENANCE_DRAIN=false` on the replacement to resume. This switch requires Temporal. Inventory current sessions, connection counts, pending Temporal work, local files, and private object-storage references. Save a verified current backup using the [storage guide](object-storage.md), and preserve legacy archives and key files separately.
2. Stop every old writer, including the app's Temporal worker, then capture the **final** consistent current database and file set. Use SQLite's backup API to include committed WAL state, or checkpoint after all writers stop. Never copy just an active `workspace.db` and discard its WAL. Run disk operations as UID/GID 10001. Restore into the new disk through an authenticated private transfer and compare database integrity, counts, key decryption, and file hashes before startup. Do not put a migration archive on a public URL.
3. Add the new Google/Slack/Notion OAuth callbacks as applicable. Update Slack event/interactivity URLs and any automation webhooks at cutover. Existing saved browser bookmarks, external callbacks, and in-flight sandbox broker URLs must be accounted for; drain/rotate old sandboxes so the next run gets the new origin and credentials.
4. Start the private app with `RENDER_MIGRATION_STAGE=false`. The empty-database guard must pass with the restored database. Check `/health` internally with the expected Host header; it returns only a minimal status. Verify employee login, an existing session, a small real sandbox run, Git broker access, and a signed Slack event before admitting normal work.
5. Keep the old web service suspended so its public address cannot remain an alternate route. Preserve its disk for rollback until the new deployment and backups are verified. Do not delete it as part of the initial cutover.

## 5. Verify the live boundaries

- Anonymous public app request reaches the Cloudflare login/block page; an unapproved identity cannot enter.
- With the handoff enabled, a valid employee Access session opens the same Moyai account directly, without a second Google account picker. Existing roles and personal ownership remain intact. With it disabled, the separate Moyai sign-in remains required.
- Missing, forged, expired, wrong-issuer or wrong-audience origin JWT gets 401. Signing-key retrieval failure returns 503 and never opens access.
- Broker requires both the broker Access identity and an active run's bearer token. The machine assertion cannot enter `/api/credentials` or the dashboard, and an employee assertion cannot substitute for the broker identity.
- An unsigned Slack event fails; signed Slack delivery succeeds. Unconfigured webhook paths remain blocked.
- Broker relay, attachments and Git send Access headers only to the configured HTTPS broker and refuse redirects. SSE/long-running requests work through the actual tunnel.
- Public origin cannot be reached independently of the tunnel. Existing history, saved credentials, file downloads and a resumed task still work.

## Rollback and operation

Before any writes reach the replacement, rollback can suspend the replacement and restore the old routing/service. After the replacement accepts writes, stop it and transfer the latest consistent data back first; never resume from the stale old disk. Preserve keys and run only one production worker. Rolling back to the public service restores its earlier exposure and should be an explicit operational decision.

Rotate the broker and tunnel tokens separately; revoke the old token after replacement callers are verified. Restart/rotate reused sandboxes on revocation because already-running processes retain their previous environment. Exact credential redaction reduces accidental log/archive disclosure but does not make those processes a secret isolation boundary. Review Access logs, denied requests and membership regularly. Disabling Access requires intentionally removing the full configuration; partial configuration fails closed.

Local verification (synthetic identities, no Cloudflare account or production secrets):

```sh
uv run pytest -q tests/test_cloudflare_access.py tests/test_cloudflare_login.py tests/test_access_transport.py tests/test_render.py
uv run python -m scripts.cloudflare_access_demo
```

References: [Access applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/), [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/), [JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/), [Render private services](https://render.com/docs/private-services), [Render disks](https://render.com/docs/disks).
