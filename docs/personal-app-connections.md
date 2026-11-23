# Personal app connections

In **Settings → Connections → Your apps**, choose **Connect**, approve access on the provider’s website, and return to Moyai. No user-created developer app or pasted access token is required. The account appears in the connection picker for private chats; select it there or in General preferences. Shared chats do not receive personal credentials. Existing organization connections are independent.

## Deployment setup

Set `MOYAI_PUBLIC_URL` to the externally reachable HTTPS application URL, including its path prefix if any. Local development supports HTTP on localhost. Persist the deployment encryption key and private database; OAuth credentials and client secrets are encrypted at rest with the same application encryption service. Use the normal native schema migration/online expansion process to apply `2026-11-23-personal-oauth-v27` before starting the updated existing-product deployment.

| Provider | One-time Moyai operator setup | Callback path |
| --- | --- | --- |
| Linear | None: Moyai registers a public PKCE client with Linear’s official MCP server. | `/oauth/personal/linear/callback` |
| Notion | None: Moyai registers a public PKCE client with Notion’s official MCP server. | `/oauth/personal/notion/callback` |
| GitHub | Register a Moyai-owned OAuth app, set `MOYAI_GITHUB_CLIENT_ID` and secret `MOYAI_GITHUB_CLIENT_SECRET`. | `/oauth/personal/github/callback` |
| Slack | Register an eligible Moyai Slack MCP app, set `MOYAI_SLACK_MCP_CLIENT_ID` and secret `MOYAI_SLACK_MCP_CLIENT_SECRET`. | `/oauth/personal/slack/callback` |

For configured apps, register the full callback URL: `MOYAI_PUBLIC_URL` plus the path above. These are deployment credentials, never end-user settings. Slack/GitHub remain visibly unavailable until configured; users are not routed to a developer-console setup form.

Slack currently permits MCP access for internal or directory-published apps. A broadly distributed Moyai integration therefore also requires the appropriate Slack distribution approval. Workspace app restrictions may still require an administrator to approve an installation. GitHub organization policies can similarly restrict OAuth app access. Moyai cannot override provider policies.

The requested permissions are defined centrally in `oauth_providers.py`: Linear issue/project reads and writes; Notion’s MCP scope; GitHub repository/user access; Slack channel/conversation search, history, users and messaging. Moyai requires approval for tool writes and retains its prohibited-tool policy.

## Lifecycle and boundaries

- Authorization uses S256 PKCE and a ten-minute, single-use state bound to the individual user, current login cookie and account version. A newer attempt or disconnect invalidates older callbacks, including callbacks already exchanging a code.
- The provider callback first loads a no-store Moyai page, then navigates to the authenticated completion endpoint. This restores same-site browser context for Strict login cookies without relaxing their policy. The public landing page never redeems a code or reads account data.
- Tokens, refresh tokens and dynamic client material are encrypted in dedicated tables. Chat grants and durable tool payloads carry the connection reference, not provider tokens.
- Before tool discovery or execution, the worker checks the current private-chat authority and resolves a fresh token. Expiring tokens renew server-side; concurrent renewal is serialized to protect rotating refresh tokens. Providers that omit expiry keep the token until revoked. Failed renewal asks the user to reconnect.
- Disconnect removes saved tokens and pending states and invalidates admitted grants. It does not revoke the provider-side authorization; users can also revoke Moyai in their provider settings.
- Tool definitions are checked again before dispatch. Writes are not automatically retried after an uncertain result.

## Verification

Protocol tests use synthetic provider responses and supplement real provider verification:

```sh
uv run pytest native/tests/test_personal_oauth.py native/tests/test_api_contract.py
MOYAI_TEST_POSTGRES=postgresql://... uv run pytest native/tests/test_native_online_schema.py native/tests/test_native_additive_schema.py native/tests/test_product_schema_versions.py
npm run api:check
npm run typecheck
npm test
npm run build
npm run test:ui
```

For a real local test, start the application with `uv run moyai dev --host 127.0.0.1 --port 8796`, an isolated data directory, workspace login, a configured gateway and `MOYAI_PUBLIC_URL=http://127.0.0.1:8796`. Connect Linear or Notion through the actual consent page, return to Your apps, and check that a read through the selected personal connection succeeds. Repeat cancellation and disconnect. Never substitute fixture responses for provider consent evidence.

Provider references: [Linear MCP](https://linear.app/docs/mcp), [Notion MCP](https://developers.notion.com/docs/get-started-with-mcp), [GitHub OAuth](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps), [GitHub MCP](https://github.com/github/github-mcp-server), [Slack MCP](https://docs.slack.dev/ai/slack-mcp-server/).
