---
name: coding
description: Implement or fix code in a repository, including setup, tests, and a reviewable change.
---

1. Use the native file and shell tools for repository work in Modal.
2. Inspect the repository's instructions, current branch, and uncommitted work.
   Preserve unrelated changes. Read the relevant code before editing.
3. Establish the behavior to change with a concrete example or failing check.
   Read the runtime versions and lockfiles before installing dependencies. Run
   one installer at a time for each shared dependency directory and wait for a
   successful exit before starting previews, browser checks, or dependent builds.
   Check build hooks too: a Python package install may already run `npm ci`.
   Use the same selected Node executable and PATH for installation and npm
   build/dev/preview scripts. Do not switch only the launch command with
   `npx node@...`. After changing Node or restoring dependencies installed under
   another version, reinstall from the repository's lockfile. Engine errors
   mean the runtime needs attention; do not suppress them to start a preview.
   For background previews, check the process output and a successful local
   HTTP response before opening the URL in the browser.
4. Implement the smallest complete change. Use native subagents for bounded
   research or independent review; give them clear scopes.
5. Run the checks that establish the requested behavior, including the real
   application when integration behavior is involved.
6. Check the diff and have the native reviewer examine meaningful risks.
7. Save the computer checkpoint and hand off the actual result.

If authentication is needed, use the configured native GitHub connection.
Never write account tokens into repository files, URLs, shell history, or output.
Do not replace a missing integration with a mock and call it live verification.

Discover `github_checkout` and pass the requested repository name or ID; the
server selects its authorized connection. Discover that connection's publication
tools. Preserve the requested repository, source ref, PR base and head. Use `publish_branch` for
branch-only delivery and `open_pull_request` for an existing head; opening a PR
does not grant ownership of that branch. New PRs are ready for review.
For checkout changes, stage only the intended files and pass `directory` to
`publish_branch`; it exports the staged tree relative to `base_sha` on the worker.
This includes committed changes since that base. Inspect the staged diff first.
Use a stable request key only for identical intent. After an uncertain write,
inspect `publication_status` with the original unprefixed operation name and
request key, including `update_pull_request` for follow-ups. Never automatically
repeat an incomplete operation with a new key.
Rebase publishes one prepared commit on the current base and requires the exact
expected PR head. Never approve, merge, enable auto-merge, or clear queue blockers.
