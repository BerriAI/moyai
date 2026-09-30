# Moyai Devin

A working MVP of an internal Devin-style workspace: assign tasks in a browser, run Nous Research's Hermes Agent in an isolated Modal sandbox, and connect Linear, Slack, and Notion.

**Cloud workspace:** [Open Moyai Devin](https://moyai-devin.onrender.com), hosted on **Render**; Hermes sandboxes and filesystem snapshots run in the **litellm** Modal workspace. Sign in with your **@berri.ai Google Workspace account**. Shared-password login is disabled. Secrets remain private in Render and the ignored local `.env` file.

**Initial Slack verification:** a real @Moyai Devin mention in [#bot-spam](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790720764864289) created exactly one cloud run and returned a protected link on the original Modal deployment. The agent answered `Ready`, used no connected-app tools, and its sandbox was terminated. The new chat continuation path is described below.

**Chat verification:** real session [`e391ff74341d464a9674810584eae976`](https://moyai-devin.onrender.com/#run=e391ff74341d464a9674810584eae976) completed three replies. A follow-up queued during the first response remembered a phrase and changed the same file from 7 to 12. A fresh deployment preserved the four-message transcript, snapshot, and latest file archive. A third message sent from the reopened browser chat recalled the phrase and read the file as 12. All six chat messages are saved, no connected-app tools or approvals were used, and no sandbox was left running.

**Current verification:** the hosted cloud path works with `openai/gpt-6-astra` through `https://gateway.litellm-sandbox.ai/v1`. Live acceptance run `15c6ceef68724dc6897dce5639ef9350` created Python files, ran four unit tests successfully, opened and read a page through the real Chromium MCP tool, returned a downloadable archive and screenshot, streamed 20 activity events without decoding errors, and confirmed sandbox termination. The configured key's model catalog also includes `anthropic/claude-opus-5-5`; Astra is the current default.

Local/cloud sign-in, protected APIs, demo tasks, Docker startup/restart, and cloud history restoration after redeployment passed. The 142 automated tests cover approvals, OAuth state, model restrictions, answer preservation, checkpoint recovery, activity framing, cancellation, recovery archives, and Slack conversation/reaction routing using simulated providers. A real provisioning-cancellation check confirmed task cancellation, token revocation, and sandbox exit. Demo events are explicitly labeled and never execute agent code.

**Live app setup:** all three dedicated integrations are connected: Linear with Read, Create comments, and Create issues for LiteLLM-prod only (Linear’s Create issues scope also allows issue updates); Slack OAuth for BerriAI with search, channel/DM history, and posting scopes, with token rotation enabled; and Notion OAuth for LiteLLM with Read and Insert content, without editing existing content or user-profile access. At the user's request, Notion was granted every available page under Teamspaces, Shared, and Private, including their children. Notion only offers pages where the authorizing user has Full Access; this does not grant access to other Notion workspaces or guarantee access to future top-level pages.

Live integration acceptance run `16f17267a9f749cb8063a5aa5d38d76c` used real Hermes with Astra in a Modal sandbox. All six native calls succeeded: search and read for each of Linear, Slack, and Notion. The run created a downloadable report, requested no writes or approvals, and finished with zero active sandboxes. A final redeployment preserved all three connections, both completed acceptance runs, and their downloadable artifacts; unauthenticated APIs still returned 401. See [integration verification](../hermes-integration-verification.md) and [result archive](../hermes-integration-test.zip). Search results are paginated and large tool outputs can be truncated; this verifies connectivity and selected targets, not exhaustive search coverage or complete Notion content retrieval. External writes have not been exercised against real accounts.

A separate deterministic runtime check also passed: the real Hermes conversation loop consumed streamed tool calls, invoked Chromium through the actual workspace MCP bridge, returned a result, and cleaned up its sandbox. That check used a test model fixture, not an AI provider.

## Render web app with Modal sandboxes

**Live migration verified September 29, 2026:** all 12 existing sessions, 14 messages, three organization connections, saved filesystem snapshot IDs, Slack source context, and 10 byte-identical result archives moved to Render. All three provider health checks passed. The existing continuity chat resumed on a new Modal sandbox and recovered “blue lantern” and file value `12`. A real [#bot-spam thread mention](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790733863830609?thread_ts=1790733854.157109&cid=C0B302ZJU05) created [a Render session](https://moyai-devin.onrender.com/#run=b371989dcc8842fdad936f5784beec7f), automatically read two source messages, and answered the marker `river-stone-73`. No external writes were requested. Both sandboxes terminated. The old Modal web deployment is stopped; its Volume remains a frozen migration backup. Existing workspace passwords are unchanged. A second Render deployment, with bootstrap disabled, preserved all 13 current sessions, 18 messages, 11 archive checksums, saved snapshots, and organization connections. Unauthenticated APIs still returned 401, and no Modal sandbox remained running.

`render.yaml` defines one Render Starter Python web service in Oregon with a 1 GB persistent disk. Render hosts the browser UI, encrypted app connections, Slack webhook, SQLite history, and approval broker. Agent machines, filesystem snapshots, and Chromium still run in Modal. Service automatic deploys and Blueprint automatic synchronization are disabled because deployments interrupt active chat turns; check for active sessions before deploying. Manually sync the Blueprint after reviewing configuration changes, then deploy the intended commit. Keep one web instance. Render's disk forces stop-before-start deployments, preserving the single-writer database requirement.

The deployed Blueprint sets `RENDER_MIGRATION_STAGE=false` and leaves `BOOTSTRAP_MODAL_VOLUME` empty now that the import is complete. For a fresh migration, `render_start.py` defaults to staging mode unless explicitly configured. The health endpoint is available, but sessions and Slack events are refused until cutover. Configure the existing environment secrets privately in Render; preserve `ENCRYPTION_KEY`, both workspace passwords, and `SESSION_SECRET`. `PUBLIC_URL` comes from Render's own `RENDER_EXTERNAL_URL`; Modal proxy rewriting and Modal Volume checkpoint writes are disabled on Render.

For cutover, wait for every old session to settle, stop the old Modal web service cleanly, then set `RENDER_MIGRATION_STAGE=false` and deploy on Render. On the first live startup, `BOOTSTRAP_MODAL_VOLUME=hermes-workspace-state` copies the final SQLite checkpoint and its result archives onto the Render disk. It validates the database, rejects checkpoints with active runs, and publishes the database only after all archives transfer. Existing Render data is never overwritten. After a successful import, remove the bootstrap environment variable to prevent accidentally restoring stale data onto a replacement disk.

Update the Slack Events request URL and Slack/Notion OAuth redirect URLs to the new origin, then verify a real Slack mention and an existing chat follow-up. Keep the original Modal Volume as a migration backup. If reverting after accepting new work on Render, export the current Render database and artifacts first; the frozen old Modal checkpoint is no longer current.

## Start locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --workers 1
```

Open **http://127.0.0.1:8787**. Start a demo task, inspect its activity, or stop it. History survives server restarts in `.data/workspace.db`.

Values explicitly present in this project's `.env` take precedence over shell environment variables, including an empty value. This prevents accidentally using an unrelated globally configured gateway key. Container deployments do not include the `.env` file and use their configured environment/secret store.

Use exactly **one server process**. The MVP owns its job queue and runner in that process. Do not use multiple Uvicorn workers, multiple containers sharing the database, or development auto-reload while real runs are active.

## Google Workspace sign-in

**Live and verified September 29, 2026:** Google-only sign-in is enabled. A real `tin@berri.ai` login reached the existing workspace as an administrator; all 14 saved chats and three organization connections were retained. The previous password and previously issued password sessions were verified to return no access. The dedicated Internal OAuth app is in BerriAI’s `protean-chassis-510202-k5` project. The 74 automated tests pass.

Moyai supports Google OpenID Connect login restricted to configured Google Workspace domains. Create an **Internal** OAuth app in BerriAI’s Google Cloud organization, then a **Web application** client with the exact redirect URI `https://moyai-devin.onrender.com/auth/google/callback`. Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS=berri.ai`, and `GOOGLE_ADMIN_EMAILS=tin@berri.ai` privately on Render. Additional administrator emails are comma-separated. Other verified BerriAI identities become members and use the existing shared connections without individual provider authorization.

Keep `PASSWORD_LOGIN_ENABLED=true` for initial setup. After a real Google administrator login succeeds, set it to `false` and redeploy; this disables password login and invalidates existing password sessions. Preserve `SESSION_SECRET` and `ENCRYPTION_KEY` and existing data during the change. Domain, OAuth client, and administrator-policy changes are checked on subsequent requests. Google sessions last at most 12 hours; Google account suspension is checked on the next Google authentication, not through directory synchronization.

The server verifies Google’s signature, issuer, audience, expiry, nonce, verified email, and hosted-domain claim. Login state is single-use, expires in ten minutes, is bound to the initiating browser, and uses PKCE. Return destinations are restricted to known local app routes. Google login requests only `openid email profile`; Google API access and refresh tokens are not stored. Passwords, authorization codes, and identity tokens must not appear in logs or screenshots. Existing app permissions and administrator-only external write approvals are unchanged.

## Chat interface

The browser opens into a conversation workspace with searchable sessions in the sidebar, a centered new-session composer, and a full-height chat with the composer fixed below the conversation. Enter sends; Shift + Enter adds a line. Unsent follow-up drafts remain with their session while switching chats. Activity opens the session's progress, Slack source context, and file download in a collapsible panel. Pending write approvals remain visible beside the composer.

Assistant replies render Markdown headings, lists, links, tables, and copyable code blocks. The pinned local Marked and DOMPurify libraries are listed with their source tarballs and hashes in `app/static/vendor/versions.json`; licenses ship alongside them. Raw HTML is escaped, the resulting markup is sanitized, and only HTTP(S)/mailto links are enabled. No external script or image is loaded to format replies.

Browser acceptance covered desktop and a 390px narrow viewport, session search, multiline input, Enter-to-send, queued follow-ups, per-session drafts, live status, activity controls, and hostile HTML/URL rendering. The 49 existing automated tests pass.

## What is implemented

| Area | Behavior |
| --- | --- |
| Chat sessions | Start a conversation, send follow-ups while work runs, revisit saved messages, see live tool progress, stop a response, and download the latest files. Follow-ups queue in order; duplicate sends do not run twice. |
| Cloud execution | Dedicated 2 CPU / 4 GB Modal sandbox for each run; bounded concurrency; no app duration or iteration cap by default; automatic machine renewal; optional full VM runtime. The entire Hermes process runs inside the sandbox. |
| Hermes | Source pinned to commit `7968c72a3cb80beaae51948378944dd6e3423b96`; dependencies prepared through Hermes PM; terminal, file, and workspace MCP tools. |
| Model access | OpenAI Chat Completions through your LiteLLM-compatible gateway. The control plane pins the model, caps output and request count, and keeps the model key outside sandboxes. |
| Native connections | First-class Linear, Slack, and Notion cards; OAuth when app clients are configured; validated personal/integration-token alternative; encrypted token storage and OAuth refresh. |
| Organization controls | Shared connections, separate admin/member access, enabled/paused and read-only policies, health checks, and an audit history of connection changes. |
| Slack sessions | Mention @Moyai Devin in a channel the bot has joined. Signed, deduplicated events start one saved session per thread. AgentChat routes mentions, thread follow-ups, and direct messages into saved conversations. The agent reacts with eyes, then posts its answer. |
| External writes | Exact arguments appear for one-time admin approval. Denied/expired actions are not sent. Ambiguous write failures are recorded as uncertain and never retried automatically. |
| Agent browser | Isolated headless Chromium with open/read/click/fill tools over MCP; latest screenshot returned in the result archive. |
| Results | Summary, tracked changes as a patch, eligible new files, and latest browser screenshot. Up to 2 MB per artifact file / 15 MB collected content / 20 MB archive download. Hidden files and symlinks are skipped. |
| Saved workspace | Each response saves Hermes conversation history and a Modal filesystem snapshot. Later responses restore those files and tool history; idle sessions use no sandbox compute. Filesystem snapshots do not preserve running background processes or browser tabs. |
| Restart handling | Saved chats and workspace snapshots survive deployments. Unfinished responses/queued messages are interrupted, capabilities revoked, and known sandboxes cleaned up. Send a new message to resume from the last saved workspace; unfinished external actions are never silently replayed. |

## Enable cloud runs

The cloud sandbox calls back to this server for model and app tools, so `PUBLIC_URL` must be a **reachable HTTPS address**. Loopback URLs deliberately keep cloud execution disabled.

Set these in `.env` or your host's secret store:

```dotenv
PUBLIC_URL=https://your-workspace.example.com
WORKSPACE_PASSWORD=<at-least-16-characters>
MODAL_TOKEN_ID=<your-modal-token-id>
MODAL_TOKEN_SECRET=<your-modal-token-secret>
LITELLM_API_BASE=https://your-gateway.example.com/v1
LITELLM_API_KEY=<a-dedicated-budget-limited-key>
AGENT_MODEL=<your-gateway-model-name>
```

Also set stable `SESSION_SECRET` and `ENCRYPTION_KEY` in cloud deployments. The example file includes generation commands. If omitted, they are generated privately under `DATA_DIR`; preserve that directory together with the database.

Restart the server and check **Runtime**. Cloud mode becomes selectable only when configuration is complete. The first run builds and caches the Hermes image and may take several minutes. This readiness check confirms configuration, not successful authentication or a completed image build.

The default Modal sandbox uses container isolation. Set `MODAL_VM_RUNTIME=true` to opt into Modal's VM runtime beta when a workload needs a full Linux kernel. This flag does not install Docker or provision nested VMs for you.

### Deploy the control plane

The included `deploy_modal.py` hosts the browser app on a **Modal Server** in the same workspace as the agent sandboxes:

```sh
uv run python deploy_modal.py
```

The deploy command reads `.env`, generates missing workspace/session/encryption secrets privately, updates the dedicated `hermes-workspace-config` Modal Secret, and deploys `moyai-devin`. It prints the actual HTTPS URL. The app resolves that URL on startup and validates Modal's forwarded hostname against that exact origin. Keep one server container (`min_containers=1`, `max_containers=1`) and the `recreate` deployment strategy. The product name and host are Moyai Devin; internal secret/volume names retain the original `hermes-workspace` prefix to preserve credentials and history. When migrating to another app name, first stop the old app and verify its containers have exited before deploying a new writer against the same volume. This is an always-on service and incurs Modal usage while deployed; stop it in the Modal dashboard when no longer needed.

SQLite runs on the container's local disk. Complete database snapshots and the latest per-session result archives are committed to the `hermes-workspace-state` Modal Volume. API mutations are checkpointed before acknowledgement, and background activity is checkpointed every two seconds. A hard failure can lose the newest background events. Starting a replacement restores the last snapshot and interrupts unfinished tasks without replaying external writes. Do not scale the service above one container or use rolling deployments; a distributed worker/database design is needed for multiple writers. Redeployments interrupt active tasks.

The authorized Modal token expires on **October 6, 2026**. Sandbox provisioning uses this token. Replace it privately in the Render environment and local `.env`, then deploy when sessions are idle before expiry to keep cloud tasks working. Use a managed service identity and your own operational policies for a longer-lived team deployment.

### Alternative: Docker on an existing cloud host

The included Docker image runs the control plane on an always-on cloud VM or container host. Modal supplies the task sandboxes separately.

```sh
docker compose up --build -d
```

The compose port binds only to the cloud host's loopback interface. Place an HTTPS reverse proxy in front of port 8787, set `PUBLIC_URL` to its exact origin, and preserve the incoming Host header. Allow `/broker/` traffic from Modal with its run-scoped bearer tokens. Disable proxy buffering for event streams and allow requests lasting up to 16 minutes for human approvals. Set an appropriate body-size limit (5 MB) at the proxy.

Use one replica with a persistent local disk. Avoid serverless request hosts that stop background work after an HTTP response. The Docker image was built and its task creation and restart persistence were verified locally. This alternative has not been deployed to a separate VM.

## Connect the apps

New browser sessions select all enabled connected apps by default; uncheck an app to exclude it from that session. No per-user provider sign-in is required.

Connections are **shared by the LiteLLM organization** and retain the permissions of their authorizing identity. The deployed app uses verified BerriAI Google SSO; password sign-in is a configurable fallback. Members can start sessions and use enabled connections. Only admins can manage connections, change access policies, view spend, link Slack identities, or approve external writes. This is a single-organization trusted-team MVP with shared session visibility.

Open **Organization** and either enter the appropriate token or use the OAuth button after configuring the provider's client ID and secret. Tokens are checked with the provider before being saved. Disconnect removes the locally stored credential; revoke the integration at the provider as well if you want to terminate its authorization there.

| App | Required setup | Tools exposed |
| --- | --- | --- |
| Linear | Personal API key, or OAuth app with `read,write`; callback `PUBLIC_URL/oauth/linear/callback`. | List accessible teams (50 results), search issue titles (20 results), read an issue, create an issue or add a comment after approval. Creating issues requires the credential’s Create issues permission (Linear also permits updates under that scope). |
| Slack | User token with `search:read`, relevant channel/DM history scopes, and `chat:write`; or a Slack OAuth app with those **user** scopes and callback `PUBLIC_URL/oauth/slack/callback`. Bot tokens cannot search messages. | Search messages (20 results), read a thread (50 messages), send a message after approval. |
| Notion | Integration token with content access and the target pages shared to it; or public integration OAuth client with callback `PUBLIC_URL/oauth/notion/callback`. | Search page titles (20 results), read up to 100 top-level blocks, append a paragraph after approval. |

Set `LINEAR_CLIENT_ID` / `LINEAR_CLIENT_SECRET`, `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`, and/or `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` to show native OAuth buttons. Provider administrators may need to approve the apps and scopes. Notion search is title search, not full-text search; nested page blocks and subsequent result pages are not automatically expanded in this MVP.

This application registers its own app integrations. It does not reuse or copy credentials from the Codex/ChatGPT connectors in this chat.

## Choose the model

**Live verification:** session [`9d137408acbc4254a1c6fbbf7e86aa77`](https://moyai-devin.onrender.com/#run=9d137408acbc4254a1c6fbbf7e86aa77) ran **Opus → Astra → Opus**. Opus remembered `copper lighthouse` and wrote `21`; Astra restored the conversation/file, incremented it to `22`, and a queued Opus turn read `22` and recalled the phrase. The picker changed to Opus while the active turn stayed on Astra. All three answers have model labels, connected apps were deselected, and all three sandboxes confirmed termination. The `@Moyai Devin model opus` command was also verified in the existing #bot-spam test thread without starting compute.

Use the model picker in the new-session composer or below an existing conversation to choose **GPT-6 Astra** (`openai/gpt-6-astra`) or **Claude Opus 5.5** (`anthropic/claude-opus-5-5`). The choice applies when you send the next message and becomes that session's preference. Your conversation and saved workspace stay together across a model switch. Each new assistant answer records its model; old answers without a stored model are left unlabeled.

Every queued message captures its model at submission. Changing the picker or Slack preference does not reroute a response already running or queued. The gateway broker pins each turn to its selected, allowed model and ignores any model override supplied by sandbox code. `AGENT_MODEL` sets the default; `AGENT_MODELS` configures the picker allowlist. Gateway credentials must have access to each enabled model.

In Slack, mention the bot with `model opus` or `model astra` to set the model for that thread's next messages. You can start with a model directive on the first line and the task on the next line, for example:

```text
@Moyai Devin model opus
Read this thread and suggest the next step.
```

A model-only command starts or updates the saved session without launching a sandbox. Its confirmation does not change running or already queued turns. These commands also work in DMs. Include the bot mention on channel follow-ups if the installation has not yet enabled ordinary thread events.

## Per-user model spend

The proposed [monthly operating-cost view](docs/operating-costs.md) is gated by
`OPERATING_COSTS_ENABLED=false`. Its rollout is deferred until the team is ready
for paid-provider billing. It adds provider statements and an optional scoped
Modal export prefill; it does not change the live deployment or existing model
accounting. See the activation notes for credit handling and collection limits.

**Live verification (September 29, 2026):** deployed commit `87aa874` on Render. Session [`9895bd3b52a9403a988f4dc06c7545dd`](https://moyai-devin.onrender.com/#run=9895bd3b52a9403a988f4dc06c7545dd), started by `tin@berri.ai`, completed an Astra response, then an Opus follow-up that recalled `granite-27`, wrote a local file and read it back. Three successful inference responses supplied `x-litellm-response-cost`: `$0.061130000000000004`, `$0.037141`, and `$0.0042726`, totaling `$0.102543600000000004` (displayed as `$0.102544`). User, session and model totals agree. Both successful turns saved filesystem/conversation snapshots and terminated their Modal sandboxes. The admin UI exposed the exact header amounts; unauthenticated spend access returned 401 and the removed callback route returned 404.

Initial verification exposed LiteLLM's rejection of request-level `turn_off_message_logging`. Moyai no longer sends that option; gateway security settings were not changed. Seven rejected attempts remain visible as unknown cost, rather than assigning an invented zero. A separate minimal diagnostic request cost `$0.00029` on the same key, outside the Moyai session ledger. The gateway still has no key-scoped logging integration, and `allowed_routes` remains `llm_api_routes`. The full 110-test suite passed before deployment; all 22 spend/model tests passed after the logging-policy correction.

Administrators can open **Spend** in the sidebar to see USD model costs by user, session and model, with UTC date filters (up to 93 days). Google sign-in registers a stable account using the verified Google subject, so an email/name change does not create a different billing identity. New sessions record their creator; every queued message independently records its sender. A response, including its tool loops, retries and context compression calls, is attributed to that sender even when another teammate queues the next message. Conversations retain shared-workspace visibility; this adds ownership and accounting, not private chats.

Each model call is durably reserved before reaching LiteLLM, with the active message sender, session, selected model and existing key fingerprint. Sandbox-supplied attribution is ignored. All requests use the same existing gateway virtual key; SSO user/session accounting stays inside Moyai. No new gateway users, keys, callbacks or reporting permissions are required.

Moyai saves the finalized `x-litellm-response-cost` from the inference response, falling back to an explicit `x_litellm_response_cost` in final usage when present. These are gateway-reported costs, not token-price estimates. Token counts are retained for context; cache pricing and other gateway pricing rules are already reflected in its returned cost. Decimal costs are stored as strings and summed without binary floating-point rounding.

Because streamed HTTP headers arrive before generation finishes, the broker requests a non-streamed completion from LiteLLM. It saves the final cost and usage before returning that response, adapting completed text/reasoning/tool calls to the Chat Completions SSE protocol when Hermes requests streaming. Tool and session activity still update live; individual inference tokens are delivered together after completion. If a sandbox stops while an already-submitted inference completes, its returned cost is still recorded. Unknown costs remain visible, separate from explicit zero-cost responses.

User totals add up to costs returned for tracked Moyai requests; the dashboard shows coverage and expandable exact per-request amounts. This does not audit the key's entire lifetime spend: earlier usage, calls outside Moyai, gateway-internal billed attempts not reflected in the returned cost, and interrupted requests without a final response cannot be recovered from response headers. Tracking starts at rollout. Key rotation retains previous accounting rows but the page displays the currently configured key. Demo sessions incur no model spend. Render hosting and Modal compute/storage charges remain separate.

Slack messages capture the sender from verified Slack events. In **Spend**, an administrator can link observed Slack accounts to existing Google accounts; the teammate must sign in once first. Reports then combine Slack and web usage, while original sender IDs remain immutable and link changes are audited. Unlinked Slack accounts stay distinct, visible spend rows.

## Recovery from code-content connection failures

A production failure on LIT-6275 exposed an edge-firewall false positive: the first inference and Linear read succeeded, but the saved conversation containing the issue’s code/reproduction examples received Cloudflare HTML `403 Blocked` before reaching the Render app. Every follow-up restored that same context and failed again. An isolated copy of the actual snapshot reproduced this: plain JSON returned the expected capability `401`, while the saved conversation returned `403`.

The sandbox now runs an authenticated loopback adapter for Hermes and MCP. Requests cross the public edge as Fernet envelopes bound to a fresh run capability and exact route, with a five-minute validity window. Render authenticates the active run **before** decrypting and keeps its model allowlist, sender accounting, input validation, size limits, tool policies, and exact-action approvals. No gateway credentials enter the sandbox, no extra gateway key is created, and no firewall setting is changed. Restored sessions receive the updated adapter files, preserving user files and history. The exact previously blocked conversation passed the edge after this transport change.

Slack context is included once instead of being appended again on every follow-up. Failed assistant turns retain their failure status and appear as failures in Slack and the web UI. Connection failures give an actionable explanation instead of guessing about the user’s model API key. For issue follow-ups, the agent checks for an existing fix PR and states the GitHub publishing limitation when a new PR is requested.

**Verification:** 124 automated tests passed, including encrypted model/tool requests, unchanged gateway costs and identity attribution, tampered/expired/wrong-route rejection, revoked capability rejection, and continued admin approval requirements for writes. Commit `046c317` was deployed to Render. The actual failed conversation resumed from its saved snapshot, re-read the Linear issue, and identified merged PR [#38416](https://github.com/BerriAI/litellm/pull/38416), shipped in v1.100.0. Its answer arrived in the original Slack thread. A subsequent Slack reply without an issue identifier received the correct contextual answer in the same web/Slack session. The spend dashboard attributed the repaired calls to the sender’s linked Google identity. Both turns saved their snapshots and stopped their sandboxes; a Modal SDK check found zero active sandboxes under `hermes-workspace` after verification.

## Chat with Moyai in Slack

**Live reaction-first verification (September 30, 2026):** commit `bb7186e` deployed successfully to Render. The existing BerriAI connection was reauthorized with `reactions:write` and `assistant:write`; no additional history or file-reading scopes were added. Slack's native Agent experience is enabled and the new logo is installed. In the [#bot-spam test thread](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790788399573509), Moyai reacted with eyes to the initial mention and to an ordinary reply without another mention. It answered `Ready.` and then recalled `cedar-comet-91`, with exactly two answers and no acknowledgment or periodic progress posts. Both turns used the same [saved session](https://moyai-devin.onrender.com/#run=ab6034c09c3949d998e20156248c3361), which returned to idle with two model calls and a saved filesystem snapshot. The visible replies show the native **AGENT** badge and Moai logo. A Modal API check confirmed zero active sandboxes under `hermes-workspace` after both turns. See [live screenshot](../moyai-reaction-agent-live.jpg). Agent-panel thread isolation is covered by automated tests; this acceptance run tested a channel thread.

The app uses the custom Moai profile and Devin-inspired three-node mark in `app/static/favicon.svg`; a 1024px PNG for Slack is in `app/static/moyai-devin-logo.png`. The SVG is the editable source.

Moyai uses [BerriAI AgentChat](https://github.com/BerriAI/agentchat), pinned to `8790fe927029cb115dab17c5e14e3a0b699b3bcc`, for normalized messages, conversation locks, handler dispatch, and replies. Its stock Slack adapter uses Socket Mode and does not route ordinary channel-thread replies. Our public Channel/State adapter preserves the existing signed HTTP webhook, rotating OAuth credentials, SQLite receipts, saved sessions, and durable reply outbox. No Socket Mode app token is required. AgentChat does not grant Slack permissions; an administrator still installs the scopes and event subscriptions below.

**Live AgentChat verification (September 29, 2026):** commit `4b2e339` deployed successfully to Render. The approved `message.channels`, `message.groups`, and `message.im` subscriptions and matching bot history scopes were installed; App Home allows direct messages. All 117 automated tests, JavaScript/Python checks, a clean Docker build, and production dependency imports passed.

A [normal #bot-spam reply without a mention](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790745129700469?thread_ts=1790740160.956979&cid=C0B302ZJU05) continued the existing session `35f33e443f214d159978842947ed69d8`, recalled `cobalt-otter-58`, and recovered file value `12` after the deployment. A [direct-message session](https://moyai-devin.onrender.com/#run=07eca0ea76204f44be44afbcf38e73bb) started with Astra, saved `19`, then continued with Opus, recalled `amber-puffin-84`, and saved/verified `23`. Both responses appeared directly in the DM and linked to the same web session. Tin’s observed Slack identity was linked to `tin@berri.ai` for combined accounting. Existing 18 sessions and all three connections survived; only the new DM added a session. No connected-app writes were requested. Modal’s API confirmed zero active sandboxes under `hermes-workspace` after completion. Private-channel scope installation is verified, but no live private-channel message was sent in this acceptance run.

See [thread screenshot](../moyai-agentchat-thread-live.jpg) and [DM screenshot](../moyai-agentchat-dm-live.jpg).

**Live thread-chat verification (September 29):** [#bot-spam test thread](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790740160956979) used one session, `35f33e443f214d159978842947ed69d8`, for two real Hermes responses posted automatically to Slack. The first read the parent marker `cobalt-otter-58` and saved a local file containing `7`; the next recalled the marker, restored the file, and changed it to `12`. Both requests used mentions. This earlier verification used mentions; the AgentChat rollout below adds ordinary thread follow-ups and DMs. No external connected-app writes were performed.


Install the dedicated **Moyai Devin** Slack app with bot scopes `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `reactions:write`, and `assistant:write`, subscribe to the `app_mention`, `message.channels`, `message.groups`, and `message.im` events, and set its request URL to `PUBLIC_URL/hooks/slack/events`. Keep the user OAuth scopes above for conversation search; bot and user credentials are separate. Configure `SLACK_SIGNING_SECRET`, `SLACK_BOT_ENABLED=true`, and `SLACK_SESSION_USERS` as comma-separated Slack user IDs or `*` for all members of the installed workspace. Set `SLACK_THREAD_CHAT_ENABLED=true` and `SLACK_DM_ENABLED=true` (the defaults). Enable App Home’s Messages tab and allow users to send messages to the app. Enable the **Agent experience** under Slack app settings → Agents for the native AGENT badge and panel. Reinstall/reconnect Slack after adding scopes. The live BerriAI installation permits workspace members.

Invite the bot to a channel and mention **@Moyai Devin** followed by a task. Moyai adds an 👀 reaction to each accepted message, then posts its answer in the originating thread with a link to the web session. Routine acknowledgment and periodic progress messages are suppressed; explicit status commands, failures, and approval requests still receive a response. Reply in that thread without another mention to continue the same saved conversation and files. Bot messages, edits/deletes, unrelated threads, and externally shared channel events are ignored. A message addressed first to another person or agent is ignored even when Moyai is mentioned later as the subject, such as “@OtherAgent what is @Moyai?” Explicitly addressing Moyai among adjacent initial mentions still works. Attachments are identified as unread; their contents are not ingested automatically.

Direct-message **Moyai Devin** to start without a mention. Subsequent DMs reuse the same saved conversation and files; replies appear directly in the DM. A DM begins with the current request and saved session history, without importing older DMs through the shared search account. One-to-one DMs are supported; group DMs are ignored. Each DM is bound to its original Slack sender, and each turn keeps that sender’s spend attribution. **DM sessions are also visible to signed-in BerriAI teammates in the web app.** The first answer and Connections page explain this shared visibility. Separate threads in Slack’s Agent panel keep their own sessions and reply within the originating thread, while ordinary top-level DMs continue their existing session.

Tasks use enabled organization connections. Everyone with access to the Slack thread can see the answers, including future answers to messages sent from its linked web session. The web composer shows this sharing state. External app writes still need an administrator to approve the exact action in the signed-in web app; saying “yes” in Slack cannot approve a write.

Send `stop` to stop the response and cancel queued follow-ups; `sleep` also pauses listening and automatic answers in that thread. Use `wake` or a new direct mention to resume. `status` reports the session state. These commands must be the entire message. Pausing the organization Slack connection disables thread intake and pending replies.

Inbound receipts, acknowledgment reactions, and the outbound reply queue are durable. Reactions are bound to accepted message timestamps; failures to react do not block answers. The bot treats an existing identical reaction as success. Pending pre-upgrade acknowledgment/progress messages are skipped on startup. Duplicate event IDs or paired mention/message events cannot create duplicate turns. Answers are bounded, formatted for Slack, and prevented from triggering user/channel mentions. An uncertain Slack delivery is not automatically retried; the answer remains in the web app. Rollout never posts historical answers: older threads are attached only after a new explicit mention. Answers completed while a thread is asleep are not backfilled on wake.

Before agent execution, Moyai reads the Slack discussion through the **user OAuth** connection. A thread mention captures the root and replies through the mention timestamp (up to three pages, retaining at most 50 messages); a top-level mention captures the nearest 30 channel messages through that timestamp. Context is bounded to 24,000 text characters, with at most 3,000 per message. Truncation, missing context, and unread attachments are explicitly reported. Later messages and Moyai’s own replies are excluded. The session shows the source link and included messages. Imported Slack content is labeled as untrusted reference data, separate from the current request.

Context retrieval runs in the background so Slack acknowledgement does not wait for history requests. The session starts only after context is ready or its failure has been recorded. Captured context is persisted, reused for follow-ups, and not fetched again on duplicate delivery. Unfinished agent work is still interrupted on restart, never silently replayed. Sessions created before this update are labeled as older sessions without automatically captured context; use a new mention for the new behavior.

**Context acceptance:** a live thread mention in [#bot-spam](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790731878237109?thread_ts=1790731869.212189&cid=C0B302ZJU05) created session [`972fcc321d864fb7976f45e7f0b1cce9`](https://moyai-devin.onrender.com/#run=972fcc321d864fb7976f45e7f0b1cce9). Hermes automatically read the two source messages, looked up the LiteLLM-prod team, and prepared a Linear issue with the staging 502 problem, three acceptance criteria, test reference `pebble-42`, and source link. The test approval was denied; no issue was sent to Linear. A separate [channel mention](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790731894868039) created session `0d80c63ccbed4c5788dcaba7f8d67a42` and drafted the correct title and criteria from the 30-message channel excerpt without app calls or approvals. Both sessions saved their conversations and terminated their sandboxes. After the user explicitly approved expanding the credential, Linear’s Create issues permission was saved and verified in its settings, still restricted to LiteLLM-prod. The app retains per-ticket admin approval. A real issue has not been created as part of this test.

Pausing the Slack connection disables new Slack sessions. Switching a connection to read-only or pausing it also revokes pending/approved writes. It does not retract provider requests already in flight.

## Architecture

```mermaid
flowchart LR
  S[Slack mention] --> C[FastAPI control plane]
  U[Browser workspace] --> C
  C --> D[(SQLite: tasks, events, approvals)]
  C --> V[Encrypted app credentials]
  C --> M[Modal sandbox per task]
  M --> H[Hermes Agent + terminal + Chromium]
  H --> B[Run-scoped model and tool broker]
  B --> L[LiteLLM gateway]
  B --> A[Linear / Slack / Notion]
  U --> P[Approve exact external write]
  P --> B
```

The app uses native REST/GraphQL adapters for predictable OAuth and a small tool surface. A stdio MCP bridge exposes those tools to Hermes. A sandbox gets a random capability limited to its run and enabled apps; the capability is revoked on stop, completion, timeout, or restart. It does not receive provider or Modal account credentials. Agent code and browser sessions run on Modal, never on the control-plane host.

Response states: `queued → provisioning → running ↔ awaiting_approval → saving → idle` (shown as **Ready**). A session keeps its ID across responses. Messages submitted during a response queue for the next turn; they do not interrupt an in-flight tool. Each machine receives a fresh sandbox capability. Model requests remain attributed to the original user and message across renewals. After saving the latest artifact and conversation/filesystem snapshot, its sandbox terminates. The next response restores that snapshot. Snapshot retention is indefinite; Modal storage charges may apply. Stopping ends the current response and cancels queued messages. A new message resumes the last completed checkpoint; unfinished changes may be lost. Legacy tasks created before chat support remain readable with **Run again** available to start a new chat.

Final answers are stored on the control-plane disk as soon as Hermes returns them, before collecting artifacts or saving the Modal filesystem. A save failure preserves the answer in web chat and Slack with an explicit warning, retains the previous snapshot, and cancels queued follow-ups. Restart recovery completes a received answer once without replaying work. A later explicit message receives the saved chat and a warning that its files may be older. The warning clears only after a successful snapshot. Recovery archives prioritize uncommitted patches and eligible new files in nested repositories over installed dependencies; they are bounded, incomplete backups and do not include newly committed history.

`RUN_TIMEOUT_SECONDS=0` and `MAX_AGENT_ITERATIONS=0` remove the app’s response-duration, tool-iteration, and model-request-count caps. Explicit nonzero values still enforce optional limits. `SNAPSHOT_TIMEOUT_SECONDS=180` separately allows three minutes for filesystem saving. Save failures now record the stage, exception type, elapsed time, configured timeout, and a redacted diagnostic reason. The original failure in session `43e5ab156ec94f31b1e8b61852be84f8` occurred after approximately 54 seconds of the old 55-second save window; Modal showed a terminated sandbox with a 15m32s lifetime and a 30m limit. The original exception details were discarded, so the exact cause is unconfirmed; the 30-minute execution limit was not reached.

Modal [limits each sandbox to 24 hours](https://modal.com/docs/guide/sandbox#timeouts). With no app time limit, `SANDBOX_ROTATION_SECONDS=82800` requests renewal after 23 hours at Hermes’s next step callback, between tool rounds. The remaining hour allows in-flight work and saving to finish. The conversation and filesystem must both be saved, all tool calls must have results, and the old machine must terminate before another machine continues the same user turn. Intermediate handoffs stay in session activity; Slack receives the final answer once. Stop, snapshot failure, incomplete tool history, and provider errors halt renewal. With the local execution engine, a server restart interrupts unfinished work without automatic replay. The optional Temporal engine below reconnects to its journaled executions. Snapshots preserve files, not live processes or browser tabs; an individual operation that cannot reach a safe boundary before Modal’s hard limit can still be interrupted.

**Live saving verification (September 30, 2026):** commit `eaf9ec5` deployed successfully to Render. A [#bot-spam mention and ordinary follow-up](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790790095028019) used the same [session](https://moyai-devin.onrender.com/#run=6cda2f2a3e4e4523a4d973376e4b5b5f). The first turn created a nested local git repository, left `note.txt` changed from `before` to `after`, and added untracked `extra.txt` containing `granite-save-62`. The downloaded archive contains the correct nested patch, new file, answer, and recovery manifest with no omissions. The second turn recalled the marker and re-read both files with their unchanged git status from a restored snapshot. Both answers appeared once in Slack with reaction acknowledgments. Existing sessions and all three organization connections remained present; health returned 200, unauthenticated session access returned 401, and Modal's API confirmed zero active sandboxes under `hermes-workspace`. See [live screenshot](../moyai-save-continuity-live.jpg). Snapshot-failure, restart, queued-follow-up cancellation, and preserved-answer Slack delivery paths passed automated fault-injection tests; no failure was deliberately injected into production.

**Unlimited runtime verification (September 30, 2026):** commit `85a99c1` deployed successfully to Render with `RUN_TIMEOUT_SECONDS=0`, `MAX_AGENT_ITERATIONS=0`, and a 23-hour renewal threshold. The Runtime page displays **No app limit**. The suite passed 142 tests, including safe renewal, cancellation, save-failure, and request-attribution cases. A real Modal test using the pinned Hermes revision and a deterministic local model server used a shortened clock: one machine completed a terminal write, saved its conversation/filesystem, terminated, and a second machine restored the history and file without repeating the write. This verifies renewal mechanics, not 23-hour endurance.

A [live Slack test](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790791584843379) queued an ordinary follow-up while a 45-second terminal operation was running. Both messages received one reaction and one answer in the same [session](https://moyai-devin.onrender.com/#run=91479b6674a64c27a587dd75bb34b19f), with no periodic progress messages. The follow-up recalled `basalt-queue-73`, restored the saved file containing `23`, and changed/read it back as `24`. Modal confirmed zero active sandboxes afterward. Health returned 200 and unauthenticated configuration/session APIs returned 401. See [runtime screenshot](../moyai-unlimited-runtime-live.jpg) and [chat screenshot](../moyai-queued-followup-live.jpg).

With the user's approval, the historical answer from failed run `43e5ab156ec94f31b1e8b61852be84f8` was restored to its web transcript and [posted once by Moyai in the original thread](https://berriaillm.slack.com/archives/C04HP96S19D/p1790791554484499). The recovery explicitly describes the earlier execution, failing integration tests, lack of a PR, and unsaved latest files. The original error, prior snapshot, and an audit record were retained; the coding request was not replayed.

## Verification

```sh
uv run pytest -q
node --check app/static/app.js
uv run python -m compileall -q app sandbox
```

The automated suite uses isolated temporary databases and mocked external services. It tests demo completion and SSE replay, cancellation, CSRF/host/session boundaries, repository URL validation, encrypted credentials, per-run tool scope, one-time approval and denial, uncertain writes, OAuth state binding/replay rejection, restart recovery, model-proxy restrictions, sandbox cleanup during provisioning and shutdown, admin/member restrictions, connection-policy revocation, Slack signature freshness and event deduplication, and bot/user token rotation.

Manual browser QA covers creating a task, streaming and saved activity, stopping a task, connection dialogs, runtime readiness, and responsive layout. The optional browser WebMCP tools expose listing tasks and starting explicit demos; they do not enable unattended cloud runs.

The completed cloud checks are described at the top of this document. For future releases, use this acceptance checklist with configured accounts:

1. Submit a small task against a public test repository in Modal mode; confirm the image builds, Hermes invokes a terminal tool, and results stream back.
2. Confirm the model gateway records the configured model/key and budget.
3. Connect each provider and run one search/read. Test live writes only with an explicitly authorized disposable destination; approval, denial, and ambiguous-write handling are covered by the automated suite, but real provider writes remain unverified.
4. Open a public page with the agent browser and download its screenshot.
5. Stop a real task during provisioning and while running; verify Modal shows no remaining sandbox after cleanup/timeout.

## Scope and next steps

The current boundary is a single shared internal workspace with public GitHub repositories. No private-repository GitHub App, automatic PR creation, per-user app grants, live remote-desktop viewer, or distributed worker queue is included. Files and conversation resume between turns; running processes and live browser tabs do not.

For a broader team rollout, extend the existing Google SSO with per-user session authorization, move orchestration to a durable worker service with Postgres, add a narrowly scoped GitHub App, and verify live writes against explicitly authorized disposable destinations. Keep a budget-limited LiteLLM key: request-count and output limits do not substitute for a currency budget. Network egress from the sandbox is not restricted to an allowlist, and downloaded source/app content remains untrusted input to the agent.

An encrypted database alone does not protect credentials from an attacker who also obtains the adjacent encryption key or controls the host. Store the cloud encryption key separately, restrict access to the host and backups, and rotate provider grants as needed. Stopping a run revokes new broker calls but cannot retract an external write already in flight.

## References inspected

- [Hermes programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration)
- [Hermes Python library](https://hermes-agent.nousresearch.com/docs/guides/python-library)
- [Hermes package management](https://hermes-agent.nousresearch.com/docs/reference/package-management)
- [Hermes source pin](https://github.com/NousResearch/hermes-agent/tree/7968c72a3cb80beaae51948378944dd6e3423b96)
- [Modal sandboxes](https://modal.com/docs/guide/sandboxes)
- [Modal VM sandboxes](https://modal.com/docs/guide/vm-sandboxes)
- [Linear OAuth](https://linear.app/developers/oauth-2-0-authentication)
- [Slack OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)
- [Notion integrations](https://developers.notion.com/docs/authorization)

Hermes is an independent MIT-licensed project from Nous Research. This MVP builds on it and is not affiliated with Devin.


## Temporal session execution

**Enabled in production September 30, 2026.** Render uses the BerriAI Temporal
Cloud namespace `moyai-devin.vpxx6` in AWS Oregon (`us-west-2`), with 30-day
workflow-history retention and task queue `moyai-sessions-v1`. Its endpoint is
`moyai-devin.vpxx6.tmprl.cloud:7233`. The `moyai-devin-worker` service account has
account Read-Only and Write access to this namespace only. Its
`moyai-render-worker` API key expires **December 29, 2026**; rotate it before then
and update Render's private environment. Local `.env` keeps
`TEMPORAL_ENABLED=false` so development does not accidentally consume production
tasks. Use a distinct queue and isolated database for integration tests.

`TEMPORAL_ENABLED=true` wraps each chat in one long-lived `SessionWorkflow`.
Messages continue to be committed to Render's SQLite inbox before acknowledgment.
A durable wake outbox sends Signal-with-Start to `moyai-session-<session-id>`;
missed acknowledgments can safely be resent. The workflow drives bounded,
retryable Activities and waits for the next message when idle. It rolls its
history forward with Continue-As-New every 150 Activities, independently of
Modal machine renewal. Temporal history contains session IDs and small status
flags, not prompts, connection credentials, model responses, or workspace files.

```mermaid
flowchart LR
  U[Browser / Slack] --> R[Render web app + Temporal worker]
  R --> D[(Render disk: sessions, inbox, execution phases)]
  R <--> T[Temporal Cloud: one workflow per session]
  R <--> M[Modal: detached Hermes supervisor]
  M --> S[Modal filesystem snapshots]
  M --> R
  R --> G[Existing LiteLLM gateway key]
```

The first deployment intentionally runs the Python Temporal worker **inside the
existing Render service**. Render services cannot share a persistent disk. Keep
one instance and one Uvicorn worker. Do not create a separate worker pointing at
a new SQLite database. Separating/scaling workers requires a shared database and
artifact storage migration. The Temporal service itself remains in Temporal Cloud.

Activities persist their phase before launching work. A stable Modal sandbox
name reconciles ambiguous creation; a detached supervisor uses an exclusive lock
and a durable started marker to prevent duplicate Hermes launches. On Render
restart the worker reconnects to the same machine and reads its result journal.
Worker shutdown leaves the sandbox alive and preserves its capability. A user
Stop instead immediately revokes that capability and durably requests cleanup.
Per-turn capabilities are derived from the unchanged encryption/session key and
are never sent to Temporal.

Every 10 minutes by default, Hermes pauses at the next safe tool-round boundary.
Its conversation and filesystem are snapshotted together before continuing on the
same machine. After 23 hours the old machine must be confirmed stopped before a
replacement restores the checkpoint and continues the same turn. Idle chats
terminate their machine; later messages restore the saved snapshot. Each final
answer is persisted before archive/snapshot operations. Three failed snapshot
attempts retain that answer with a warning, retain the prior snapshot, and stop
queued messages. Intermediate checkpoints are not posted as Slack answers.

**Durability limits:** Activities are at-least-once, not an exactly-once guarantee
for external effects. A missing machine or an abandoned launch marker with no
confirmed result stops with an explicit interruption rather than automatically
replaying potentially completed writes. The last safe checkpoint is retained for
an explicit follow-up. Snapshots do not contain RAM, running processes, or browser
tabs. A tool that cannot reach a safe boundary before Modal's hard limit can still
be interrupted. In-flight HTTP model/tool requests to the Render broker can fail
during a Render restart; Temporal does not transparently replay those requests.
Existing write approvals, uncertain-write records, Slack outbox deduplication,
and per-user inference accounting remain in force. Modal snapshots are retained
indefinitely as before; periodic snapshots add storage usage and need an explicit
retention policy before large-scale use. Losing the Render disk still loses the
application data; Temporal history is not a database backup.

### Configure and roll out

1. Create a Temporal Cloud namespace in a nearby region. Create a service account
   limited to worker/client access for that namespace, not organization admin.
2. Privately set `TEMPORAL_ADDRESS` to the namespace's displayed endpoint,
   `TEMPORAL_NAMESPACE`, and `TEMPORAL_API_KEY` in Render. Keep
   `TEMPORAL_TLS=true`, `TEMPORAL_TASK_QUEUE=moyai-sessions-v1`, and
   `TEMPORAL_CHECKPOINT_SECONDS=600`. Never change `ENCRYPTION_KEY` or
   `SESSION_SECRET` during the migration.
3. Wait for existing legacy runs to finish or explicitly stop them. Back up the
   Render database. Then enable `TEMPORAL_ENABLED=true` and deploy. Startup
   refuses to adopt a legacy in-flight process without a recovery journal.
4. Runtime shows the selected engine and connection status. If Temporal is
   unreachable, new messages remain saved and queued. The worker retries the
   connection; it does not fall back to a second execution engine.
5. Verify a small session in `#bot-spam`, restart the Render worker while an
   operation is running, queue a follow-up, and confirm both responses and
   restored files. Confirm the workflow in Temporal and no idle Modal machines.

To roll back, drain Temporal work first; disabling it while execution phases are
active is rejected to protect unfinished work. Keep workflow and Activity names,
phase schema, and task queue compatible across code deployments. A breaking
change needs a versioned workflow/queue migration, not an in-place rename.

Local testing uses the official Temporal dev server through the Python SDK:
`uv run pytest tests/test_temporal_integration.py -q`. The first run downloads the
official CLI. That test stops a real Temporal worker during a simulated Modal
operation, queues a message while offline, starts a new worker on the same disk,
and verifies one execution per message and deterministic workflow replay.
`tests/test_durable.py` separately covers lost launch acknowledgments, checkpoint
renewal, cancellation, missing machines, save failure, and wake outbox races.


**Pre-deployment verification (September 30, 2026):** 154 tests pass. A real
Temporal dev server plus real Modal sandboxes and the pinned Hermes agent
survived a worker restart, processed a follow-up queued while offline, and
restored conversation/files across four sandboxes under shortened checkpoint and
renewal clocks. A deterministic local model fixture produced the file contents
`x`, `x`, `xy`, `xy`, confirming each terminal write happened once. Both answers
were recorded once; all test machines were terminated. This verifies the adapter
and recovery mechanics, not a 23-hour endurance run or the production cutover.

**Production recovery verified September 30, 2026:** commit `a39f68f` deployed to
Render with Temporal enabled after a private database backup and confirmation
that no legacy execution was active. A real
[#bot-spam conversation](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790794436049919)
created [this session](https://moyai-devin.onrender.com/#run=6c4bad3e5a9e4b2c88c223bed07f2e24).
Render was restarted during a 180-second terminal operation, with an ordinary
Slack follow-up already queued. The replacement worker reattached to the same
running Modal sandbox, completed the first response, and restored its snapshot
on another sandbox for the follow-up. Both answers and both reaction receipts
were sent once. The final downloaded archive contains exactly `first\nsecond\n`
in `new-files/temporal-restart-check.txt`, demonstrating neither append was
duplicated. The actual Cloud workflow history replayed successfully, recorded
the retried Activity, contains no test prompt or credentials, and is now waiting
for more messages with zero pending Activities. No Modal sandbox remained active.
All existing shared connections and BerriAI sign-in remained present;
unauthenticated configuration/session APIs returned 401 and health returned 200.

The restart also exposed a browser feed that could remain closed after a
deployment HTTP error. Commit `bb6fd9a` explicitly reconnects from the last event
cursor while preserving the composer; regression checks cover repeated failures,
duplicate events, navigating away, and stale callbacks. Run these checks with
`node --test tests/test_chat_stream.cjs`.
A second production restart with no active tasks verified that the updated web
chat showed "Reconnecting", recovered without a page reload, and preserved an
unsent draft. The Render Blueprint now also keeps `TEMPORAL_ENABLED=true`,
matching the verified production configuration.
