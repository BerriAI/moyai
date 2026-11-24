---
name: pr-delivery
description: Deliver implemented code as a regular pull request with verification and working-demo evidence.
---

Create a regular ready-for-review PR unless the requester specifically asks for
a draft. Before publishing, inspect the final diff, run appropriate checks, and
verify the changed behavior in the real application. Include reproducible steps,
a useful screenshot, and a short real recording. For the shared desktop, start
`desktop_action(command={"action":"record","seconds":45})`, perform the real
workflow with native browser/desktop tools, then request
`desktop_action(command={"action":"stop_recording","recording":"<returned identity>"})`.
Read `desktop_read(recording="<identity>")` until it reports `ready`, and export
that exact path using `save_artifact`. Start acknowledgement is not a completed
recording. Capture only task-relevant test data; finish any human sign-in before
starting. Captures have no audio and stop automatically within 120 seconds.
If status is failed/interrupted, report missing evidence; do not present an empty
file, screenshot slideshow, or reconstructed output as a live recording.

Lead the PR description with the user-visible problem and resulting behavior.
Include relevant validation and material limitations. Return a prominent PR link.
Do not merge the PR or send Slack announcements without explicit authorization.
The application delivers replies back to their originating conversation.
