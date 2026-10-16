---
name: coding
description: Implement or fix code in a repository, including setup, tests, and a reviewable change.
---

1. Use the native file and shell tools for repository work in Modal.
2. Inspect the repository's instructions, current branch, and uncommitted work.
   Preserve unrelated changes. Read the relevant code before editing.
3. Establish the behavior to change with a concrete example or failing check.
4. Implement the smallest complete change. Use native subagents for bounded
   research or independent review; give them clear scopes.
5. Run the checks that establish the requested behavior, including the real
   application when integration behavior is involved.
6. Check the diff and have the native reviewer examine meaningful risks.
7. Save the computer checkpoint and hand off the actual result.

If authentication is needed, use the configured native GitHub connection.
Never write account tokens into repository files, URLs, shell history, or output.
Do not replace a missing integration with a mock and call it live verification.
