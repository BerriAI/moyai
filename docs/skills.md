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
current session through the executing message can be imported. If you have not
specified Personal or Organization, Moyai asks which you want and waits before
saving. A `skills_save` call with no scope returns `scope_required` and writes
nothing. The library's Add skill form also requires a choice instead of
preselecting Personal; editing preserves the existing selection. Keep API keys in
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
automatically loaded. The first model call also receives a directory of up to
12 authorized skill references, descriptions and revisions, bounded to 6,000
serialized characters. Task keyword matches rank first, then personal skills,
then name; only the current requester's message and acknowledged steering supply
ranking keywords. No model call or embedding lookup is needed to build it.
Moyai can call `skills_load` directly for a relevant entry. Full instructions
enter context only when explicitly invoked or loaded.

The directory reports an omitted count. `skills_search` searches the full
authorized library when the directory is insufficient, returning up to five
matches. Search uses names and descriptions, never instruction bodies. Search
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
Library limits are 32,000 instruction characters per skill, 50 personal skills per user and 200 shared skills
(including archived entries). Skills cannot bypass tool permissions, provide
credentials, or approve writes. Personal skills do not make shared session
outputs private; generated results keep the session's existing sharing.

Each skill supports up to 20 UTF-8 supporting files, 1 MiB each and 4 MiB total.
`skills_load` returns a file manifest; `skills_read_file` privately loads bounded
excerpts (up to 24,000 characters, with optional text search). Only the four most
recent excerpts stay in inference context. File contents never appear in broker
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
the draft. No private tool payloads, raw tool outputs, reasoning or native SDK
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
  replicas and execution workers never start this worker.
- Schema revision **4** adds the preferences, queue and draft tables. This release
  needs the normal coordinated schema migration with runtime owners stopped;
  it is not an API-only compatible release. See [schema migrations](schema-migrations.md).

Run `uv run python scripts/skill_learning_demo.py --port 8842`, then open
`http://127.0.0.1:8842/demo/login` for a local demo. The task history and model
response are simulated; review, editing, acceptance, storage and skill loading use
the real APIs. `/demo/recall` checks the saved skill through normal broker context.
The temporary local database is removed when the server stops.
