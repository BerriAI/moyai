# Personal memory in hosted Moyai

With memory enabled, individual users can recall saved notes in both private
and shared chats. Direct private chats can save supported preferences, corrections,
project decisions and observations through the normal memory tools. Background
review can capture omissions from newly completed private user messages.

Notes learned in a private chat are used only in that owner's private chats,
including later chats with different authorized personal connections. Shared
chats, Slack threads and shared background reviews cannot list, search, read,
overwrite or delete those notes. Account authorization, repository relevance,
source verification and memory preferences still apply on every read.

The server records the source visibility inside each encrypted note. Existing
notes without this marker retain their original recall behavior. Updating a note
from a private chat makes the updated note private. Editing it in Settings
preserves that restriction; agent tools cannot broaden it. Settings labels these
notes **Private chats only**. Manual notes created in Settings retain the
existing across-chat behavior.

Pause memory to disable recall and automatic capture. Select **Save manually**
to keep recall while disabling agent writes and background capture. Deletion and
manual edits retain revision checks and barriers against stale background work.
Automated and delegated runs still cannot mutate personal memory.

Previously completed private chats are not included in background backfill.
Changing a conversation's visibility invalidates an in-flight memory review.
The retained legacy runtime excludes private notes from its shared-chat memory
tools and background review.

Private calendar schedules can recall these notes under their owner's original
private execution authority. Scheduled turns and their resumptions cannot write
personal memory. See [private schedules](pydantic-automations.md#private-schedules)
for audience, account-selection and deployment requirements.

## Deployment

Native protocol revision 7 introduced private-note filtering; revision 8 also
binds private scheduled turns to their original visibility. Use the reviewed
contract-adoption process and upgrade the API, worker, broker and coordinator
together. The encrypted note format is additive and existing unmarked notes do
not change. Private scheduling separately requires schema v24.

Do not downgrade to an older memory reader with private notes present: that
reader does not enforce their visibility. A reviewed rollback must first keep
memory disabled or remove those notes while the current filtering is active.
