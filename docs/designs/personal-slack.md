# Optional personal Slack connections

## Evidence and scope

Base acecc76010af3ccd5c86b93052dc4429c8429132. Two independent scouts completed. Prior art: merged #226 preserves verified requester identity; local history is shallow and contains no reverted personal-Slack work. Open-PR census is not available through the exposed listing tools, so do not claim it was completed.

Unmodified real FastAPI app, SQLite, authenticated TestClient (no external Slack credentials): GET /api/connections/slack/personal returned 404; Bob GETting Alice's run returned 200. This is a declared in-process ASGI reproduction substitution for a LiteLLM proxy rig. Subsequent verification must also run the changed application over HTTP and show selected synthetic upstream identity.

Root cause: connections is keyed only by provider, credentials() executes SELECT * FROM connections WHERE provider=?, and browser run endpoints authenticate workspace membership without an owner ACL. Personal credential selection therefore requires a durable privacy boundary for resulting content.

## Shape

Problem classes: OAuth delegated authorization, credential precedence, and information-flow confinement. Established local solutions are owner-scoped skills/memory storage, verified requester resolution in AutomationTools.actor, and single-use browser-bound OAuth state. Reuse these conventions; native checkpoint invalidation alone does not protect published results.

Candidates:
1. Add personal token selection to existing shared chats. Loser: persisted results remain readable by every member with the URL.
2. Filter private Slack messages out. Loser: fails the requested DM/group-DM capability and cannot establish arbitrary channel membership from message output.
3. Separate encrypted personal grants plus immutable owner-only sessions. Selected: grants are optional; absence retains existing organization behavior. Personal access in a shared session returns an actionable error, never falls back. No conversion of already-shared sessions.

Decisions are separate: personal grant preference is unconditional when configured; eligibility of the current session is a separate denial; bot sends remain organization-owned. Initially support private web sessions, with explicit user-visible limitations for Slack shared threads/automations rather than silently claiming all surfaces work. The private session mechanism must be usable via UI and preserve personal reads across turns. Explicit disconnect restores shared fallback, but never makes prior private sessions public.

## Contracts

GET /api/connections/slack/personal returns connected, available (individual sign-in eligibility), oauth_configured, label, effective_source (personal/organization/none), health, and a plain limitation/description where relevant. POST same path + /oauth starts personal user-only OAuth; POST + /check verifies current grant; DELETE disconnects. All are requester-owned, with CSRF for mutations. The existing /oauth/slack/callback may dispatch a scoped state or a dedicated callback may be used consistently in redirect_uri. Keep organization controls admin-only.

New run request accepts private_session:boolean default false. Set immutable private_owner_id on creation to a verified individual (reject shared-password/local identities except an explicitly isolated test fixture). UI exposes an owner-only new-chat option and offers it from personal Slack settings. Existing shared chats retain behavior; a configured personal grant there denies read with instructions to start a private chat. Private chats cannot be converted/shared. For first version, block private automation creation, side-chat export and delegation if their inheritance cannot be comprehensively enforced. Prefer safe explicit denial to a partial privacy implementation.

## Mechanical questions

- Reachability: broker discovery, broker execution, automatic Slack source context, session creation and automation validation must use the same read selection owner. Ordinary reads receive server run context; as_bot reads/sends keep their own path. Verify through public broker HTTP, not only helper calls.
- Divergence: one personal-grant service owns requester mapping, existence, refresh and selected identity. No UI or caller re-derives credential choice. One session privacy service owns authorization and derivative checks.
- Rebinding: capture requester, turn, capability and grant revision before awaits; re-read before returning content. Never trust tool-supplied requester IDs.
- Call-site census: classify all raw credentials('slack'), allowed(), execute(), run read/list/search, events, artifact, attachments, captures, browser/WebSocket, media sharing, side-chat, agents, automation-history and Slack mirroring callers. Inventory exact sites during implementation and enforce each or deny the unsupported path.
- Error shapes: absent grant alone permits fallback; expired/revoked/decryption/refresh/network/rate-limit/provider errors keep the grant and return a redacted actionable error. Authorization ambiguity denies. OAuth rejection consumes state without modifying existing grant. No bearer strings in errors.
- Carry-forward: private owner never clears on disconnect, restart or turn change. Derived content retains its private owner or export is denied. Refresh uses owner lock plus revision CAS so disconnect/reconnect cannot resurrect stale credentials. Recheck post-await before response release.
- Hermeticity: tests pin individual users, profile freshness, OAuth state/time, policy, installation, provider transport and token sentinels. No live keys, model inference or environment-dependent provider reads.

## Conditional design walk

Authorization: individual Google/Cloudflare owners may connect; verified Slack mappings use established same_requester logic, stale/ambiguous mappings deny. Shared login cannot own personal grants. Admin role grants no private-session read override. Org enabled/read-only policies and session plugin gates still apply to personal access. A grant is reachability, not a policy waiver.

Channel x ceiling: web/private => personal grant + immutable owner + org policy + plugin; web/shared, Slack shared, automation/unsupported derivatives => configured personal grant denied; absent personal grant => existing shared account subject to existing ceilings; bot => existing independent bot authorization. No new budget/rate-accounting system is introduced.

Storage/lifecycle: separate encrypted personal-grant table; explicit owner key; row absence means no configured grant; invalid grants remain present. Single authoritative SQLite schema. Credential rotation uses the existing encryption service. Removal deletes grant only; privacy metadata persists. State consumption is transactional; refresh guarded by revision and owner lock. Indexed owner PK lookups need no speculative query redesign.

Modes: all personal states (absent/healthy/expired/revoked/transient failure), session modes (shared/private), identities (individual/shared/Slack verified/ambiguous), and org enabled/read-only switches receive explicit test matrix coverage. Unsupported combinations fail at creation or before any private provider read.

Caching/refresh: usable returns selected personal; absent uses shared; definitive failure asks reconnect; transient failure allows retry of same identity. Refresh cannot change identity or resurrect deletion. Avoid caching absence.

Data propagation: OAuth preserves team/user IDs and scopes, personal exchange does not save organization bot state. Wire/reporting identities remain distinct. Settings labels never expose tokens. Private content cannot enter organization-visible traces, search snippets, archives, public media links, inherited workers or automatic mirrors; deny unsupported sinks before reads.

UI: reuse settings primitives and render-version guard, accessible controls, reconnect/check/disconnect, effective identity and disconnect consequence. Show private-session limitation plainly. Verify real rendered UI and actual backend behavior separately; a fixture screenshot is not OAuth proof.

Testing: token-sentinel workflows prove preference, fallback only on absence, no fallback on failures, policy enforcement, private ACL and OAuth ownership/replay. Mutation-remove selector/ACL to prove core tests fail. Run existing organization/bot workflows to preserve compatibility.

## Backend integration refinements

The combined implementation registers the personal service directly on the app, uses
one authoritative broker read path in tests (no fixture adapter), and forwards the
verified browser actor to capture listing. Restored or inconsistent automation history
must exclude private runs both from public history and future prompt construction.
Broker policy rejection preserves the personal eligibility guidance rather than
mislabeling a configured-personal/shared-chat denial as an organization policy failure.

Owner-record loss is not proof that a configured grant disappeared: if a persisted
individual grant still exists but its user record cannot be resolved, deny the read
and require sign-in instead of treating it as organization fallback. Disconnect
retains a revision-only tombstone (no encrypted grant) so late OAuth/refresh writes
cannot resurrect access; this is logical absence of a grant, not a failed grant.

Initial Slack source preparation may be invoked before the first queued message is
claimed (the legacy Slack insert sets owner but leaves active_user_id empty). The
selector uses the persisted owner when there is no active requester, so a configured
personal owner is denied in this shared context instead of silently using the
organization token. Once a turn is active, its requester always takes precedence.

Organization compatibility: an individual row created by legacy organization-only
workflows with individual login disabled is not itself a personal grant. It retains
organization fallback when no personal grant exists. A configured grant still
requires a currently eligible individual and fails closed when login is disabled.

## Integration lessons

Credential precedence and the audience of persisted results are independent contracts.
A requester-owned token cannot be introduced safely by changing only the adapter:
session lists, stream endpoints, artifact reads, traces and derivative workflows
must enforce the same durable owner. Disconnect revokes future credential use,
while previously stored private results keep their owner. HTTP and browser
verification therefore exercise selection, audience and disconnect separately.

Final integration was rebased onto 1abf8ff; the only manual conflict was the script
include list, preserving newer activity/sandbox assets and the personal Slack entry.
