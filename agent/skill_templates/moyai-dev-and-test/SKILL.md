---
name: moyai-dev-and-test
description: Set up, run or test BerriAI/moyai code changes. Use for Moyai development and regression verification; other repositories need their own runbook.
metadata:
  moyai:
    icon: code
    version: 1
---

# Develop and verify Moyai

Applies to https://github.com/BerriAI/moyai. Check the selected repository and
current branch before using this runbook; a request made *through* Moyai is not
necessarily a request to modify Moyai.

Read the checkout's applicable `AGENTS.md` and current CI workflow for the affected
area. This skill records known commands, but the checked-out revision determines
what exists and is required. Do not fetch every reference for a small edit.

- For dependency setup or a local app, use `skills_read_file` to read
  `references/local-development.md` from this loaded skill.
- For choosing checks, use `skills_read_file` to read `references/verification.md`.

Choose checks that reach the changed behavior. Report the command, observed result,
and any required check that was unavailable. A mocked provider proves the local
contract; it does not establish that a live model, sandbox or connected app worked.
After a correction, rerun the affected checks. Broaden verification when a shared
boundary changed, rather than repeating unrelated suites after every small edit.

These references stay in the skill library; their paths are not workspace files.
Use Moyai's normal GitHub and execution tools and their current schemas. Repository
access, publication and external-service permissions remain those of the task.
