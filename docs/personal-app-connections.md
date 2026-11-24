# Personal app connections

In an existing-product deployment, open **Settings → Connections → Your apps**,
choose **Connect**, and approve the provider's consent screen. Select the account
in a private chat. Users do not register developer apps or paste tokens.

This feature uses the deployment's existing app registrations. It does not
create a new OAuth client for each person. Each person receives a separate grant
and encrypted tokens. Organization connections remain independent.

## Existing services

| Provider | Shared app configuration | Callback |
| --- | --- | --- |
| Linear | Existing `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` | `/oauth/linear/callback` |
| Notion | Existing `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET` | `/oauth/notion/callback` |
| GitHub | OAuth client pair retained in the existing encrypted GitHub App registration; `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` are the fallback for an already registered app | `/oauth/github/callback` |
| Slack | Existing Personal Slack service and `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | Existing `/oauth/slack/callback` |

Linear and Notion delegate authorization, code exchange, refresh, verification,
and tool execution to `app.connectors.Connectors`. GitHub uses the same GitHub
App registration and its user authorization flow, with the existing GitHub
capability for personal tools. New GitHub App registrations retain the returned
client pair encrypted in `github_app`; older registrations may need their
operator to supply the same app's client pair.

Slack's existing Personal Slack component is also shown in Your apps. Its
storage, scopes, callback, renewal, account selection, and tools remain in the
existing service. This feature does not add a Slack MCP app or parallel Slack
token store.

The fresh standalone mode does not expose these deployment-managed personal
connections. Existing-product mode uses its individual work-account sign-in;
shared-password identities cannot authorize personal apps. These modes select
data and identity integration, not different agent engines.

## One-time operator setup

Use `PUBLIC_URL` (or the deployment's existing public-URL alias) plus each callback
path above, including any application path prefix. Reuse a Moyai-owned app.
Only an operator creates a registration if none exists. Provider workspace or
organization restrictions can still require an administrator's approval.

Linear uses its existing `read,write` user consent and S256 PKCE. Notion retains
the capabilities configured on the shared connection and the pages authorized
by the person. GitHub App user access is bounded by both the app installation
and the user's permissions; a classic GitHub OAuth App is not the intended
replacement for the existing GitHub App.

Validate recovered credentials before enabling them: confirm the app identity,
owner, permissions and callback; complete consent through an isolated real
Moyai deployment; verify a provider read and disconnect. An accepted client
authentication probe alone is not a successful end-to-end consent test. Keep
missing, empty, invalid or unverified pairs unconfigured.

Persist the existing source encryption key and separate native encryption key.
Apply native schema expansion `2026-11-23-personal-oauth-v27` using the normal
release/migration process before starting the changed application.

### Product-dev infrastructure

Product-dev's configuration lives in `BerriAI/litellm-ops` under
`helm/overlays/aws/litellm-dev/us-west-1/litellm-product-dev/moyai/`.
Follow its ConfigMap and ExternalSecret pattern. Shared app secrets belong in
AWS Secrets Manager, not Helm values, source control, or user settings.

For example, after validating and staging a Notion pair in
`litellm-product-dev/moyai/runtime`, a JSON6902 patch on the existing
`moyai-runtime` ExternalSecret can add:

```yaml
- op: add
  path: /spec/data/-
  value:
    secretKey: NOTION_CLIENT_ID
    remoteRef:
      key: litellm-product-dev/moyai/runtime
      property: NOTION_CLIENT_ID
- op: add
  path: /spec/data/-
  value:
    secretKey: NOTION_CLIENT_SECRET
    remoteRef:
      key: litellm-product-dev/moyai/runtime
      property: NOTION_CLIENT_SECRET
```

Use the same mapping for a validated Linear or GitHub pair when needed. Stage
the referenced properties before syncing the patch: missing properties can
prevent the ExternalSecret from reconciling. Use the usual reviewed GitOps
rollout for every application role; publishing application code does not
configure provider registrations or deploy these credentials automatically.

## Lifecycle and boundaries

- Ten-minute, single-use state is bound to the individual, login cookie,
  account version, provider, app identity and connection revision. A newer
  attempt or disconnect fences earlier callbacks, including an exchange in flight.
- Personal state has a distinct `personal-app:` prefix at the shared callback.
  Ordinary organization authorization and GitHub installation callbacks retain
  their existing handlers.
- A no-store callback landing page restores first-party browser context before
  authenticated completion. It never redeems a code or reads account data.
- Per-user tokens and pending PKCE material are encrypted in native tables.
  Shared app secrets stay in the existing deployment configuration or encrypted
  GitHub registration. Durable chat grants contain references, not tokens.
- Linear, Notion and GitHub personal grants are admitted only in private web
  chats, not Slack or automation surfaces. Existing Personal Slack retains its
  own established surface rules.
- Workers resolve current credentials before tool execution. Expiring tokens
  renew server-side; a database row lock serializes rotating refreshes across
  processes. Credentials without a provider expiry remain usable until revoked;
  a failed renewal asks the person to reconnect.
- Current workspace enabled/read-only policy is checked at dispatch. Personal
  writes require approval, retain prohibited-tool filtering, and are not
  automatically replayed after an uncertain result.
- Disconnect clears saved tokens and pending states and invalidates admitted
  grants. It does not revoke provider-side authorization; the person can also
  revoke Moyai in provider settings.

## Verification

Protocol and browser-fixture tests supplement actual-app/provider proof:

```sh
uv run pytest native/tests/test_personal_oauth.py native/tests/test_product_connections.py native/tests/test_github_publication_setup.py native/tests/test_api_contract.py
MOYAI_TEST_POSTGRES=postgresql://... uv run pytest native/tests/test_native_online_schema.py native/tests/test_native_additive_schema.py native/tests/test_product_schema_versions.py
npm run api:check
npm run typecheck
npm test
npm run build
node --test --test-concurrency=1 tests/browser/native_settings.cjs
```

For local before/after proof, use the supported existing-product local
rehearsal with isolated source/native PostgreSQL schemas, source/native keys,
an individual test identity, and `uv run moyai dev`. Run the same Connections
workflow on the baseline and final revisions. Verify real provider consent,
the private account appearing, a read, cancellation and disconnect. Document
substituted identity/bootstrap data and any missing provider prerequisites;
fixture responses do not establish successful provider authorization.
