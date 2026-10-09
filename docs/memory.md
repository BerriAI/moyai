# Personal memory

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Personal memory across sessions

**Settings → Memory** holds personal preferences, corrections, project context,
and references. Automatic saving and recall are enabled by default for verified
users. Choose **Only save manually** to stop agent writes while keeping recall,
or **Pause memory** to stop both. You can review, edit, and delete each note.
Shared passwords cannot use personal memory; local demo access has its own library.

With automatic saving enabled, the agent is instructed to save useful context as
soon as it comes up, before continuing unrelated work, without waiting for a final
answer. This is model-selected behavior, not a guarantee that every fact is saved.
A separate background reviewer also checks successfully completed turns, so
capture does not depend on the task agent remembering to call `memory_save`.
You do not need to say “remember this.” Preferences, corrections, decisions and
references can become notes; one-off instructions and temporary status do not.
An empty review is a normal result, not a reason to invent a memory.

General working preferences default to personal scope across repositories, even
when the session has a repository selected. Only repository-specific notes use
the `selected_repository_url` supplied in the memory context. Mentioning or
checking out a repository does not select it for memory. A current-task constraint
such as “do not merge this PR without my permission” stays in the conversation.

User preferences, corrections and decisions require an exact quote from the
requester's message. Corrections preserve the mistaken assumption, desired
alternative and future rule when supported, without broadening what the user said.
The agent can also save non-obvious repository/environment lessons it verifies
during execution, with an `observation` containing concrete `scope` and concise
`evidence` instead of user-quote fields. Scope must appear in the note content.
These project/reference notes are labeled **Observed during work**, with scope and
evidence visible under **Why Moyai saved this**. They are agent-reported observations,
not server-authenticated tool receipts; they cannot establish user preferences or
permission, overwrite user-backed/manual notes, or change an existing observation's
scope. Tool/file/web instructions are not memory directives. Recheck observations
when the environment changes. Selected-repository matching is enforced; without a
selected repository, the named environment scope is guidance, not a retrieval partition.

The reviewer waits 60 seconds after completion and defers while the session has
queued or running messages. It uses the completed requester's messages and
acknowledged same-requester follow-ups, never other participants, assistant text,
tools, files or a whole shared transcript. It compares existing notes, reuses keys
for corrections, and requires an exact supporting quote for every new or updated
note. Sources remain visible under **Review & edit → Why Moyai saved this**.

On startup it seeds the queue from at most 50 recent completed turns from the
last seven days. Already reviewed turns are not repeated. This bounded seed can
recover useful preferences missed before deployment; it is not a complete
historical import. Demo, failed, cancelled, subagent and automation turns are
excluded. The Memory page shows pending reviews and whether the most recent
review saved notes, found nothing new, or failed.

- `memory_search` retrieves up to five relevant notes, with an 8,000-character
  total payload budget. Nothing from the library loads until the agent searches.
- `memory_save` creates or updates a short note immediately. User-backed writes
  require an exact supporting quote from the current requester’s message, including
  acknowledged steering inputs. Observations instead require bounded scope and
  evidence, and are bound to the active turn by the server. Source checks do not
  validate the model's interpretation or prove an observation correct.
- `memory_forget` removes a selected note when its owner asks. Deletion clears its
  stored body and active references; opaque tombstones prevent retry resurrection.

These tools use Hermes tool search. Ownership comes from authenticated Google
identity or a fresh, eligible Slack email match, never an accounting link or a
model-supplied user ID. Each model call rechecks requester, settings, expiry,
repository scope and current note revisions. Subagents and automation runs can
recall authorized notes, but cannot automatically write personal memories.

Rejected memory tool arguments, scope, permission or revision checks return an
error receipt so the agent can correct supported arguments or continue without
saving. A rejection never counts as a saved note and must not be bypassed by
broadening its scope. Broker authentication and storage/connection failures keep
their failure behavior; manual Settings API error statuses are unchanged.

Slack profile verification refreshes automatically every 30 minutes, ahead of
its one-hour authorization limit; users do not need to sign in again. The worker
also refreshes older eligible profiles still carrying a daily refresh timer.
Failed lookups retain the five-minute retry delay and disable personal access
until verification succeeds.

Run `uv run python scripts/memory_refresh_demo.py` to exercise recovery, refresh
across two hours, and recall in a new session through the local broker. The demo
uses a synthetic Slack profile and an advanced clock, without external calls.

Notes (including titles and source quotes) are encrypted in the existing durable
SQLite database. Preserve the database and encryption key across deployments;
Temporal and sandbox snapshots are not the memory store. Retrieved note bodies
are injected only at the model broker and excluded from tool results and system
prompt traces. Memory tool payloads are scrubbed from saved conversation tool
calls. To also omit them from exported tool traces, turn on **Settings →
Preferences → Hide private tool content in traces** (off by default). This account
preference also covers credential, skill, and connector tool payloads in new
responses; secret redaction stays on either way. Responses in shared chats can still reflect
remembered context; memory does not make those responses private. Deletion does
not erase previous conversations, inference requests, or retained backups.

Each library supports 200 notes, at most 1,200 characters per note. Project and
reference notes expire after 90 days unless edited; preferences and corrections
remain until deleted. Expired notes remain reviewable but are not retrieved.
Known credential patterns are rejected; arbitrary sensitive data cannot be
reliably detected, so keep secrets in the credential vault.

This version uses bounded keyword retrieval and concise notes. It does not embed
or search whole transcripts or maintain a vector database. Shared reusable
procedures belong in **Skills**. The background reviewer makes a separate,
tool-free model request through the existing gateway, billed to the original
requester and turn in the normal usage ledger. It uses the turn's configured
model unless `MEMORY_REVIEW_MODEL` selects another enabled workspace model.
Foreground model requests can reclaim its slot. Each input has at most three
attempts; failures never fail or replay the user's task.

Queue admission and turn completion commit together. Saving the review's notes
and marking the job complete are also atomic. Original identity, settings,
source text, repository and note revisions are rechecked before writing. Manual
edits, forgetting and settings changes invalidate older background inputs, so
restarting or re-enabling memory does not restore deleted notes from old chats.
Only IDs, status and counts enter job records and activity events; generated
notes and supporting quotes remain encrypted in the memory store.

Configuration: `MEMORY_REVIEW_ENABLED` (default `true`), `MEMORY_REVIEW_MODEL`
(default empty), `MEMORY_REVIEW_IDLE_SECONDS` (60),
`MEMORY_REVIEW_TIMEOUT_SECONDS` (60), `MEMORY_REVIEW_BACKFILL_LIMIT` (50).
Setting the backfill limit to zero disables the startup seed. Turning background
review off leaves existing task-agent memory tools and user preferences intact.

To evaluate automatic capture with a real model, set `GATEWAY_BASE_URL` and
`GATEWAY_API_KEY` and run `uv run python -m scripts.memory_capture_smoke`.
This opt-in, billed check uses synthetic messages, a temporary database and the
real memory broker. It checks a preference save before task tools, a real scratch-file
fixture observation saved before the next unrelated task step, correction without
duplicates, fresh-session recall of both sources, one-off requests, quoted text and
manual mode. Partial case reports remain available if a later assertion fails.
It tests model choices with the production memory prompt and tools;
it does not run a complete SDK or cloud sandbox, and is not a guarantee that
every future preference will be captured. Use `--model` to test another model
and `--report path.json` to save the evidence.

Add `--scope-only` for five focused cases: a general preference with a selected
repository, recall from another repository, a one-task approval constraint, a
mentioned repository with no selection, and an explicit repository-specific
decision. The same live-model limitations apply.

To exercise the background path with a live model, run
`uv run python -m scripts.memory_review_smoke --model openai/gpt-6-astra --report report.json`
with those same gateway variables. This uses a fresh synthetic database, settles
turns with **no agent save calls**, then runs the actual reviewer and checks
capture, correction, duplicate handling, no-op tasks, quoted third-party text,
cross-session recall and manual mode. It does not execute a cloud coding task.

## Comparison that informed this implementation

Reviewed October 8, 2026 against official documentation and LiteLLM source:

| System | Documented collection behavior | Applied in Moyai |
| --- | --- | --- |
| [LiteLLM memory](https://docs.litellm.ai/docs/proxy/memory) | User/team-scoped CRUD storage. [Endpoint implementation](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/memory/memory_endpoints.py) does not extract memories from chats. | Retain scoped, durable storage and separate capture from persistence. |
| [Devin Memory and Dreaming](https://docs.devin.ai/product-guides/memory) | Saves notes during work; background dreaming consolidates notes, captures missed lessons and seeds from recent sessions. Notes preserve source links. | In-session saves plus background capture, bounded recent-session seed, source evidence and correction merging. |
| [Codex local memories](https://learn.chatgpt.com/docs/customization/memories) | Background generation from eligible prior chats after idle time; separate extraction and consolidation settings. | Durable review queue, idle deferral, bounded inference, separate setting and no dependence on task-agent tool choice. |
| [Claude Code auto memory](https://code.claude.com/docs/en/memory) | Selectively records user preferences, feedback, project context and references; skips one-off work and cheaply recoverable facts. | Keep these categories and quality filters; allow a review to save nothing. |

Moyai keeps its encrypted server-side store and current search-based recall.
It does not clone Devin's Git memory drive or daily pruning, Codex's local file
pipeline, or Claude's startup `MEMORY.md` index. These are behavior references,
not a claim of identical internal implementations.
