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

With automatic saving enabled, the agent reviews the current requester's messages
before its final answer and saves lasting preferences, corrections, decisions or
references. You do not need to say “remember this.” It searches first to avoid
duplicates and checks that the save succeeded. One-off task instructions and
temporary status do not become memories. The note and supporting quote go only
to the private `memory_save` tool, not files, unrelated tools or chat.

- `memory_search` retrieves up to five relevant notes, with an 8,000-character
  total payload budget. Nothing from the library loads until the agent searches.
- `memory_save` creates or updates a short note. Agent writes require an exact
  supporting quote from the current requester’s message, including acknowledged
  steering inputs. This validates provenance, not the model’s interpretation.
- `memory_forget` removes a selected note when its owner asks. Deletion clears its
  stored body and active references; opaque tombstones prevent retry resurrection.

These tools use Hermes tool search. Ownership comes from authenticated Google
identity or a fresh, eligible Slack email match, never an accounting link or a
model-supplied user ID. Each model call rechecks requester, settings, expiry,
repository scope and current note revisions. Subagents and automation runs can
recall authorized notes, but cannot automatically write personal memories.

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
prompt traces. Memory tool payloads are omitted from tool traces and scrubbed
from saved conversation tool calls. Responses in shared chats can still reflect
remembered context; memory does not make those responses private. Deletion does
not erase previous conversations, inference requests, or retained backups.

Each library supports 200 notes, at most 1,200 characters per note. Project and
reference notes expire after 90 days unless edited; preferences and corrections
remain until deleted. Expired notes remain reviewable but are not retrieved.
Known credential patterns are rejected; arbitrary sensitive data cannot be
reliably detected, so keep secrets in the credential vault.

This version uses bounded keyword retrieval and agent-authored notes. It does
not embed or search whole transcripts, run background summarization, or backfill
old chats. Shared reusable procedures belong in **Skills**. The agent may spend
additional tool rounds finding and saving memories; there is no separate model
or vector database running on every message.

To evaluate automatic capture with a real model, set `GATEWAY_BASE_URL` and
`GATEWAY_API_KEY` and run `uv run python -m scripts.memory_capture_smoke`.
This opt-in, billed check uses synthetic messages, a temporary database and the
real memory broker. It checks implicit preferences, correction without duplicate
notes, recall in a fresh session, one-off requests, quoted third-party text and
manual mode. It tests model choices with the production memory prompt and tools;
it does not run a complete SDK or cloud sandbox, and is not a guarantee that
every future preference will be captured. Use `--model` to test another model
and `--report path.json` to save the evidence.

The design draws on official [Codex memories](https://developers.openai.com/codex/customization/memories),
[Claude Code memory](https://code.claude.com/docs/en/memory), and other coding-agent
knowledge documentation (reviewed October 2, 2026): scoped recall, concise notes,
provenance, user controls, and separation from required team instructions. Reusable
guidance belongs in Skills. This is Moyai’s implementation, not a claim of
exact product parity.
