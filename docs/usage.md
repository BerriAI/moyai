# Using Moyai

[Install Moyai](../README.md#run-locally) · [Documentation](README.md)

## Attachments and the computer

The native interface reuses Moyai's navigation style, home constellation and
interactive sculpture. Start a task directly from the home composer, or continue
an existing conversation beside the Computer, Activity, Plan and Files panel.

Drop files anywhere in the chat column, choose **Attach**, or paste an image
with Command/Ctrl+V. New-session, main-chat and side-chat composers show
removable attachment cards and support file-only messages. A message can contain
eight files, each up to 20 MB; PNG, JPEG, GIF and WebP images are limited to 8 MB
and 20 megapixels each. Zip a folder before attaching it.
Uploads are stored in encrypted objects; admission binds each upload to its
owner's chosen conversation. Images use native Pydantic `BinaryContent`,
including when steering a run. Other originals are available through the shared
`attachment_copy` capability and the SDK's workspace tools, without embedding
large binary documents in model prompts.
Attachment cards and local image thumbnails appear immediately, with an
uploading indicator until the original is saved. Uploads run in bounded parallel
batches; failed cards can be retried or removed, and incomplete files cannot be
sent. The first accepted message appears directly in the chat when leaving the
home screen. Only messages explicitly waiting behind active work appear in the
queue; **Send now** moves saved input into the transcript while admission settles.
Session search is available through Command/Ctrl+K rather than a second sidebar
filter.

Saved-file links in the transcript, subagent results and side chats open the
right-panel preview. Text and Markdown render in the panel; supported images,
videos and audio use its media viewer. **Download original** remains available
inside the preview. Reopening the same file selects its existing tab. External
website links keep their normal behavior.

Sessions open with chat only. **Workspace** opens a compact overlay ordered
**Pull requests → Subagents → Outputs**, aligned to the main chat's right edge.
It slides within the chat column, including when another panel is open, and
respects reduced motion. Selecting a resource opens a right-panel tab. Its **+**
menu adds Side chat, Files, Computer, Activity or Plan.
The initial split is **65% chat / 35% panel**. Drag the divider to save a new
default for this signed-in user and product mode in this browser, across
conversations and reloads. The divider also supports arrow keys, Shift for larger
steps, and Enter to restore 65:35. Smaller viewports clamp or stack the panes
without overwriting that preference.
After a task creates its Modal workspace, add **Computer** and choose **Open
computer**, or use the session menu. Background coding and sandbox creation do
not open the panel automatically.

Opening the computer starts a real X desktop in the same sandbox, with Chromium open and a taskbar.
The panel shows the desktop itself. Click **Take control** on the screen to
click, type, paste text and scroll, using Chromium's own tabs and address bar.
Expand the panel for more space. The small **Release control** overlay appears
on hover or keyboard focus; Ctrl+Shift+Escape also returns control. Leaving or
reloading the page releases this browser's control.

Desktop access follows session visibility and rechecks the workspace's current
account grants. A private session is owner-only; workspace members can view and
take turns controlling a shared session's computer, including its browser sign-ins.
A persistent database lease allows one human controller at a time.
Chat, native coding work, and steering continue while a person controls the
desktop. An idle conversation starts new work normally. Abandoned leases expire. Input is
serialized in the UI and guest; uncertain input is not automatically repeated.
An in-flight action also prevents another tab from releasing control, and
expired input is rejected in the guest before dispatch.
The desktop uses noVNC over an authenticated, origin-checked application WebSocket.
Raw VNC and browser debugging listeners stay on guest loopback. Hidden tabs pause
their display/input transport while retained control leases keep their heartbeat.

Files and browser sign-ins placed in this computer are accessible to
code running there. The human input lease is not a security boundary against
native shell execution. The agent's `browser_open`, `browser_action` and
`browser_read` tools use the same visible Chromium. A human control lease refuses
new automated browser writes while chat and coding continue. Ambiguous browser
writes retain durable effect receipts and are not automatically repeated.
Restrictions on the app's connected-service
tools do not restrict an account signed into manually inside Chromium.

Existing computers install the desktop prerequisites on first opening; new
images include them. This adds basic viewing/control, not every legacy computer
feature. Desktop recording, automatic workspace restoration, and restored
browser-login workflows remain outside this implementation.

The sidebar separates **Participated** (another person's shared session you have
sent a message to) from **Recent** (sessions you created, including linked Slack
history). This personal list applies to administrators too; unrelated shared
sessions remain accessible through search or a direct link. Participated starts collapsed; an explicit
expansion is remembered. Session action menus contain commands for that session;
the dedicated New session controls create a new one. Pins, folders, collapsed groups and unread
state are personal. Folder creation, rename and removal preserve session sharing.
The session menu offers rename, folder placement, copy link, open computer,
read/unread, pin, archive and delete; owner-only actions are enforced by the API.
The header and sidebar use the same menu. Deleted conversations can be restored
from the Deleted list once any active work has stopped. Restore preserves saved
answers and downloads and does not restart cancelled work.

**Workspace** opens an overlay of pull requests, subagents and outputs. Uploaded
sources and saved outputs are also available in the Files tab.
Subagents come from native Harness run lineage, with persistent display names;
only their requester can inspect their answers and recorded steps. Saving a
file with `save_artifact` adds an output, and `attach_link` adds a PR, external
output or source. PR links in the public transcript also appear as mentioned PRs.

PR badges in the sidebar and overview use the same GitHub state. The in-app PR
view shows changes, description, discussion, commits and checks. Public PRs need
no connection; private PRs use eligible native GitHub connections. Shared
conversations never use a viewer's personal connection. Anonymous reads are
cached for five minutes, with explicit Refresh available. Large or omitted
GitHub patches are labelled; this is a read-only viewer.

Queued messages appear above the composer. **Edit** loads their text into that
same composer and keeps attachments; Save/Cancel restore the unsent draft.
**Steer now** promotes the existing message through the native Temporal signal
and Pydantic `AgentRun.enqueue` path. A message already admitted cannot be edited.

## Settings and administration

The account's Settings button opens a dedicated rail with Overview, Skills,
Automations, Memory, Connections and Secrets. Administrators also see Users,
Performance and Spend & usage. These pages use native application records,
not the legacy application's database or APIs.

- **Skills:** bundled instructions stay available; personal and shared saved
  skills load through Pydantic's deferred capability mechanism. Personal skills
  are limited to their owner's private web conversations.
- **Memory:** inspect and edit the Harness memory store in your authorized
  namespaces. Version checks reject conflicting edits. Other users' personal
  memory is not available to administrators.
- **Automations:** interval schedules use Temporal Schedules and admit ordinary
  native turns with deduplicated occurrence receipts. Pause/delete fences new
  admissions; work already admitted follows the normal Stop controls. Creation
  requires a registered Settings worker. Schedules are private and belong to
  their requester.
- **Connections:** manage the same account records selected in the composer.
- **Secrets:** encrypted personal/shared storage with metadata-only reads and
  rotation. Values are never returned to the browser or automatically injected
  into tools or computers. Explicit scoped runtime secret grants remain future
  product work.
- **Users:** manage local accounts, roles and enabled state. Changes invalidate
  prior authentication; the final enabled administrator cannot be disabled or
  demoted.
- **Analytics:** filter and export recorded usage and turn timing. Latency is
  accepted-to-finished wall time, including tools and waits. Native turn records
  do not yet carry authoritative billed cost; missing costs are shown as
  unpriced, never as zero. Provider billing reconciliation remains separate.

## Separate API and workers (standalone installations)

Configure a Temporal service, `TEMPORAL_TASK_QUEUE`, a dedicated PostgreSQL
database, a shared `ENCRYPTION_KEY`, and shared object storage. Use the same
configuration on every API and worker:

```sh
uv run moyai serve --host 127.0.0.1 --port 8796
uv run moyai worker
```

The API owns admission/dispatch and optional Slack delivery. Workers execute the
registered native activities. Use HTTPS at the application edge,
`MOYAI_PUBLIC_URL`, and `MOYAI_SECURE_COOKIES=true`.

Application tables have a `moyai_` prefix. Harness manages its own native tables.
Standalone development can bootstrap its own database. Existing-product mode
uses the canonical product database with an explicitly provisioned native schema,
reuses canonical identities and exposes historical sessions as read-only history.
Old sandboxes and pending workflows are not implicitly converted.

Existing-product startup verifies versioned application and Harness provisioning
receipts. It never bootstraps a replacement user directory. The stock Harness
stores still perform their documented lazy, idempotent DDL on first use; see the
deployment boundary for required permissions and explicit provisioning commands.
Drain or version active Temporal workflows before changing registered activity
names or pinned SDK execution behavior.

## Accounts and integrations

Add accounts in **Connections**, then select them under **Context & connections**
for a message. New conversations select no accounts; continuations restore the
requester's previous selection for review. Read-only is the default;
non-read-only tools require explicit approval by default.

Personal accounts are restricted to private web conversations. Shared web
conversations use explicitly shared accounts. A live computer is bound to its
account context; changing that context requires a new conversation, rather than
inheriting files from an account that is no longer selected.

- GitHub uses a token with the intended repository permissions.
- Linear uses an API key or OAuth access token.
- Notion's hosted tools require an OAuth access token, not an integration token.
- Personal Slack tools require a user token, not a bot token.

Moyai uses the SDK's provider tool catalog and schemas. Token permissions establish
resource access. An optional `allowed_tools` policy narrows the catalog further.
Merge, approving-review and repository deletion tools are excluded. Account
credentials never enter the coding computer, so a shell cannot obtain a
merge-capable token from the application's connection configuration.

Live provider qualification requires configured test accounts. Installing a
capability is not evidence that every remote operation or legacy connector
contract is available.

## Slack bot surface

The bot surface is separate from personal Slack tools. Set
`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID`, and an HTTPS
`MOYAI_PUBLIC_URL`. Register `/hooks/slack` as the event endpoint. Configure the
bot's mentions/messages and ordinary message-posting/history permissions.

Create an application user linked to a verified Slack member ID:

```sh
uv run moyai user teammate --name "Teammate" --slack-user U0123456789
```

The command asks for the application password privately. Unlinked users cannot
start work. Mentions create a thread conversation; replies from its original
linked requester steer or queue work. All Slack-origin conversations have private
web access for that requester. This avoids equating channel membership with
workspace-wide sharing.

Slack memory is isolated per thread and requester; it does not inject personal
web-chat memory into a channel. Connected accounts are disabled for Slack-origin
conversations until destination-specific export policies are implemented. The
web interface identifies that its replies are delivered to Slack.

Signed events are persisted before acknowledgment and deduplicated. Outgoing
answers have persistent send ownership across network waits. A lost send
acknowledgment is checked against Slack metadata; it is never automatically
reposted. A missing receipt remains uncertain, including when a thread read is
truncated or unavailable.

The initial bot adapter sends a bounded answer and a link to the conversation.
Questions and approvals are completed in the authenticated web app. Rich Slack
cards, media uploads, OAuth onboarding and the full legacy Slack command set are
not implemented by this adapter.

## Behavior and recovery

- Repeated client request identities do not admit duplicate turns.
- Queued messages can be edited or cancelled until admission.
- **Send now** uses the native pending-message mechanism. A changed requester,
  model or connection set becomes a follow-up rather than inheriting an active
  run's authority.
- **Stop** revokes unstarted work before requesting Temporal cancellation.
  Failed cancellation RPCs are reconciled. Native foreground-command cancellation
  is exercised by the tests. Already returned background process handles and
  external writes remain real effects; cancellation cannot undo them.
- Model and tool attempts recheck current authorization at the activity boundary.
  An approval cannot restore a revoked account grant.
- Native working-history search and memory are scoped independently of the
  shared product conversation. Private and shared memory namespaces differ.
- A possibly dispatched shell or connected-service write retains an unresolved
  receipt. Another write is blocked even if the model changes the call ID.
  The requester can record inspected evidence using
  `GET /api/sessions/{id}/effects` and `POST /api/effects/{id}/resolve`.
  This is conservative uncertainty handling, not exactly-once external execution.
- The answer is committed before computer snapshot settlement. A snapshot failure
  produces a warning without replacing the received answer.
- Deferred requests also checkpoint their computer before becoming ready for a
  response. A cold continuation restores an eligible saved filesystem image before
  native execution, preserving the conversation, deferred results and effect
  receipts. Missing/stale images and uncertain actions fail closed; there is no
  empty-workspace fallback. See [workspace recovery](pydantic-workspace-recovery.md)
  for authority checks, the 30-day image retention policy and process-state limits.
- Native model-client HTTP retries are disabled. Temporal allows up to three
  model attempts; writes and arbitrary shell dispatch have one automatic attempt.
  Specifically classified reads can have three attempts.

Step, memory and plan stores contain private application data. Protect and back
up the database. Application secrets, saved artifacts and Temporal payloads,
including encoded failure attributes, are encrypted. Large Temporal payloads are
offloaded to immutable private objects. Keep those objects for the lifetime of
the corresponding workflow histories.
