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
The child's generated assignment inherits the parent's admitted account policies
and cannot grant human authority, expand skill selections, mutate personal memory
or manage skills/automations as a direct human request. Subsequent authenticated
human messages use normal session admission.

Old SDK-run delegates remain inspectable in their original panels. They are not
silently converted into new conversations, and in-flight recorded workflows keep
their original delegation path through the Temporal version marker.

Existing-product deployments require the additive
`2026-10-16-interactive-delegation-v17` migration and native protocol revision 4
through the coordinated maintenance release. Earlier schema receipts are preserved.
Standalone local databases add the new link table at startup.
