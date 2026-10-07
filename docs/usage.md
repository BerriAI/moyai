# Using Moyai

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Chat interface

The browser opens into a conversation workspace with searchable sessions in the sidebar, a centered new-session composer, and a full-height chat with the composer fixed below the conversation. Enter sends; Shift + Enter adds a line. Unsent follow-up drafts remain with their session while switching chats. Activity opens the session's progress, Slack source context, and file download in a collapsible panel. Connected-app tools execute without an approval prompt. Historical approval records remain readable.

The sidebar lists the 100 most recently updated parent sessions first, using the same last-updated time shown beneath each title. New messages and session state changes update this time; simply opening a session does not. Subagents stay nested under their parent in assignment order. The list refreshes every 15 seconds, and a selected older session remains accessible beyond the list limit.

**My sessions** includes sessions you created or sent a message in, including linked Slack activity and participation in a child agent chat. Only admins can select **All sessions**. Members always receive their personal list, even when the API scope is omitted; an explicit `scope=all` request requires an admin role. Saved admin filters are reset when a member signs in or the browser refreshes their role. This controls session discovery in the sidebar; existing authenticated shared links still let teammates open a session and join the conversation.

Use **+** beside Sessions to create a named folder. Drag a session onto a folder to move it, or onto **Recent · not in a folder** to unfile it. The destination highlights while dragging, and a collapsed folder opens after the move. A session's **⋯** menu also moves it into a folder or back to **No folder** (including on touch screens or with a keyboard); the folder's **⋯** menu renames or removes the folder. Removing a folder keeps its conversations. Folders are personal to the signed-in identity (shared password sign-ins share an identity), and do not change who can access a session. Child agents stay with their parent. Folder names and membership are saved in the database and checkpoints; filed sessions remain in the sidebar beyond the recent-session limit. Search matches folders, sessions, and child agents. Collapsed folders are remembered in the current browser.

**Settings** is the sidebar's single configuration entry. It groups Skills, Memory, Connections, Secrets, and Runtime, plus admin-only Users, Spend, and Environments. Available automation features appear under Workflows. Each section links back to Settings in the header; existing direct links such as `#skills` still work.

Assistant replies render Markdown headings, lists, links, tables, and copyable code blocks. The pinned local Marked and DOMPurify libraries are listed with their source tarballs and hashes in `app/static/vendor/versions.json`; licenses ship alongside them. Raw HTML is escaped, the resulting markup is sanitized, and only HTTP(S)/mailto links are enabled. No external script or image is loaded to format replies.

Browser acceptance covered desktop and a 390px narrow viewport, session search, multiline input, Enter-to-send, queued follow-ups, per-session drafts, live status, activity controls, and hostile HTML/URL rendering. The 49 existing automated tests pass.

## Choose the model

**Live verification:** session `9d137408acbc4254a1c6fbbf7e86aa77` ran **Opus → Astra → Opus**. Opus remembered `copper lighthouse` and wrote `21`; Astra restored the conversation/file, incremented it to `22`, and a queued Opus turn read `22` and recalled the phrase. The picker changed to Opus while the active turn stayed on Astra. All three answers have model labels, connected apps were deselected, and all three sandboxes confirmed termination. The `@Moyai model opus` command was also verified in the existing #bot-spam test thread without starting compute.

Use the model picker in the new-session composer or below an existing conversation to choose **GPT-6 Astra** (`openai/gpt-6-astra`), **Claude Opus 5.5** (`anthropic/claude-opus-5-5`), **Claude Sonnet 5.5** (`anthropic/claude-sonnet-5-5`), or **GLM-5.3** (`fireworks_ai/glm-5p3`). The choice applies when you send the next message and becomes that session's preference. Your conversation and saved workspace stay together across a model switch. Each new assistant answer records its model; old answers without a stored model are left unlabeled.

You can also ask in ordinary language in Slack or on the web: **“switch to GLM 5.3”** or **“use GLM 5.3 and summarize this thread.”** The agent discovers `model_list` and `model_switch`, validates the enabled choice with the broker, and switches before continuing the remaining task. The next inference uses that model with the same conversation and workspace; completed work is not restarted. The currently selected model handles the initial request to switch, so it must be reachable. If it is unavailable, use the picker or the explicit Slack command below to switch without inference. A successful switch confirms routing selection, not provider availability; gateway credentials still need access to the target model.

Every queued message captures its model at submission. Changing the picker or Slack preference does not reroute a response already running or queued. The agent's explicit `model_switch` tool changes the active turn starting with its **next** inference and becomes the default for future messages, unless a newer queued message already set a preference. An in-flight inference and queued messages keep their assigned models. Switches are scoped to a direct active user chat, recorded durably, and idempotent on retries; delayed retries cannot undo later switches. The broker ignores raw model overrides supplied by sandbox code. User-message labels preserve their original selection; assistant answers record the final selected model, with per-inference spend/traces retaining each actual routed model. `AGENT_MODEL` sets the default; a custom gateway default remains selectable alongside the catalog.

The picker and validation share `MODEL_CATALOG` in `app/config.py`. To add a model, add its gateway ID and display name there, optionally add a shorthand in `resolve_model()`, and deploy the code. Render does not need a separate model list. The legacy `AGENT_MODELS` environment variable is ignored and should be removed from Render and local `.env` files; it can no longer hide models added by a release.

Local routing demo: run `uv run python scripts/model_tools_demo.py` and open `http://127.0.0.1:8795/demo`. It exercises the real broker and a local HTTP provider stub, including the next inference and retry/queue behavior. Tool selection is scripted; it does not test live model intent recognition or production provider access.

In Slack, mention the bot with `model opus`, `model sonnet`, `model astra`, or `model glm-5.3` to set the model for that thread's next messages. You can start with a model directive on the first line and the task on the next line, for example:

```text
@Moyai model opus
Read this thread and suggest the next step.
```

A model-only command starts or updates the saved session without launching a sandbox. Its confirmation does not change running or already queued turns. These commands also work in DMs. Include the bot mention on channel follow-ups if the installation has not yet enabled ordinary thread events.

## Session workspace panel

Cloud sessions have a collapsible right-side workspace. Open **Computer** or
**Files**, or use **+** to add a computer, file browser, activity view, or side
chat. Each selected file gets its own preview tab. Tabs can be closed, resized,
expanded, and reopened; their layout and side-chat drafts are saved per user and
session in that browser. Hiding or leaving the Computer tab releases human
control and stops preview polling. Closing a tab does not stop its agent.

Side chats are independent durable sessions. They start with a bounded snapshot
of the original conversation and use a separate workspace, message queue, and
billing attribution for the person who sends the message. They do not steer the
main agent, share its live browser/files, or mirror into its Slack thread. They
remain available in the add-tab menu and session list after closing the tab.
Credential requests can be completed through **Open session**.
