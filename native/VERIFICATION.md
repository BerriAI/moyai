# Moyai native-core verification

This rebuild uses Pydantic AI 2.55.0 and Pydantic AI Harness 0.55.0 directly inside Temporal workflows. Modal supplies the computer, and LiteLLM supplies inference. Moyai owns admission, authorization, presentation, resource ownership, and delivery receipts.

This is the first native-core implementation, not complete parity with all 442 legacy inventory entries. See `README.md` for supported behavior and remaining product work.

## Real application comparison

The baseline was the actual `BerriAI/moyai` application at `5b66bb4b97c4bc4c6546749717636d3f2a8b2758`, extracted without source changes and deployed locally. Its cloud session used the Codex harness, GPT-6 Astra through the approved development gateway, real Modal computers, and a local Temporal service. An authenticated temporary tunnel supplied its required broker callback. The tunnel was closed afterward.

The new implementation was deployed from the PR's `native/` application with its actual web interface, API, worker, real gateway/model, Modal sandbox, and independent local Temporal service. No scripted model or reconstructed UI supplied these results.

Both applications completed the same workflow:

1. Create `/workspace/hello.py` printing `Hello from Moyai`, run it, and save it for download.
2. In the same computer, read that file, change it to print `Hello again from Moyai`, run it, and save the new version.
3. Download and inspect the saved output.

Both reported exit code 0 and the requested observed output. The downloaded updated files contained exactly:

```python
print("Hello again from Moyai")
```

Their SHA-256 was identical:

```
9cee03b2b5897de1d9ade528f45190afcec93fe2dd700fe5b760818148cccdf5
```

The baseline also succeeds at this task. This is a preservation/rebuild comparison, not a claimed baseline bug fix or a performance benchmark.

## Real recovery proof

### Replaced worker during inference

The API and Temporal service remained running. The native worker was killed while `agent__moyai__model_request_stream` was active, then a replacement worker was started against the same task queue and private data.

Workflow `moyai-turn-fd16fbb8-70cf-4072-bc50-bc5c9f15d575` resumed and completed. Temporal recorded attempt 2 for the interrupted activity. There was one application turn and one saved assistant answer. It read the existing file from the same Modal computer and returned its exact contents.

### Unavailable computer

The owned Modal sandbox was explicitly terminated. A subsequent request failed with:

> The conversation's computer is no longer available. Your saved answers and downloads are preserved.

The saved workspace reference remained unchanged, and no replacement sandbox was running in the isolated test app. The previously saved file still downloaded with the SHA-256 above.

### Interrupted write

A separate actual-app task ran a command that incremented a marker and then waited. The worker was killed only after the real Modal file confirmed the first dispatch. After replacement, turn `43f4f384-0907-4869-84d3-a20b4322f12a` failed conservatively. Temporal recorded one shell attempt, the marker remained `1`, and one unresolved effect receipt remained. The command was not automatically repeated.

## Automated checks

The native suite contains 33 tests. The native tests use the real Pydantic libraries and local Temporal server, with explicitly controlled model responses. PostgreSQL qualification uses a disposable local database. Slack protocol tests use a transport double and make no real Slack posts.

Coverage includes native file tools, skills, plans, memory, delegation, replay, deferred questions, steering, Stop, private access, grant revisions, workspace account scope, immutable outputs, encrypted/offloaded payloads, concurrent bootstrap, request idempotency, unknown effects, and Slack delivery ownership.

The existing repository frontend build and all 593 frontend unit tests also passed. This does not claim the entire legacy backend regression suite was run.

## Browser and captures supplied with the PR handoff

The actual native app was inspected at measured viewport widths of 1440, 768, and 320 pixels. The measured document widths matched the viewport widths; mobile navigation was exercised.

- `moyai-baseline.mp4`: live browser capture of the baseline follow-up and completed result.
- `moyai-native.mp4`: live browser capture of the native follow-up, with a completed-state segment showing the later worker-recovery read.
- `moyai-baseline.jpg` and `moyai-native.jpg`: actual rendered results.
- `moyai-native-tablet.jpg` and `moyai-native-mobile.jpg`: measured responsive layouts.

The videos join a live running segment with a short live completed-state segment. Waiting/intervening verification time is omitted. No UI, model output, terminal output, or tool result was synthesized for these captures. Both videos were decoded successfully after encoding.

## Qualification limits

- GitHub, Linear, Notion, and personal Slack capabilities are wired to native Harness integrations, but no live test identities were configured for this task.
- The Slack bot's signed ingress and uncertain-send handling were tested with transport doubles. No real Slack messages were sent. Live Slack acknowledgment-loss proof still needs an authorized test bot and destination.
- Live GitHub PR-creation acknowledgment-loss reconciliation was not performed. The application conservatively blocks further writes after an unknown outcome; it does not claim provider-specific exactly-once writes.
- Automatic computer restoration, complete legacy Slack behavior, SSO, automations, sustained goals/swarms, skill learning, billing reconciliation, and legacy-data migration remain product work.
- The native schema bootstrap is not a production migration system.

## Bugbot follow-up: October 14, 2026

All five findings on `1eeb2570afb009fe5f8d91aeae6a5e464c838d16` were reproduced and fixed.
Nine added regression cases fail on that revision and pass with the changes.
The native suite passes 41 tests, with the opt-in PostgreSQL test qualified separately.

The actual native API and worker were deployed from the original and changed
sources with separate local Temporal services, real GPT-6 Astra inference and
real Modal computers. Killing the worker during `moyai_save_computer_v1` changed
the original answer's turn to `failed`; the corrected application kept it
`completed`, displayed a snapshot warning, and released the active turn.
A real native question in a shared conversation exposed `pending` to another
member on the original version; only the requester received it after the fix.

Signed synthetic Slack envelopes entered the running `/hooks/slack` endpoint:
the original admitted two turns for one message, and the corrected app admitted
one. The actual dispatcher staged 100 of 125 seeded completed replies before,
and all 125 afterward. Outbound Slack HTTPS was blocked through a dead loopback
proxy in these isolated API processes; no real Slack messages were sent, and
these checks do not qualify delivery to Slack itself.

Real browser captures show the original pin control remaining "Pin" after
clicks and the corrected control changing between "Pin" and "Unpin", with its
saved preference reflected by the session API.

## Reproduce the native app

From the repository root:

```sh
npm ci
npm run build:native
cd native
uv sync --frozen
```

Configure `.env` using `.env.example` with a private application password, approved model gateway/key and Modal authentication. Then:

```sh
uv run moyai dev --port 8796
```

Open `http://127.0.0.1:8796`, sign in, and use the workflow above. The development command uses real configured resources. Keep test data and credentials separate from production, and terminate test computers when finished.
