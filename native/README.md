# Moyai on Pydantic AI

This is a fresh, separately runnable Moyai application. Pydantic AI owns the agent
loop, Pydantic AI Harness supplies agent capabilities, and Temporal owns active
execution and recovery. Modal supplies the coding computer and LiteLLM supplies
model inference. There is no dependency on Moyai's legacy engine or its Deep Agents
integration.

The implementation uses Pydantic AI **2.55.0** and Harness **0.55.0**, pinned with
`uv.lock`. It is a foundation for the ownership plan, not a claim of exact parity
with every behavior in the 442-item legacy inventory.

## Ownership

| Responsibility | Implementation |
| --- | --- |
| Model/tool execution, validation, continuation | Native Pydantic `Agent`, `Conversation`, and `AgentRun.enqueue` |
| Files, commands, repo instructions, delegation | Native Harness `Coder` |
| Planning, skills, memory, compaction, recall | Native `Planning`, `Skills`, `Memory`, `SummarizingCompaction`, `ConversationSearch` |
| Saved agent steps and lineage | Native `StepPersistence` and SQLite/PostgreSQL stores |
| Questions and approvals | Native `AskUser`, deferred tool requests/results, and toolset approval controls |
| Active execution, retries, cancellation, replay | Native `TemporalDurability`, `PydanticAIWorkflow`, and `PydanticAIPlugin` |
| Computer operations | Native `ModalSandbox` and workspace APIs |
| GitHub, Linear, Notion, personal Slack tools | Native Harness service capabilities, account-scoped through a dynamic toolset |
| Authentication, admission, current authority | Moyai application records and a Temporal activity interceptor |
| Web and Slack presentation, saved outputs | Moyai adapters using native events, snapshots, workspace files, and ordinary Slack bot APIs |
| Unknown external effects and bot send receipts | Moyai's persistent admission/receipt records; neither starts another agent loop |

`src/moyai/agent.py` composes the native capabilities.
`src/moyai/workflow.py` calls the SDK's public iterator directly. The iterator body
does not dispatch models or tools. HTTP and Slack requests persist application
inputs; the dispatcher starts or signals the existing Temporal workflow.

Temporal is the only component that resumes active execution. No background job
loads a step snapshot and launches a competing agent.

## Local startup

From the repository root:

```sh
npm ci
npm run build:native
cd native
uv sync --frozen
cp .env.example .env
chmod 600 .env
```

Configure a strong `WORKSPACE_PASSWORD`, your LiteLLM gateway URL/key, an enabled
model alias, and Modal authentication. The current inference binding uses the
gateway's **Responses API**; qualify each enabled model alias through that API.
Modal can use its normal authenticated CLI profile or the token pair in `.env`.
The CLI loads `.env` into the trusted host process without overriding exported
deployment variables.

```sh
uv run moyai dev --port 8796
```

Open `http://127.0.0.1:8796` and sign in as `admin`, using
`WORKSPACE_PASSWORD`. This starts a real local Temporal server, a worker and the
API. It uses real configured inference and Modal, not simulated responses. The
first computer may need an image build.

Try:

> Create /workspace/hello.py that prints "Hello from Moyai". Use a short plan,
> create the file, run it, and save it for download. Report the observed output.

Then ask for a change in the same computer. The conversation stores its native
workspace reference. Files explicitly saved with `save_artifact` remain available
after the live computer expires.

`moyai dev` is for local development. Its Temporal history and application data
live in `DATA_DIR`. Keep that directory private and preserve its encryption key.

## Images and the computer

The native interface reuses Moyai's navigation style, home constellation and
interactive sculpture. Start a task directly from the home composer, or continue
an existing conversation beside the Computer, Activity, Plan and Files panel.

Paste an image with Command/Ctrl+V or choose **Attach**. Both composers show
removable previews and support image-only messages. PNG, JPEG, GIF and WebP
uploads are limited to eight images per message, 8 MB and 20 megapixels per
image. Uploads are validated and stored in encrypted objects; admission binds
each upload to its owner's chosen conversation. The worker supplies native
Pydantic `BinaryContent`, including when steering a run.

After a task creates its Modal workspace, choose **Computer → Open computer**.
This opens a real X desktop in the same sandbox, with a terminal and Chromium.
Use **Take control** to click, type, paste text and scroll; **Open browser** opens
an HTTP(S) address, and **Screenshot** downloads the current view. Expand the
panel for more space. **Release control** or Ctrl+Shift+Escape returns control.

Desktop access requires a private conversation and rechecks the workspace's
account grants. Shared sessions retain agent execution, activity, plans and
saved files, but do not expose a personal browser profile to other requesters.
A persistent database lease keeps new
agent tasks queued while a person has control. Taking control requires the
active task to finish or acknowledge Stop. Abandoned leases expire. Input is
serialized in the UI and guest; uncertain input is not automatically repeated.
An in-flight action also prevents another tab from releasing control, and
expired input is rejected in the guest before dispatch.
The desktop uses the native workspace API and private Modal exec, without
exposing VNC or a browser debugging port.

Files and browser sign-ins placed in this private computer are accessible to
code running there after handoff. Restrictions on the app's connected-service
tools do not restrict an account signed into manually inside Chromium.

Existing computers install the desktop prerequisites on first opening; new
images include them. This adds basic viewing/control, not every legacy computer
feature. Desktop recording, automatic workspace restoration, and restored
browser-login workflows remain outside this implementation.

## Separate API and workers

Configure a Temporal service, `TEMPORAL_TASK_QUEUE`, a dedicated PostgreSQL
database, a shared `ENCRYPTION_KEY`, and shared object storage. Use the same
configuration on every API and worker:

```sh
uv run moyai serve --host 127.0.0.1 --port 8796
uv run moyai worker
```

The API owns admission/dispatch and optional Slack delivery. Workers execute the
registered native activities. Use HTTPS at the application edge,
`MOYAI_PUBLIC_URL`, and `MOYAI_SECURE_COOKIES=true`.

Application tables have a `moyai_` prefix. Harness manages its own native tables.
Use a dedicated database; this implementation does not migrate an existing
installation's users, conversations, sandboxes or pending workflows.

The initial schema is created at startup. This is not a production migration
system. Introduce versioned schema migrations before changing a deployed schema.
Drain or version active Temporal workflows before changing registered activity
names or pinned SDK execution behavior.

## Accounts and integrations

Add accounts in **Connections**, then select them under **Context & connections**
for a message. New conversations select no accounts; continuations restore the
requester's previous selection for review. Read-only is the default;
non-read-only tools require explicit approval by default.

Personal accounts are restricted to private web conversations. Shared web
conversations use explicitly shared accounts. A live computer is bound to its
account context; changing that context requires a new conversation, rather than
inheriting files from an account that is no longer selected.

- GitHub uses a token with the intended repository permissions.
- Linear uses an API key or OAuth access token.
- Notion's hosted tools require an OAuth access token, not an integration token.
- Personal Slack tools require a user token, not a bot token.

Moyai uses the SDK's provider tool catalog and schemas. Token permissions establish
resource access. An optional `allowed_tools` policy narrows the catalog further.
Merge, approving-review and repository deletion tools are excluded. Account
credentials never enter the coding computer, so a shell cannot obtain a
merge-capable token from the application's connection configuration.

Live provider qualification requires configured test accounts. Installing a
capability is not evidence that every remote operation or legacy connector
contract is available.

## Slack bot surface

The bot surface is separate from personal Slack tools. Set
`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID`, and an HTTPS
`MOYAI_PUBLIC_URL`. Register `/hooks/slack` as the event endpoint. Configure the
bot's mentions/messages and ordinary message-posting/history permissions.

Create an application user linked to a verified Slack member ID:

```sh
uv run moyai user teammate --name "Teammate" --slack-user U0123456789
```

The command asks for the application password privately. Unlinked users cannot
start work. Mentions create a thread conversation; replies from its original
linked requester steer or queue work. All Slack-origin conversations have private
web access for that requester. This avoids equating channel membership with
workspace-wide sharing.

Slack memory is isolated per thread and requester; it does not inject personal
web-chat memory into a channel. Connected accounts are disabled for Slack-origin
conversations until destination-specific export policies are implemented. The
web interface identifies that its replies are delivered to Slack.

Signed events are persisted before acknowledgment and deduplicated. Outgoing
answers have persistent send ownership across network waits. A lost send
acknowledgment is checked against Slack metadata; it is never automatically
reposted. A missing receipt remains uncertain, including when a thread read is
truncated or unavailable.

The initial bot adapter sends a bounded answer and a link to the conversation.
Questions and approvals are completed in the authenticated web app. Rich Slack
cards, media uploads, OAuth onboarding and the full legacy Slack command set are
not implemented by this adapter.

## Behavior and recovery

- Repeated client request identities do not admit duplicate turns.
- Queued messages can be edited or cancelled until admission.
- **Send now** uses the native pending-message mechanism. A changed requester,
  model or connection set becomes a follow-up rather than inheriting an active
  run's authority.
- **Stop** revokes unstarted work before requesting Temporal cancellation.
  Failed cancellation RPCs are reconciled. Native foreground-command cancellation
  is exercised by the tests. Already returned background process handles and
  external writes remain real effects; cancellation cannot undo them.
- Model and tool attempts recheck current authorization at the activity boundary.
  An approval cannot restore a revoked account grant.
- Native working-history search and memory are scoped independently of the
  shared product conversation. Private and shared memory namespaces differ.
- A possibly dispatched shell or connected-service write retains an unresolved
  receipt. Another write is blocked even if the model changes the call ID.
  The requester can record inspected evidence using
  `GET /api/sessions/{id}/effects` and `POST /api/effects/{id}/resolve`.
  This is conservative uncertainty handling, not exactly-once external execution.
- The answer is committed before computer snapshot settlement. A snapshot failure
  produces a warning without replacing the received answer.
- Expired computers fail closed through native `WorkspaceUnavailableError`.
  There is no silent empty-workspace fallback. Automatic snapshot restoration and
  a workspace-recovery UI remain application work.
- Native model-client HTTP retries are disabled. Temporal allows up to three
  model attempts; writes and arbitrary shell dispatch have one automatic attempt.
  Specifically classified reads can have three attempts.

Step, memory and plan stores contain private application data. Protect and back
up the database. Application secrets, saved artifacts and Temporal payloads,
including encoded failure attributes, are encrypted. Large Temporal payloads are
offloaded to immutable private objects. Keep those objects for the lifetime of
the corresponding workflow histories.

## Public SDK compatibility bindings

Harness 0.55 needs a few explicit bindings when composed with Temporal:

- Question/history toolsets keep stable registered identities.
- Plan tools execute as workflow-side orchestration, while the native plan
  store operations remain durable activities.
- `TemporalRunContext`'s documented extension point recreates a worker-local
  tracer for native memory operations.
- Modal's module is explicitly passed through Temporal's workflow import
  sandbox; actual computer I/O remains in activities.

These bindings preserve the SDK's tools, schemas, algorithms and stores. They
do not patch installed packages or copy the agent loop.

## Verification

```sh
cd native
uv run pytest
uv run pyright
cd ..
npm run typecheck:native
npm run build:native
```

The controlled-model tests use the real SDK, local Temporal server, encryption
codec and native stores. They cover native capability composition, history replay,
deferred questions, cancellation, request deduplication, scope changes, unknown
effects and Slack delivery ownership. Transport doubles are explicitly labelled.

For native PostgreSQL qualification, point `MOYAI_TEST_POSTGRES` at a disposable
**local** server on which the test user may create/drop a database. The test
creates its own randomly named database and removes it afterward:

```sh
MOYAI_TEST_POSTGRES=postgresql://testuser@127.0.0.1:5432/postgres \
  uv run pytest tests/test_postgres_qualification.py
```

Live app/model/Modal verification is separate from those tests. The PR's evidence
records the actual deployment revisions, workflow, observed outcomes and remaining
qualification gaps. No fixture run substitutes for that proof.

## Product scope still outside this foundation

The exact legacy swarm/goal rules, automation editor and event sources, skill
learning and personal library editing, quote-based memory review, SSO, billing
reconciliation dashboards, desktop recording, automatic computer restore,
and legacy-data migration remain explicit product work.

Use native Pydantic/Harness capabilities and Temporal primitives for those
features. Add application policy and presentation around them rather than bringing
back a legacy execution engine.
