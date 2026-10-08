# Explicit PR write access for one session

## Evidence and root cause

The current `GitHub.update` and `GitHub.comment` call `owned_publication(target, args.number, version)` before obtaining write access. That function raises when no confirmed workspace publication receipt exists. A human's permission has no server-side representation. Checkout already accepts arbitrary PRs. The existing follow-up boundary tests exercise this denial; the scout also executed both handlers against a controlled external-PR fixture. This is a connector feature, so a local broker with controlled GitHub responses substitutes for a LiteLLM proxy/provider rig.

Prior art: PR #204 intentionally enabled continuation of confirmed Moyai publications across chats. External grants must never become publication receipts, which would leak them to other chats. Open #139 touches GitHub issue tools; #217 touches broker error handling. Shallow history cannot rule out older reverted work.

## Shape

Problem class: resource-scoped delegated authorization with explicit user consent. Use a server-owned grant, separate from provenance. Candidates: (1) trusted UI permission per exact session/requester/PR, consumed by existing tools; (2) per-write payload approval, rejected because permission must last the session; (3) pretending the PR is a Moyai publication, rejected because it grants cross-chat authority. No passkeys or independent identity provider are introduced: use the application's existing authenticated user/CSRF boundary and requester matching, with no model-callable approval operation. Agent instructions must forbid approving their own requests through the browser.

The request tool derives actor/session/connection from server state and reads PR metadata. The authenticated application decision endpoint records approval or denial. It must match the initiating requester, not merely any administrator. The UI names repository, PR, branch and repeated code/comment permission for this session. The agent can request/read status but never pass an `approved` argument. Existing Moyai PR behavior remains supported.

Scope: exact run ID, active requester, GitHub connection revision, base repository ID and PR number, verified head repository ID and branch. A different run (including children) or requester cannot reuse the grant. Session means the saved chat, including its same-requester follow-up turns and restarts. Deleted/stopped sessions cannot write. Closure, merge or changed branch identity blocks writes. Denial/revocation are durable and cannot be reset by retrying a request key. New PRs need new approval. Do not invent a 12-hour expiry that the user did not request.

For forks, code writes need independently enabled write access to the head repository; base access alone is insufficient. Comments target the base. GitHub branch protection remains authoritative; no forced history replacement or protection bypass. Existing optimistic head checks, non-force updates and durable write receipts are preserved. File overwrites mean replacing approved files via a new commit, not clobbering concurrent commits. Check the actual head before writes and reject stale checkout inputs.

## Mechanical questions

- Reachability: wire the request model into real connector discovery/dispatch; both public update/comment handlers consult the same authorization owner. Exercise through broker HTTP, not just a private helper.
- Divergence: grant identity is built once server-side; local checkout files cannot create permission. Keep publication and grant sources distinct but resolve one validated write target.
- Rebinding: distinguish base repository used for PR/comments from head repository used for commits/refs and corresponding token. Never reuse a base token for a fork.
- Call-site census: request tool, update, comment, discovery, sandbox schemas/wrappers, agent instructions, authenticated decisions, session response/UI, and lifecycle invalidation. No raw Git write transport changes.
- Error shapes: validation errors reject malformed requests; missing/denied/revoked permission gives actionable request guidance; provider errors remain provider errors; uncertain writes keep existing readback/idempotency policy. No automatic second write after lost response.
- Carry-forward: derive active requester each call, reread grant and policy before externally visible mutation, preserve intended branch/head across async reads, refresh current PR on subsequent calls. A returned receipt does not authorize another session.
- Hermeticity: isolated Store/temp files, fixed identity and connection fixtures, controlled GitHub transport and head changes, no real tokens or network in regression tests.

## Conditional design walk

- Reachability channels x ceilings: publication and approved grant both retain selected-repo, connection, read-only, run capability, file limits and path guards. Neither grants global repository permission.
- Authorization: direct requester/root session only; other user, session, child, PR, repository and connection denied. Session active_user_id is the authority; creator identity is not a substitute. Pending/denied rows confer nothing.
- Modes/state: absent -> pending -> approved or denied; approved -> revoked; only human decision transitions pending to approved. Duplicate same-key request is idempotent. Stale/repeated decisions cannot silently regrant revoked access.
- New fields/endpoints: initialize/migrate the one actual Store schema; escape untrusted repository/PR titles in UI; validate IDs and status enums; use parameterized SQL. Session deletion and stop leave no usable access. Collection responses expose only appropriate session requests, never credentials.
- Security boundary: broker bearer tokens cannot call authenticated decisions; CSRF enforced; only matching requester can approve/revoke. Document existing authenticated browser as the trusted approval surface, not a cryptographic human-presence claim.
- Retry/cache: never cache a successful authorization across operations; idempotency keys bind arguments and current connection. Unknown provider outcomes remain uncertain until readback.
- Shared helper: preserve all existing publication happy/negative cases, including malformed receipts and fork rejection for publication-derived access. Grant path explicitly handles forks only with head access.
- Async lifecycle/UI: permission persists only in this chat; approval must leave a concrete continuation route without duplicate writes. Reuse existing message/notification mechanics where possible rather than building another orchestration subsystem.

## Verification

Before: existing missing-publication cases reject both operations. After: real broker request -> pending -> authenticated decision -> existing operation writes; then another session/requester/PR fails. Cover denial, revoke, stop/delete, connection/read-only changes, closed/merged/retargeted PR, stale SHA, forks, duplicate request/decision/write, and lost response. Run relevant GitHub/security/tool and UI tests; mutation-check core grant guard. Record a local browser approval flow backed by actual routes and a controlled GitHub service, clearly labeled as a fixture.

## Implementation facts and amendments

- GitHub initializes its own persistence alongside Store. Add the grant table there, not a parallel database. Tool schemas come from `app.github.TOOLS` and flow through connector discovery; ordinary tools already pass through the sandbox bridge, so the request tool needs no custom transport.
- Use a deterministic scope identity (run, active requester, connection revision, base repository, PR) for request idempotency rather than an agent-chosen approval key. Pending and terminal decisions are immutable except approved -> revoked. Bind both base ref and head repository/ref, so retargeting invalidates approval.
- Store lifecycle triggers revoke grants as soon as status becomes stopping or cancelled, or on deletion (a real cancel route may remain stopping while a worker finishes). Restart/token refresh alone does not expire session consent. Each mutation checks current requester and turn; direct child/automation requests are excluded.
- There is one organization installation and selected repository ID list. Fork code writes can only use a head ID independently selected in that installation, with a separately scoped head token. Cross-installation forks are unavailable. Base comments require only base write access.
- Consent does not execute a write. The approval panel offers an explicit continuation using the existing chat composer/message endpoint (which handles queueing, requester changes, and sandbox restoration), avoiding another runner state machine. Pending requests tell the agent to stop and await the human; status can be checked by repeating the request.
- Shared-password accounts remain shared application identities, as elsewhere in the app. This is authenticated UI consent, not a new human-presence protocol; the agent is explicitly prohibited from operating the consent UI.

Requester identity cross-check: `Store.session_view_owner_in` explicitly does not authorize users. PR consent therefore uses the existing `Credentials.same_requester` verifier for Slack/Google equivalence (fresh eligible Slack profile plus independently verified Google identity), never the accounting/sidebar link. The stored initiating actor remains explicit; every use revalidates equivalence. This makes the approval panel and web follow-up usable for Slack-originated chats without granting another user access.

Live UI and persistence: new requests and changed decisions emit the existing approval event so open chats refresh through their established stream. The request and decision routes flush the existing SQLite checkpoint mechanism, preserving consent across saved-container restoration as well as ordinary process restart. Idempotent repeated decisions do not emit duplicate approval events.
