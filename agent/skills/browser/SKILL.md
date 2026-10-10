---
name: browser
description: Use real browser interaction, inspect visual behavior and verify web or UI outcomes with evidence.
---

Discover the browser primitives available to this runtime. Read the current page/frame state before acting; use stable targets and refresh stale references. Follow extraction cursors/ranges or inspect another page region when a view is incomplete. Use screenshots for canvas, images or ambiguous layout. DOM text alone cannot verify visual behavior.

Use ordinary navigation, tabs, frames, keyboard, scrolling, upload/download and coordinate interaction as the task requires. Preserve the user's intended destination and actual state. A mockup, local screenshot or successful HTTP request is not proof of a deployed outcome. If a person takes browser control, continue independent work or wait rather than repeatedly competing for control.

For changed UI behavior, exercise the actual flow and its relevant failure/retry states. Inspect narrow layouts and keyboard access when affected. Use `browser_record_start` before a meaningful flow and `browser_record_stop` after it; save a useful named PNG with `browser_screenshot`. These capture the sandbox browser, not the user's computer. Never capture passwords or secrets, manufacture a successful demo, or deploy publicly solely to make a recording.

Load the artifact procedure before delivering captures or downloads. Return the exact saved paths/revisions, distinguish synthetic fixtures from provider-backed verification, and state what could not be exercised.
