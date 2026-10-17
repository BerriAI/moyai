# Ordinary subagent conversations

Ask a normal chat to delegate three self-contained tasks. New `delegate_task`
calls create three persistent native sessions. Open **Workspace → Subagents**
and select a child to use the same transcript, composer, queued input,
**Send now**, questions, files, activity and Stop controls as any other session.
**Parent chat** returns to its direct parent.

The left sidebar nests subagents under their parent, with an expandable group.
Selecting a name opens the full conversation. **Open in side chat**, available
beside the child and in its menu or Workspace row, opens that same session in
the right panel while preserving the main conversation and its draft. The
side chat has its own composer, queue, **Send now** and Stop controls.
Reopening the same child selects its existing tab and retains the side-chat draft.

An authorized parent outside the current history page is included as sidebar
context without consuming pagination slots. Missing or unavailable parents do
not hide accessible children. Explicit child pins and folder placements retain
their own location; otherwise children follow their parent's sidebar group.
Search continues to return matching conversations independently.

Children begin with the delegated task and copies of input attachment references,
not the parent's private conversation history. Each has its own computer and
filesystem; pass necessary context and share saved outputs explicitly. Children
are private to the requester because their assignments may contain private
working context. Existing-product callers need an individual sign-in capable of
creating a private conversation.

The parent waits for the original delegated turn, including steering accepted
into that turn. At settlement it receives a saved report that later child
conversations cannot rewrite. Queued follow-ups run independently and are not
exported back into the parent, which may have a different visibility or account
context. A child
needing a human answer, stopped by the user, failed, or deleted produces that
status rather than a successful result. A later child answer/follow-up does not
automatically restart a completed parent. Stop affects the selected conversation;
other independently admitted sessions keep their own controls.

Delegation remains bounded to the root, children and grandchildren. Swarm mode
still uses its fixed roster. Each child uses ordinary turn admission, accounting,
usage controls, connected-account authorization and durable Temporal execution.
The child's private/web presentation is separate from its execution authority.
A sealed snapshot retains the originating visibility, surface and account grants.
Generated work cannot grant human authority, load personal skills or memory
unavailable to its parent, or manage skills/automations as a direct human request.
Human follow-ups use normal admission within that same originating policy context;
they can steer the child and reuse its computer without silently expanding access.
Use a new ordinary conversation for a different account or skill context.
Grandchildren and deferred-answer continuations preserve the same boundary.

Admission, preparation, live authorization, connector credentials, inference,
workspace restoration, computer access and account/skill pickers use that persisted
context. Presentation and transcript ACL checks still use the private child session.
Earlier link records containing only grants recover their context from the
original parent preparation, never its current session settings.

Old SDK-run delegates remain inspectable in their original panels. They are not
silently converted into new conversations, and in-flight recorded workflows keep
their original delegation path through the Temporal version marker.
The workflow carries that decision into its activities; older activity payloads
retain the SDK's shared parent computer setup.

Instructions, available tools, execution and computer startup use one delegation
policy. New ordinary root/child chats can delegate; swarm members and chats at the
depth limit cannot. Even a stale or directly invoked delegate call is refused
before child admission or computer setup. The workflow version is execution state,
not a change to admitted connection permissions or the connector schema cache.
Child admission rechecks guidance inside the transaction holding the parent
session fence. Submission, **Send now**, signal/receipt transitions, Stop and
settlement serialize on that same row. Admitted guidance has a durable ordering
receipt, so promoting an older queued message supersedes newer typed input;
retrying an older delivery receipt cannot reorder guidance. Reporting
already-admitted children remains independent of that fence.
The dispatcher and memory mutation guards use the same admission-order selector.
An already-committed start recovers its original ID/name receipt before applying
new-work eligibility checks; recovery validates the original parent, requester,
product grant and task. It cannot authorize new work or release child content
after Stop or revocation. The activity interceptor leaves this decision to
admission. A separate Temporal marker enables three bounded attempts for this
idempotent command without changing retries for models or external effects.
The input identity travels as an explicit durable-operation argument and is
validated by admission itself, including direct calls and retries. Older pending
activities recover it from the SDK-injected run context; if neither carries an
identity, admission treats the request as the original parent input and still
checks it against newer guidance.

Existing-product deployments require the additive
`2026-10-16-interactive-delegation-v18` migration and native protocol revision 5
through the coordinated maintenance release. Earlier schema receipts are preserved.
Standalone local databases add the new link table at startup.

## Regression coverage

`test_delegation_authority.py` covers shared web, Slack and private parents;
personal skills, memory namespaces, legacy snapshots, grandchildren, direct
follow-ups, computer reuse, account pickers and promoted-message ordering.
`test_delegation_postgres.py` tests real PostgreSQL contention in both directions,
as well as canonical connector selection, continuations and revocation.
The credential suite checks that private child presentation does not expose
personal vault entries unavailable to its parent. Temporal tests exercise
ordinary delegation and steering through the pinned SDK and replay their histories.
Browser tests exercise nested navigation, both entrypoints to side chat, draft
isolation, access loss and responsive layouts. These tests supplement the required
baseline/final real-application recording; they do not replace it.
`test_delegation_retries.py` exercises receipt recovery through the actual activity
interceptor and a lost-reply Temporal retry, including history replay. PostgreSQL
tests also cover a concurrent retry whose first receipt lookup misses. Memory
mutation tests prove that a promoted older input supersedes a newer-created one.
