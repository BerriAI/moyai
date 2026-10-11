# Agent responses before computer startup

The independent runtime lets the selected agent use connected apps and answer
questions before acquiring its full workspace computer. A shell, repository,
browser, credential command or file operation acquires that computer under the
same turn. The agent retains its model, conversation, goals and requester.

This path is enabled by default for **Codex on Modal with durable execution**.
`INDEPENDENT_AGENT_RUNTIME` defaults to `true`; set it to `false` to opt out. Other harnesses and providers
use their existing runtime. A computer already kept warm also finishes through
the existing path. No harness or model is silently substituted.

## Qualify a deployment

Use a staging deployment with the existing Modal connection, model gateway,
durable runtime and connected apps. This change requires schema revision **13**
and API, worker, broker and coordinator protocol revision **15**. Follow the
[coordinated migration procedure](schema-migrations.md); it is not an API-only
or compatible rolling update from revision 8.

1. Set the following shared setting on every role in the target release:

   ```dotenv
   INDEPENDENT_AGENT_RUNTIME=true
   ```

   Preserve the existing model, harness, provider and credentials. Shared
   runtime fingerprints include this setting; change it through a coordinated
   release, not on one replica.
2. From that release's repository root, prebuild both runtime images with the
   deployment's configured Modal connection:

   ```sh
   uv run --frozen python build_workspace_image.py
   ```

   A successful command reports the workspace image ID after both builds
   finish. A failed build leaves this path unqualified.
3. After the coordinated staging release, send a Codex chat that needs one
   connected-app read. Confirm a useful answer or read receipt arrives before
   a full computer is acquired. Then ask it to create and publish a small file.
   Confirm the same session acquires a computer and the delivered file contains
   the requested bytes. Repeat from a bound Slack thread.
4. Open **Computer** during an app-only turn. **Wake up to see live** should
   acquire the computer without submitting another user message. Verify stop,
   a worker restart, a lost computer, a follow-up model change and a child or
   credential wait before enabling the path for normal traffic.

Record receipt-to-first-inference and receipt-to-first-useful-result latency for
cold and warm runs. The local test uses the installed Codex SDK and synthetic
inference/admission; it does not measure Modal startup or live model quality.

## Recovery and operation receipts

The existing durable session controller owns both machines. Computer capacity
applies when a computer is reserved; an app-only agent turn does not reserve a
full computer slot. The control runtime has its own Modal compute cost.

Each computer operation has a stable `request_id`. The broker stores admission,
dispatch and result receipts. A worker retry can reconcile the same launch; a
dispatched operation cannot move to another computer. When the computer or its
response disappears, the agent can use `computer_operation_read` to inspect the
receipt. An unknown outcome requires inspecting destination effects before a new
write. Stop cancels undispatched work and preserves uncertainty for work already
dispatched.

Answers that need no computer settle from the durable conversation and answer
records. Computer turns save the computer's artifacts and filesystem checkpoint.
The ephemeral control filesystem is never exposed as the user's Computer. After
computer loss, the last filesystem checkpoint can be older than saved action
receipts; the recovery notice preserves that distinction.

Large computer requests and opaque native SDK state use encrypted, verified
transport parts. Parts are bound to the original run, turn, requester, capability
and destination. They remain stored with the session until its data is removed;
account for them in storage capacity. Native-state reuse still requires the exact
public checkpoint and compatible requester/model context. Private memory or skill
context continues to invalidate reuse.

## Local checks

From the repository root:

```sh
uv run --frozen pytest -q tests/test_independent_runtime.py tests/test_independent_runtime_sdk.py tests/test_broker_payloads.py
uv run --frozen pytest -q tests/test_context_store.py tests/test_context_gateway.py tests/test_durable.py tests/test_workspace_image.py
```

The first command covers operation identities, lost responses, ownership changes,
computer loss, complete payloads and the real SDK/MCP path. The second covers the
shared conversation, native-state and durable lifecycle. Use
[migration checks](schema-migrations.md#local-proof) against a disposable
PostgreSQL instance. Passing these checks does not enable or deploy the feature.
