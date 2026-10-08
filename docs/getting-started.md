# Install Moyai and run your first real task

[Documentation](README.md) · [Project overview](../README.md)

Deploy a new Moyai installation on Modal, configure your model, and run a task. The default walkthrough uses GPT-6 Astra with the Claude Agent SDK harness through LiteLLM. You can choose any configured model with any harness.

## Deployment layout

```text
Your browser → Moyai web app on Modal → isolated Modal agent sandbox
                        ↑                         |
                        └── model/tool requests ──┘
                        |
                        ├── your model API / LiteLLM gateway
                        └── GitHub and other apps you connect later
```

Run the installation commands on your computer. After deployment, you can shut it down and use Moyai from a browser. You pay Modal for the web app and agent machines, and your model provider for inference. Moyai stores encrypted connections on the web server and brokers the agent's access to them. Modal supplies the HTTPS address.

**This is a shared, trusted-team workspace.** Do not expose it without authentication or invite untrusted users. Connected apps retain their authorizing identity's permissions; see [security boundaries](security-and-scope.md).

## 1. Prepare your computer

Install Git, Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/). These commands use a macOS/Linux shell; on Windows, use WSL.

```sh
git --version
uv --version
uv python install 3.12
git clone https://github.com/BerriAI/moyai.git
cd moyai
uv sync --frozen --python 3.12
cp .env.example .env
chmod 600 .env
```

Run the remaining commands from this repository directory. Use the Modal CLI installed by `uv sync` in `.venv`. If you have a `.env`, edit it rather than overwriting it.

## 2. Choose a sandbox provider

For an existing Substrate cluster, follow [Substrate setup](substrate.md) and host the Moyai web app using [Render or Docker](deployment.md). No Modal sandbox credentials are needed for that path. The steps below use Modal for both hosting and sandboxes.

### Modal credentials

1. [Create a Modal account](https://modal.com/docs/guide), choose the workspace that should own Moyai, and make sure its usage/billing policy permits the deployment. Prefer a fresh workspace: the deploy script uses fixed resource names and will update an existing Moyai installation in the same environment.
2. Authenticate from the repository:

   ```sh
   uv run modal token new
   ```

3. Complete the browser login for your chosen workspace. The CLI verifies the credentials and saves them in `~/.modal.toml`, unless you changed Modal's configuration location.
4. Open that file in a private editor window. Copy the workspace profile's `token_id` into `MODAL_TOKEN_ID=` in `.env`, and its `token_secret` into `MODAL_TOKEN_SECRET=`. Keep both values from the same profile. Do not print them into a shared terminal or chat.

Supply both `.env` fields even if you have logged in through the Modal CLI. Check the token's expiry in your Modal workspace and plan its replacement. For a team service, follow your organization's service identity policy.

## 3. Configure a model endpoint

Choose one option. Store API credentials in `.env` or the host's secret store. Do not paste them into task prompts.

### Option A: your existing LiteLLM gateway

Ask your gateway administrator for:

- Its OpenAI-compatible API base URL, usually `https://your-gateway.example.com/v1`.
- A dedicated API key with a budget and permission to use your chosen model.
- The **exact model alias** exposed to that key. Do not guess the alias from the provider's marketing name.

Edit these entries in `.env`:

```dotenv
LITELLM_API_BASE=https://your-gateway.example.com/v1
LITELLM_API_KEY=<dedicated gateway key>
AGENT_MODEL=openai/gpt-6-astra
```

Ask your gateway administrator to enable `openai/gpt-6-astra` and grant your key access through the Messages API for Claude Agent SDK. Supply the API base ending in `/v1`, without `/messages`. Use an address reachable from Modal; `localhost:4000` refers to the cloud container after deployment. To host your own gateway, follow the [LiteLLM gateway setup guide](https://docs.litellm.ai/docs/proxy/quick_start) and expose it at a protected endpoint.

### Option B: set up your own LiteLLM gateway

Follow the [LiteLLM gateway setup guide](https://docs.litellm.ai/docs/proxy/quick_start). Add your provider API key to the gateway's secret store and expose a model alias named `openai/gpt-6-astra`. Create a dedicated gateway key with access to that alias and the Messages API, then use the settings from Option A. Your gateway needs a protected HTTPS address reachable from Modal.

The provider key stays on the gateway. Put the gateway key in Moyai's `.env`; do not point the Claude Agent SDK harness at OpenAI's API, which does not expose the Messages protocol.

### Choose a harness

Use the **Harness** picker beside **Model** in a new session. You can bring any model that supports your harness's API and tool-calling requirements. Set `AGENT_MODEL` to its exact gateway alias to add a custom default to the model picker; the built-in names do not grant access to those models.

With **Auto** selected, resolved `openai/` models use Codex SDK and `anthropic/` models use Claude Agent SDK, including future versions. Other provider prefixes and unqualified gateway aliases fall back to Claude Agent SDK. An explicit picker choice or `AGENT_HARNESS` setting overrides these defaults.

| Harness | Model selection | Required API |
| --- | --- | --- |
| Hermes | Any configured compatible model | Chat Completions |
| Claude Agent SDK | Any configured compatible model | Messages |
| Codex | Any configured compatible model | Responses |
| OpenCode | Any configured compatible model | Chat Completions |
| Deep Agents | Any configured compatible model | Chat Completions |
| Tool Loop | Any configured compatible model | Chat Completions |

For Claude Agent SDK or Codex, use a LiteLLM gateway that exposes the required native API. Moyai forwards Messages and Responses requests without converting them to Chat Completions. Confirm that your key permits the selected model and protocol.

Keep the chosen harness for the session; start a new session to change it. You can switch between configured models without provider-prefix restrictions. The gateway must support the selected model's tool calls on the harness's API; this is not a guarantee that every provider implements every feature. New automations use the configured default harness; existing automations keep Hermes until changed in the editor. To add another harness, register a lifecycle adapter using the [harness extension guide](harnesses.md).

### Finish the first-run configuration

Set or add:

```dotenv
# Avoid requiring the separate default title model during initial setup.
SESSION_TITLES_ENABLED=false
```

Choose repositories in **Connections → GitHub → Manage → Choose repositories** after installing your organization-owned GitHub App. Moyai stores permanent repository IDs; no repository environment variable or redeploy is required. You can run the file-writing check without GitHub. Personal-account installations are not supported.

Leave Google OAuth, Slack, tracing, and Temporal disabled for now. You do not need their credentials to run an agent. The copied Google domain/admin examples are BerriAI-specific; replace them before enabling Google SSO for your own team.

For this **Modal-hosted web app** path, leave `PUBLIC_URL` at its example value and leave `WORKSPACE_PASSWORD`, `SESSION_SECRET`, and `ENCRYPTION_KEY` empty on the first deployment. The deploy script generates missing secrets and determines the actual public URL. If you set a workspace password yourself, use a unique value of at least 16 characters. Keep `PASSWORD_LOGIN_ENABLED=true` until you have verified another administrator sign-in method.

### Check configuration before deployment

This checks for missing values without printing secrets or making a model call:

```sh
uv run python -c 'from app.config import Settings; s=Settings(); missing=s.missing_cloud(); print("Missing: " + ", ".join(missing) if missing else "Required cloud fields are present (credentials not tested)."); raise SystemExit(bool(missing))'
```

Fill in any missing fields before deploying. You will test credentials and connectivity with the first task.

## 4. Deploy the web app

**This command changes your Modal account and starts billed compute.** It updates the `moyai` web app, `hermes-workspace-config` secret, and `hermes-workspace-state` volume. Do not run it against an existing shared installation without coordinating downtime.

```sh
uv run python deploy_modal.py
```

After the image build and deployment finish, open the address printed as `Workspace URL: https://…`. Read `WORKSPACE_PASSWORD` from `.env` in your private editor and use it to sign in. The deploy script generates that password and the session/encryption secrets if empty, saves them in `.env`, and uploads the configuration to a Modal Secret. It sets `PUBLIC_URL` to Modal's assigned origin at startup.

Keep a secure backup of `.env` for redeployment. Preserve `ENCRYPTION_KEY` to retain access to encrypted connections. Git ignores `.env`, and the image build excludes it. Run one web container with persistent snapshots; see [deployment and backups](deployment.md#deploy-the-control-plane) before changing that setup.

Edit credentials in `.env`: its values override shell variables, including blank entries. After an edit, wait for active sessions to finish and rerun the deployment command to update the hosted app.

## 5. Run your first task

1. Open **Settings → Runtime** and check for **Cloud ready**. The badge checks for required settings; you still need to test the credentials.
2. Start a new session. In **Context**, select **Execution → Cloud session** and leave **GitHub repository** empty.
3. Choose **Claude Agent SDK** in the harness picker and **GPT-6 Astra** in the model picker. To test another combination, select any configured model and a harness whose API your gateway supports.
4. Send:

   > Use the terminal to create `/workspace/setup-check.txt` containing `moyai setup works`. Read it back with a tool and report the contents. Do not connect apps or publish anything.

5. Allow several minutes for the first sandbox image build. Wait for the turn to finish, inspect the tool calls in **Activity**, and open `setup-check.txt` in **Files**.
6. Send a follow-up in the same chat:

   > Read `/workspace/setup-check.txt` using a tool and report its contents.

Confirm that both turns read `moyai setup works` from the file. If either fails, resolve the error before connecting apps.

## 6. Connect your GitHub repository

You need an organization owner or someone allowed to register/install the organization's GitHub App.

1. Confirm `.env` names **your** repository, and deploy again if you changed it after step 4.
2. Sign in as an administrator and open **Connections → GitHub → Connect**. Check that the displayed repository list is correct before continuing.
3. Click **Register a new GitHub App**, then **Continue to GitHub**. Complete the organization-owned App creation and install it on **only the configured repositories**. If your team already has a suitable App, enter its **App ID** and upload its **PEM private key** using **Verify app and continue** instead. Never paste the PEM into chat.
4. Review permissions: Contents and Pull requests **read/write**, Metadata **read**. The new-App manifest also requests Administration **write** for ruleset reviewer edits and Issues **write** for the issue tools. Neither permission is required for ordinary checkout/PR work with an existing suitable App. See [GitHub permissions](integrations.md#shared-organization-github) before granting it; do not add the App as a ruleset bypass actor.
5. Return to Moyai and confirm the connection is healthy. Registering an App without completing its installation is not enough.
6. Start a **new Cloud session**, set **GitHub repository** to `https://github.com/your-org/your-repo`, and ensure GitHub is checked under **Organization connections**. Existing sessions keep their original connection selection.
7. First ask for read-only work:

   > Check out this repository, identify the command for running its tests, and run the smallest relevant test suite. Report the command and actual result. Do not edit files or open a PR.

After the tests pass, request a small change and ask for a pull request. Review and merge it yourself; Moyai cannot approve or merge PRs. Add [Slack](slack.md), [Linear, or Notion](integrations.md) as needed.

## Troubleshooting

| Symptom | What to check / do |
| --- | --- |
| `uv` is not found or Python is too old | Install uv, reopen your shell, run `uv python install 3.12`, then `uv sync --frozen --python 3.12`. |
| `Configure Modal credentials in .env first` | CLI login is separate. Put both values from the same Modal profile into `.env`. Blank `.env` values override exported shell variables. |
| Modal rejects authentication or permissions | Check that the token belongs to the intended workspace, has not expired, and can deploy/use sandboxes. Replace the credentials privately and redeploy. |
| Modal image/deployment build fails | Read the failed build step in the deploy output or Modal dashboard. Check workspace billing/quota and network access to package/Git dependencies. A running UI does not prove the separate agent image built. |
| Localhost works but **Cloud session** is disabled | Loopback `PUBLIC_URL` deliberately disables real execution. Use the Modal URL printed by deployment, not `localhost:8787`. For a separately hosted web app, configure its real reachable HTTPS origin. |
| **Cloud ready**, then model request fails | The badge checks presence only. A gateway 401/403 usually means key/access problems; 404 can mean the wrong base URL or model alias; 429 can mean budget/rate limits. Inspect the provider/gateway error and your model access. Do not select a built-in model unless your endpoint serves that exact ID. |
| Web app cannot reach gateway | A local-only gateway is not reachable from Modal. Use a reachable protected endpoint; the app appends `/chat/completions` to `LITELLM_API_BASE`. |
| Agent fails to call back to a separately hosted app | Use HTTPS and the exact configured origin; proxies must preserve Host and allow `/broker/` requests authenticated by run tokens, without an extra browser-only login. The Modal-hosted path configures its origin for you. |
| Responses say demo/simulated | Start a new session with **Cloud session** selected. Existing demo sessions keep their mode after configuration changes. |
| Model missing or gateway rejects a harness request | Set the gateway alias as `AGENT_MODEL` and redeploy. All harnesses offer the configured models. Check that your gateway/key supports the model through Messages (Claude Agent SDK), Responses (Codex), or Chat Completions (other harnesses). |
| GitHub shows BerriAI's repository / wrong repositories | Change the allowlist in `.env`, redeploy, then reconnect/install the App for exactly those repositories. |
| GitHub connected but missing from an old session | Start a new session and explicitly check GitHub in **Context**. Check that the connection is enabled and healthy. |
| Changed `.env`, but nothing changed in the app | Redeploy for Modal hosting; restart for a locally hosted control plane. Preserve existing secrets and wait for active turns to settle first. |

Do not include `.env`, model keys, Modal tokens, or PEM files in bug reports. Include the failed step and redacted error instead.

## Stop and maintain the installation

- Closing the browser does not stop the service. Stop/cancel active sessions, then stop the `moyai` web app in the Modal dashboard if no longer needed. Check remaining sandboxes separately and review storage retention/charges; stopping the web app does not delete persisted data.
- Keep one web container. Do not enable rolling deployments, multiple Uvicorn workers, or multiple writers on the same database/volume. Redeployments interrupt active tasks.
- Back up your stable session/encryption secrets along with state. Rotate expired credentials privately and redeploy when idle.
- Use [deployment alternatives](deployment.md) if you need Render or an existing VM. Choose Modal or [Substrate](substrate.md) for the agent sandboxes; the web app’s Docker container does not run agents itself.

## Optional: local UI development only

For UI development, use a separate checkout with an unmodified `.env.example`:

```sh
cp .env.example .env
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 --workers 1
```

Open [localhost:8787](http://127.0.0.1:8787). The preview uses simulated responses and saves history in `.data/workspace.db`. It does not call a model, start an agent machine, or change a repository. Use one server process.

## Existing BerriAI installation

BerriAI teammates can open the hosted workspace and sign in with an `@berri.ai` Google account instead of deploying another copy.
