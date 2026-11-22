# Personal instructions for private chats

Settings → General lets an authenticated individual save up to 4,000 characters
of writing preferences, or clear them. Saving applies to subsequent model requests
in direct private web chats, including new chats. A request already in flight keeps
its original input. Clearing stops future injection; it cannot erase earlier
assistant replies or facts the user separately saved in memory.

Instructions are user-level input, subordinate to system rules and the current
request. They cannot grant tools, credentials, permissions, or approvals. The
capability adds a request-only `UserPromptPart` through the pinned Pydantic AI
`before_model_request` hook. It neither mutates persistent message history nor
changes system `instruction_parts`, model settings, or tool grants.

## Storage and concurrent edits

The existing native `User.preferences` JSON stores a `private_instructions`
envelope with `revision` and `sealed`. The native `Security.seal` Fernet encryption
protects instruction text together with its owner ID. A copied ciphertext for
another owner is never loaded. Empty instructions retain only the revision and
an empty sealed value. No database migration is required.

The existing `/api/settings/general` PATCH still merges only supplied fields
under the user-row lock and preserves unrelated keys. Instruction writes additionally
require `personal_instructions_revision`; stale writes return 409 atomically.
The editor preserves the draft on failure and offers an explicit reload.
Pydantic owns both new fields and the generated OpenAPI/TypeScript types.
Preference reads revalidate the canonical grant without taking the admission
gate, releasing the native connection first so one-slot pools remain usable.

## Scope and durable execution

Each model request performs a bounded durable read without a cross-user cache.
The loader rechecks live execution authorization, individual identity, persisted
session owner/visibility/surface, and human origin. `Database.human_origin`
verifies approval-resumption fingerprints and excludes automation provenance,
including the interval schedule receipt's pre-link window.

Shared chats, Slack, delegated sessions (including private-looking children),
SDK delegates, schedules, goals, and swarms do not load these preferences.
Persisted goal/swarm membership and Slack-thread links are checked as well as
scope flags. These exclusions are deliberately conservative: unattended work
must not silently acquire new private preferences. A future extension requires
an explicit persisted authority design.

The workflow patch marker preserves replay behavior for workflows created
before this capability existed. New durable read results use the application's
existing encrypted Temporal payload codec.

## Verification

`uv run pytest native/tests/test_private_instructions.py` covers encrypted
storage, owner binding, partial merging, stale revisions, clearing, request-only
user-role injection, excluded contexts, and a native Temporal run and replay.
`npm run test:ui` includes responsive light/dark editor behavior, conflict
recovery, keyboard actions, route changes, and shared-sign-in handling.
These tests supplement the actual local baseline/candidate application and
real-model evidence attached to the pull request.
