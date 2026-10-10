---
name: pr-delivery
description: Prepare a coding PR handoff with verified behavior, a real demo, screenshot or recording, and a concise PR description. Use when delivering an implemented code change as a PR.
metadata:
  moyai:
    icon: code
    version: 1
---

# Deliver a reviewable change

Use this when the task includes implementing a change and delivering a PR. Respect
local-only work and requests to skip captures. Review-only or explanatory requests
do not become implementation/publication tasks because this skill is available.

Choose the smallest real demonstration that shows the requested outcome. Use the
repository's actual setup and verification instructions; retain the relevant input,
steps and observed output. For UI work, exercise the changed flow in the running
app. For backend/CLI work, use a real request or command and its meaningful result.
Describe fixture data and mocked external systems accurately.

Read `references/demo-and-handoff.md` with `skills_read_file` when capturing or
packaging the demonstration. Reuse working evidence from this task when it still
matches the final code; do not rerun an unrelated test stack just for the handoff.

Write the PR around the final change: what was broken or missing, what the user now
experiences, what was verified, and any remaining limitation. Use the repository's
PR template. Remove abandoned approaches and stale validation claims. Follow
Moyai's existing PR creation/update tools; a local commit is not a published PR.

Finish with the confirmed PR URL prominently, a short explanation of the observed
behavior, the relevant demo/capture links, and an accurate verification statement.
Distinguish local checks, CI on the current commit and human review. Pending CI is
pending. Do not claim deployment or merge as part of PR delivery.
