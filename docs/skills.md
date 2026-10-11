# Skills

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Skills

The **Skills** library stores reusable Markdown instructions. Add a name, a
description of when to use the skill, and Markdown text, or import a `SKILL.md`.
You can also ask Moyai in chat to “save this as a personal skill” or, as an admin,
“save this for the organization.” Attach a `SKILL.md` and its supporting text
files; `skills_save` copies the originals by attachment ID into the encrypted
library. It can also write instructions directly, update an existing skill, add
or replace references, and explicitly remove files. Only sent attachments in the
current session through the executing message can be imported. New skills default
to Personal in chat and in the Add skill form. Organization publishing requires
an explicit request and an admin; editing preserves the existing selection. Keep API keys in
**Secrets**, not in skill text.

- **Personal:** only the owner can view, edit, or use the skill. The current
  message's verified Google identity controls access, not the session creator.
- **Organization:** every signed-in teammate can view and use it. Admins can
  create and maintain shared skills; only the original owner can change its
  sharing. Archiving removes a skill from use and is reversible.

Type `/` in a new-session or follow-up composer to search an inline list of your
personal and organization skills. Use arrow keys and Enter/Tab, or click a skill;
selection inserts its explicit scoped reference without sending or replacing the
rest of your draft. Escape closes the menu and Shift+Enter still adds a new line.
`/skill` opens the same list. The **Skills** button also remains available.

Write `/personal:benchmark-review`, `/org:benchmark-review`, or `/skill benchmark-review`
to invoke a workflow; existing `$personal:benchmark-review` and `$org:benchmark-review`
references still work. Slash references inside code, URLs, and file paths are not
automatically loaded. The first model call also receives an authorized directory,
fitted to the selected model's input window. Task keyword matches rank first, then personal skills,
then name; only the current requester's message and acknowledged steering supply
ranking keywords. No model call or embedding lookup is needed to build it.
Moyai can call `skills_load` directly for a relevant entry. Full instructions
enter context only when explicitly invoked or loaded.

The directory reports an omitted count. `skills_search` searches the full
authorized library when the directory is insufficient. An empty query lists every
authorized entry; follow `next` with `cursor` until it is null. `page_size` controls
transport, with no total result cap. Catalog changes reject a stale cursor so a
caller can restart enumeration. Search uses names and descriptions, never instruction bodies. Search
results and loaded skills take priority and are not repeated in the directory.
The full library remains available in the Skills page and composer picker.
Unqualified `/benchmark-review` or `$benchmark-review` prefers a personal skill over the same name in
the organization library. Slack sessions use the same references; personal
access requires a fresh eligible Slack email matching verified Google SSO, not
an accounting-only identity link. Saving from Slack requires that matching user
to have signed in with Google at least once; the saved owner is their Google
identity, and admin status is checked against the current SSO configuration. Subagents have the current requester's skill
access and can load a skill named in their assignment.

Definitions are encrypted at rest. Directory entries, searched descriptions and loaded definitions
are injected privately into inference by the server, rather than returned in
sandbox tool results or copied into workspace files. Search results replace the
previous search selection and remain scoped to the current requester and turn.
Each turn pins the revision it first loads, including across durable resumes;
later turns use the latest revision. Permissions and archive status are checked
again on every model call. There is no per-turn skill-count limit; explicitly
requested and tool-loaded skills remain available for that turn. The model's
context budget still applies to the full request, including skill instructions.
There are no fixed skill-count or instruction-length quotas. An omitted body is
explicitly marked and remains readable by range. Skills cannot bypass tool permissions, provide
credentials, or approve writes. Personal skills do not make shared session
outputs private; generated results keep the session's existing sharing.

Skills support complete UTF-8 supporting files, subject to ordinary transport and
storage availability. `skills_load` returns a file manifest; `skills_read_file`
privately reads `SKILL.md` or supporting-file ranges using `offset` and `limit`
(16,000 characters by default). Follow `next_offset` to reconstruct a file.
Every read receipt is retained; the inference view prioritizes recent requested
ranges and exposes omissions. File contents never appear in broker
tool results. References are versioned alongside instructions and retain the
loaded revision through restarts. Editing in the library preserves supporting
files, which can be downloaded from the editor by authorized users. Saving a
skill does not execute scripts or grant additional tool permissions.

Agent saves use a stable `request_id` per turn and an `expected_revision` for
updates. The skill, reference bundle, audit entry and replay result commit in one
transaction; an identical replay returns the original result, while conflicting
edits fail without overwriting newer work. New turns pick up saved changes.
Expected skill-tool rejections (such as unavailable skills, invalid content or
revision conflicts) return an error receipt so the agent can correct or report
the problem and continue. Authentication, server and checkpoint failures still
stop the request.

Public procedures shipped with the agent are listed in the initial context with
content revisions. `bundled_skill_read` reads their full instructions or ranges
on chat, automation, and delegated surfaces. They are independent of a user's
private library. The shared `repository_guidance` tool resolves workspace
`AGENTS.md` files from root to the directory being edited, with scopes, hashes,
and complete range reads. Native duplicate guidance loading is disabled.

## Suggestions learned from completed work

In **Settings → Skills**, enable **Suggest skills** to have Moyai draft reusable
procedures after future tasks finish. It is off for each user until enabled.
No historical scan runs on startup. Learning is separate from personal memory:
preferences belong in Memory; concrete repeatable procedures belong in Skills.

Suggestions are private, encrypted drafts. Open **Review** to inspect the reason,
exact supporting requests and successful command records, edit the instructions,
and choose Personal or Organization before saving. Organization publishing still
requires an administrator. Updates are proposed only for your own personal skills;
acceptance checks the original revision and preserves supporting files. Dismissed
names are suppressed within the repository; a dismissed update is suppressed for
that target revision. This is exact deduplication, not a guarantee against all
semantic paraphrases. Turning learning off cancels queued reviews and removes
unaccepted drafts; saved skills remain available.

The first version learns only from completed, non-automated root chat turns in
single-requester conversations. A verified personal account (including a fresh
matching Slack identity) is required. Demo runs, subagents, failed tasks, deleted
sessions and conversations with other requesters are excluded. Evidence is checked
again before a draft is written or accepted. Changed/deleted sources invalidate
the draft without treating it as a dismissal: later valid work can propose the
same workflow again, using a new draft ID. No private tool payloads, raw tool outputs, reasoning or native SDK
history are sent to the reviewer. Secret-pattern rejection is defense in depth,
not a guarantee of detecting arbitrary secrets.

Each review uses at most three eligible requests from the same user and repository
within 14 days of review, captured since enabling learning. It includes bounded
final answers and up to eight successful terminal command summaries per request.
A final answer is a claim, and exit code zero alone does not establish correctness.
A proposal must cite the current request plus either a successful command or a
second request demonstrating recurrence. Human review determines whether the
procedure is useful and accurate. Model quality has not been established by the
synthetic transport tests.

This keeps the harness thin: settlement enqueues an ID; a tool-free maintenance
worker proposes at most one draft; accepted procedures use the existing versioned
library, bounded metadata index and on-demand loading. No model review or extra
container acquisition occurs before the task's first response.

Operational bounds:

At 20 pending drafts, queued reviews wait for capacity instead of being discarded.
The worker defers that person's due backlog for 60 seconds without consuming retry
attempts, while other people's reviews can continue. Accepting or dismissing a
draft frees capacity; the next due review can proceed. Invalid source evidence is
retired during listing and review claiming, so it cannot hold a slot indefinitely.

- `SKILL_LEARNING_ENABLED=true` permits the feature; personal opt-in is still required.
- `SKILL_LEARNING_MODEL` defaults to the completed turn's approved model.
- `SKILL_LEARNING_IDLE_SECONDS=60` delays review after completion; busy sessions defer again.
- `SKILL_LEARNING_TIMEOUT_SECONDS=60` bounds a review. Transport output is capped at
  64,000 bytes, completion at 3,072 tokens, serialized input at 48,000 characters.
- At most 20 pending drafts per owner; at most three attempts per captured turn.
  Foreground inference can reclaim the shared model slot. Spend is attributed to
  the original requester and turn. Reviews add background model cost.
- The inference owner alone recovers/runs reviews: the separate broker, or the
  standalone/coordinator process when no separate broker is configured. API
  replicas and execution workers never start this worker. With verified broker
  overlap, only the broker holding maintenance ownership runs learning; handoff
  waits for its reviews to drain before a replacement starts.
- Schema revision **15** adds the preferences, queue and draft tables after the
  released sign-in, swarm and rolling-release migrations. This release
  needs the normal coordinated schema migration with runtime owners stopped;
  it is not an API-only compatible release. See [schema migrations](schema-migrations.md).

Run `uv run python scripts/skill_learning_demo.py --port 8842`, then open
`http://127.0.0.1:8842/demo/login` for a local demo. The task history and model
response are simulated; review, editing, acceptance, storage and skill loading use
the real APIs. `/demo/recall` checks the saved skill through normal broker context.
The temporary local database is removed when the server stops.
