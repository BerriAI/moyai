# PR write approval implementation handoff

The implementation adds a separate, server-owned approval record for an external PR. It never creates a `github_publications` receipt for that PR. Existing publication access remains the first authorization path.

## Files

Production:
- `app/github_write_access.py`: persistence, lifecycle invalidation, exact requester/session/repository/PR scope, authenticated decisions and PR identity validation.
- `app/github.py`: request/check tool schema and dispatch, common authorization for updates/comments, independent head-repository token for fork code writes, rechecks before mutations.
- `app/main.py`: existing requester identity verifier, session response and authenticated CSRF-protected decision route.
- `app/static/app.js`: approval, denial and revocation panel; editable continuation through the existing chat composer and message endpoint.
- `app/static/style.css`: scoped mobile touch targets and wrapping.
- `sandbox/agent.py`, `sandbox/github_tools.py`: explicit consent instructions and checkout guidance. The request/check tool uses the existing generic broker wrapper; there is no new grant-capable sandbox tool.

Tests/demo/docs:
- `tests/test_github_write_access.py`: real broker/auth routes with controlled GitHub transport; includes the demo resume integration test.
- `tests/test_pr_write_access.cjs`: escaped metadata, allowed UI transitions, navigation races, draft preservation.
- `tests/test_activity.cjs`, `tests/test_session_actions.cjs`: register the new renderer in existing isolated renderer harnesses.
- `scripts/pr_write_access_demo.py`: no-credentials local demo using actual app routes and the regression GitHub fixture.
- `docs/pr-write-approval-design.md`: lead design plus repository-fact amendments.
- This verification note.

## Local demo

From the checkout, run:

```sh
.venv/bin/python scripts/pr_write_access_demo.py --port 8794
```

Open `http://127.0.0.1:8794/demo`. Click **Start demo chat**, **Allow for this chat**, **Continue in chat**, then **Send message**. The actual broker update/comment results appear in the chat. Revoke access and send another message to observe refusal. Start another demo chat to exercise denial. The demo depends on installed dev dependencies and reuses `tests/test_github_write_access.py`'s controlled GitHub transport.

The chat UI, sign-in cookie, CSRF check, decision endpoint, message admission, broker and persistence are real. GitHub and the task runner are controlled in-process fixtures; no provider credentials, model calls or external PR writes are used. The demo changes its disposable run's execution mode while the fixture worker calls the real broker; this is not a cloud-runner demonstration. Ctrl-C removes its disposable database.

Recorded final UI: `/workspace/moyai-captures/pr-session-approval-16621eddbada.webm`.
Final UI screenshot: `/workspace/moyai-captures/pr-session-approved-145af4945a7c.png`.
Responsive inspection: `/workspace/pr-access-1440.png`, `/workspace/pr-access-768.png`, `/workspace/pr-access-320.png`; each inspected viewport had no document horizontal overflow. Approval and denial buttons remain visible at 320px, with 44px touch targets. Chromium inspected with reduced motion enabled; denial and the real follow-up flow were exercised.

## Verification commands

```sh
.venv/bin/pytest -q --tb=line tests/test_github_write_access.py tests/test_github_followups.py tests/test_github.py tests/test_github_checkout.py tests/test_github_identity.py tests/test_github_rulesets.py tests/test_tool_discovery.py tests/test_tool_execution.py
node --require /workspace/pr-node-compat.cjs --test tests/*.cjs
git diff --check
```

The Node compatibility preload only defines Node 18's missing global `File` from `node:buffer`; it does not mock application behavior. Unmodified Node 18 runs have four unrelated audio-recorder failures (`File is not defined`). With that preload the complete Node suite passes: 373 tests. The file contains `globalThis.File = require('node:buffer').File;`. Modern Node versions with the global can run `node --test tests/*.cjs` directly.

The existing follow-up suite was independently run to completion: 75 passed. No claim is made about lead baseline process 71910.

Mutation verification removed the run-ID predicate from grant lookup, ran `test_scope_and_lifecycle_block_writes[session-comment]`, and observed the expected failure because the other chat could write. The source was restored and that case passed. Logs: `/workspace/pr-scope-mutation.log`, `/workspace/pr-scope-restored.log`.

Final results:
- Combined Python command above: **269 passed, 1 skipped**, 1 Starlette deprecation warning, in 151.29s. Log: `/workspace/pr-verification-final.log`.
- After the final approval-event/checkpoint changes, `.venv/bin/pytest -q --tb=line tests/test_github_write_access.py`: **71 passed**, 1 warning, in 53.91s. Log: `/workspace/pr-access-final-confirmed.log`.
- Complete Node suite with the documented compatibility preload: **373 passed**, zero failed/cancelled. Log: `/workspace/pr-ui-compatible.log`.
- `git diff --check`: clean. Python compilation checks passed.
- Demo shutdown with an open event stream: exit 0 in 0.86s after SIGINT, verified after adding the bounded graceful-shutdown setting.

## Limits and boundaries

- No external messages, live PR writes, publication, push, merge, or deployment was performed.
- GitHub has one configured installation in this application. Fork code writes require the head repository to be independently selected and able to receive a contents-write token in that installation; cross-installation forks remain unavailable. Base comments remain separately authorized.
- Slack-to-web requester equivalence uses the existing fresh verified-profile matcher, never accounting/sidebar links. A stale or conflicting Slack profile fails closed.
- Consent lasts in the exact saved chat and requester scope; stopping/deleting the chat revokes it. Connection replacement, closure, merge or retargeted head/base identity prevents writes. No arbitrary approval TTL was added.
- The UI's continuation prepares an editable follow-up; the user sends it through normal queue/resume behavior. Approval alone does not execute a write.
- The optional pinned Hermes integration test is skipped unless `HERMES_TEST_SOURCE` and `HERMES_TEST_PYTHON` are configured.
- The normal app login/CSRF boundary is the consent trust boundary, not a cryptographic proof of human presence. Shared-password logins remain shared app identities.

## Lead verification

The restored patch was exercised again through the browser: pending access, approval, explicit continuation, and successful update/comment receipts through the real broker with controlled GitHub transport. The complete Node suite passed again (373 tests). Stopping the demo exited successfully and removed its disposable database; its open SSE stream logged cancellation during the bounded shutdown.

Reusable lesson: keep publication provenance separate from delegated authority, and bind consent to the exact requester, saved session, connection revision and PR identity. Test both granting and revocation at the final write boundary, including after asynchronous token and metadata reads.
