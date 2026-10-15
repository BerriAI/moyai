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

At the initial core handoff, the native suite contained 33 tests. The native tests use the real Pydantic libraries and local Temporal server, with explicitly controlled model responses. PostgreSQL qualification uses a disposable local database. Slack protocol tests use a transport double and make no real Slack posts.

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
At fix commit `056fef0`, 41 native tests passed, with PostgreSQL qualified separately.

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

## Native UI, clipboard images and computer control

The before deployment uses the actual native application from
`056fef0320611a753a442a6812c8af1642ebb6f2`; its imported source path was verified.
The after deployment uses this checkout's built UI, API and native Temporal
worker, real GPT-6 Astra through the development gateway, and real Modal.

- Pasting a synthetic PNG into the original composer created no preview and
  left image-only Send disabled. After adding the same text question, the real
  model reported that no image was attached.
- Command+V in the new home and conversation composers produced a removable
  preview. Sending the image with the same question produced the observed
  answer **"Violet, 7319."**, matching the image.
- The Computer panel opened a real desktop in that conversation's existing
  Modal sandbox. Keyboard paste and Enter in its terminal wrote
  `/workspace/manual-proof.txt` and displayed **"Native computer control works."**.
- An agent request remained `queued`, with no active turn, while human control
  was held. After release, the same native workspace was available to the agent.
  The first read encountered a gateway HTTP 503; a subsequent read-only request
  succeeded and returned the file's exact contents.
- Browser checks measured 1440-, 768- and 320-pixel widths without horizontal
  document overflow. Mobile navigation and clipboard-preview removal were
  exercised. Real captures show the app and sandbox; there is no reconstructed
  UI or scripted model response in this proof.

Automated coverage additionally checks encrypted image admission and native
Temporal input, invalid/corrupt image rejection, private upload access, forbidden
cross-conversation reuse, control lease admission/expiry, requester-only computer
access, revoked connection grants, and releasing control from another tab while
input is in flight.

The final native suite passed **52 tests**, including disposable PostgreSQL
qualification. Ruff, Pyright, native TypeScript/build, and wheel/sdist packaging
also passed. A fresh live demo read the image, wrote and ran
`/workspace/native-demo.txt`, and saved a download containing `VIOLET\n7319\n`.
Its SHA-256 is
`d6aee3ae50ae7b717a61ab6c073e75e5812b84464feeb50075478ab40957258a`.
The guarded desktop then ran `cat` on that same file, and the release-control
keyboard shortcut was exercised through the browser.

## Desktop review follow-up: October 15, 2026

Two additional Bugbot findings on `04eb20d` were reproduced and fixed.
The actual baseline API was served from that revision's extracted source, and
the corrected API from this checkout, both using the real application's data
and a real native Modal workspace. No model response or desktop implementation
was substituted for these endpoint checks.

A reversible filesystem fault made the guest helper exit with an error:
before, start returned 503 and both release and retry returned 409, with the
operation and lease retained. After, start/retry return a confirmed 422 failure,
release returns 200, and neither record is retained. Ambiguous timeouts still
retain ownership. The guest directory was restored after each run.

Matched browser scroll gestures moved the surrounding baseline panel 227 pixels
but left the corrected panel at 0. The access logs recorded 7 versus 3 computer
POSTs in the observed windows (including any heartbeat). Real before/after
recordings were captured. The queue regression holds a transport call open,
feeds 200 wheel updates, and verifies only two bounded scroll commands; other
tests verify that pointer/key input and release supersede stale movement.

The updated native suite passes **55 tests**, including PostgreSQL. Three
desktop-input tests pass and run in the native web CI job.

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

## Sidebar, shared desktop, and independent chat: October 15, 2026

Baseline: actual local native app at `43f16104120c043938d8bbb846bdbdf0fe019fc8`.
The updated UI/API/worker used the same authenticated local deployment, real
GPT-6 Astra through the approved dev gateway, real Modal, and local Temporal.
This section supersedes the earlier private-only and chat-pausing computer policy.

- Baseline shared computer GET returned 403. Updated shared access returned 200;
  another user's private computer returned 404. A second controller received 409,
  then successfully acquired control after release. Automated coverage also checks disabled users.
- Baseline chat stayed queued without an active task while human control was held.
  Updated chat produced **READY** while **You have control** remained visible.
- Baseline queued editing used a dialog. Updated editing used the normal composer,
  saved the changed text, and restored **Keep this unsent draft.** without submitting
  it. A queued correction was promoted with **Steer now** and the real model replied
  **STEERING_WORKS** through the existing Temporal signal/native enqueue path.
- A fresh shared computer opened Chromium and a taskbar. Typing/pasting an address
  into its actual browser navigated to example.org. A separate Terminal launcher
  remains available. No public VNC/debugging port was opened.
- Real UI checks covered folder creation, assignment and rename, both three-dot
  menus, copying the exact session URL, and the Participated group for a message
  sent to another user's shared conversation. Personal preference writes do not
  reorder everyone else's Recent list. Existing folder strings are adopted on startup.
- The known empty pinned test conversation was archived through the authenticated
  API after verifying it had no messages. Real user conversations were retained.

The original shared proof computer expired during verification; its reference
was retained and the final shared-desktop check used a fresh native session.
An older manually provisioned demo computer lacked the bundled skill directory;
that test prerequisite was restored before the successful live chat/steering check.
No model or desktop implementation was substituted.

Local checks: **58 native tests passed**, plus **1 PostgreSQL qualification test**
on a disposable local PostgreSQL 18 server; **3 desktop-input tests passed**.
Native TypeScript, Vite build, Python typecheck, Ruff and diff whitespace checks passed.
The actual sidebar menus were checked at 1440, 768 and 320 pixels, including keyboard
activation and a phone-sized move dialog. Source and generated native assets are committed together.

The handoff includes actual rendered screenshots and `before.mp4` / `demo.mp4`.
The short demo selects the successful composer/steering sequence from the live
capture and holds its final frame for readability. Local recordings are not
committed to the repository. No production service or Slack messages were modified.

To reproduce: run the native application using the README setup; create a session,
open its Computer panel and take control, then send a message. It should run without
releasing the computer. While another task runs, queue a follow-up, edit it in the
composer, save, then select Steer now. Create a shared session with a second local
account to check participation and shared viewing; simultaneous control must return 409.

## Chat-only entry and composer alignment: October 15, 2026

Compared the actual locally running UI at `00665e97f1c00e1f92367ea90fca745a98477526`
with this update. Session entry previously mounted the computer panel automatically.
Sessions now start with chat only; the Workspace header button and session-menu
Open computer action explicitly reveal it. Checked fresh session creation, reload,
ordinary navigation, opening another session's computer, repeated same-session menu
requests, and closing an expanded computer back to chat. The header toggle is available
on phones. No backend or model behavior changed in this follow-up.

The composer previously measured 1112 px wide against an 820 px conversation column
at a 1440 px viewport. Both now use the same 820 px maximum width and responsive gutters,
including reserved scrollbar space. Actual DOM measurements at 1920, 1440, 768 and
320 px show identical left/right edges and widths, with the panel open and closed;
all eight cases had zero width difference and no horizontal document overflow.
Native TypeScript and Vite build passed. Before/after screenshots and an actual-app
recording accompany the local handoff. The empty fresh-session test was archived.
