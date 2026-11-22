# Private cross-chat recall

The native agent can use `private_history_search(query)` and
`private_history_read(chat_id, message_id, query="")` to recall facts from the
current individual's other private native web chats. This works independently
of saved personal memory, including when memory is disabled.

Search matches a literal, case-insensitive phrase in user/assistant message
text. Results contain the source chat title, native chat URL, message ID, role,
timestamp and a short excerpt. Read returns a larger excerpt of one exact
message, optionally centered on the same phrase. Chat URLs open the existing
chat; message IDs identify the evidence, without inventing a message-scroll
route. Titles and excerpts are untrusted evidence, never instructions,
permissions, or authority to execute actions.

## Limits

- Search considers at most 30 recently updated eligible chats and their 200
  most recent eligible messages. It returns at most five 320-character excerpts.
- Only messages preceding the current turn are eligible. The current chat is
  excluded. Returning to an older chat can recall a later-created chat.
- Messages above 48,000 encrypted bytes are skipped before retrieval. Matching
  examines at most the first 8,000 characters; exact-message read returns at
  most 1,200 characters. Each operation has a ten-second timeout.
- No pagination, SQL input, transcript export, embeddings, external provider,
  saved memory duplication, or tool/working-step search is added. Empty results
  do not prove a fact was never discussed.
- Legacy product chats are outside this native-only feature. Archived private
  chats remain eligible; deleted chats do not.

## Authority

Tools are offered only to the direct root agent of a private web turn with a
saved product grant. Every call independently verifies the current individual,
immutable turn grant, running execution, connections, current chat ownership
and visibility, and the existing `allowed_session` checks.

Persisted delegation, swarm, goal and automation provenance is checked in
addition to supplied scope fields. `Database.human_origin` validates direct
human input and genuine approval resumptions, including the native interval
automation receipt window before its turn ID is linked. Source messages use
the same persisted ancestry check before decryption. Historical grant versions
establish provenance only; they never grant current execution or read access.

Both search and read repeat current authority and source checks after async
work. Release uses a fresh transaction to reread source messages and drops
references whose ownership, privacy, availability or content changed. Neither
an administrator nor a linked Slack identity can read another person's
private history. Shared, Slack, delegated and autonomous executions cannot
recall this history.

There are no HTTP API, protocol, storage schema or UI changes. Registration is
limited to the capability import, capability entry and read-tool timeout
metadata in `agent.py`. All model tool calls retain the existing
`Authority.wrap_tool_execute` → `ToolCaller.call` path.

## Verification

Run the focused PostgreSQL regressions using a disposable local database:

```sh
cd native
MOYAI_TEST_POSTGRES=postgresql://localhost/postgres \
  uv run pytest -q tests/test_private_history.py
```

The tests cover bounds, citations, literal queries, source/caller revocation,
ownership, deleted/shared/Slack sources, delegates, persisted interval
receipts, actual database approval resumptions, automation ancestry, and
returning to an older chat.

For actual application proof, run baseline and candidate through `moyai dev`
with separate local PostgreSQL databases, Temporal data directories, and
authenticated synthetic individual accounts. Disable personal memory. In one
private chat, give a distinctive synthetic project fact and explicitly keep it
out of memory. In another private chat, ask to retrieve that fact and cite its
source. The baseline can only search the current conversation; the candidate
can search and read the source. Repeat as a second individual and from a
shared chat to verify isolation. Use a real configured model, record the
actual UI, and retain the source and answer IDs. Fixtures are supplementary.
