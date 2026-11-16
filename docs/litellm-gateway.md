# Personal LiteLLM gateways

Connect a LiteLLM gateway in Settings > Connections using your personal virtual key. Moyai prefills the gateway URL from the deployment's inference configuration when available. Model selection and inference continue to use the deployment's configured models and key

The connection belongs to the signed-in user and is available in private web chats. A gateway connection cannot be shared with the workspace. The key must have access to the requested MCP servers and Skills Hub resources; uploads also require a writable user identity in LiteLLM. Downstream MCP services may require separate account authorization in the gateway

On save or **Refresh capabilities**, Moyai discovers Skills Hub metadata and MCP tool availability independently. An unavailable service does not disable the other. Credentials are encrypted in the existing connection store and are never returned to the browser. Changing the gateway URL requires a replacement key

- MCP tools use the existing connection tool search and load definitions on demand
- Skills appear in the `/` picker under their gateway name. Selecting a skill explicitly admits its connection and loads the package at execution. Connecting or browsing the picker does not fetch or apply instructions
- The agent can search or pull skills when asked. Pull returns package contents for inspection; it does not select the skill for a task
- The agent can create and upload a new skill when asked. Turn off **Read only** to permit writes; normal Moyai approval still applies. Report the gateway response, including pending review. Existing skill names are not overwritten

Choose default connections in Settings > General to make gateway tools available without selecting a skill. Disabling a connection prevents new use and invalidates the revision admitted by existing work

Supported packages contain a root `SKILL.md` and UTF-8 supporting files. Packages are read in memory, never extracted onto the Moyai host, and are limited to 100 entries, 10 MiB compressed, and 450 KB expanded text. Discovery supports up to 1,000 skills per connection. Binary assets and nested plugin packages are outside this initial integration

## Local verification

The external provider in `scripts/gateway_demo.py` serves synthetic skill/MCP data and records requests. It does not proxy or simulate inference. Run the actual Moyai app with a real configured model and isolated local data

```sh
uv sync --frozen
npm ci
npm run build
.venv/bin/uvicorn scripts.gateway_demo:app --host 127.0.0.1 --port 8874
```

In another terminal, configure normal deployment inference and an isolated Moyai data directory. For the approved Codex E2E profile:

```sh
WORKSPACE_PASSWORD='moyai-local-gateway-demo' \
  /Users/tin/.codex/private/moyai-e2e/run \
  --app-name moyai-codex-e2e-gateway \
  -- env MOYAI_MCP_ALLOWED_ORIGINS='["http://127.0.0.1:8874"]' \
  .venv/bin/moyai dev --host 127.0.0.1 --port 8876
```

The launcher sets a private data directory and loads approved credentials without writing the inference key to files. The allowlist must be supplied inside the launcher because it filters inherited configuration. For other environments use `moyai dev` with the repository's normal local configuration

1. Sign in at `http://127.0.0.1:8876` as `admin` with the synthetic password above
2. Add a LiteLLM Gateway named **My LiteLLM Gateway**, URL `http://127.0.0.1:8874`, personal key `gateway-demo-token-only`
3. Confirm one Skills Hub entry and one MCP tool. The deployment model picker is unchanged
4. Start a private chat and ask for a plain response without tools. `/receipts` should contain no skill archive request
5. Type `/gateway`, choose **Gateway Verification**, and send **Verify the inventory using this skill**. Expect quantity `42`, code `GATEWAY-VERIFIED-4821`, and **Explicit skill selection verified.** Expand activity to see tool search and `lookup_inventory`
6. Ask to pull `gateway-verification` for inspection only. Expect `SKILL.md` and `references/check.txt`; no additional inventory call
7. Edit the connection, turn off **Read only**, then ask to create/upload a new skill. Approve the exact action in Moyai and continue. Expect `pending_review`, with the filename recorded by the external provider
8. A second local user and a shared conversation must not list the personal gateway or its skills. Sharing the connection is rejected. Disable it and confirm new invocations are rejected

Inspect only synthetic provider receipts:

```sh
curl -fsS -H 'Authorization: Bearer gateway-demo-token-only' \
  http://127.0.0.1:8874/receipts
```

Stop both local processes when verification is complete. Screenshots and recordings of this workflow prove Moyai behavior against a real model and the identified synthetic external provider; they do not claim an end-to-end production Skills Hub or downstream OAuth deployment test
