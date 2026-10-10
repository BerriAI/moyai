# Skills

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Skills

The **Skills** library stores reusable Markdown instructions. Add a name, a
description of when to use the skill, and Markdown text, or import a `SKILL.md`.
You can also ask Moyai in chat to “save this as a personal skill” or, as an admin,
“save this for the organization.” Attach a `SKILL.md` and its supporting text
files; `skills_save` copies the originals by attachment ID into the encrypted
library. It can also write instructions directly, update an existing skill, add
or replace references, and explicitly remove files. Only sent attachments in the
current session through the executing message can be imported. If you have not
specified Personal or Organization, Moyai asks which you want and waits before
saving. A `skills_save` call with no scope returns `scope_required` and writes
nothing. The library's Add skill form also requires a choice instead of
preselecting Personal; editing preserves the existing selection. Keep API keys in
**Secrets**, not in skill text.

- **Personal:** only the owner can view, edit, or use the skill. The current
  message's verified Google identity controls access, not the session creator.
- **Organization:** every signed-in teammate can view and use it. Admins can
  create and maintain shared skills; only the original owner can change its
  sharing. Archiving removes a skill from use and is reversible.

Type `/` in a new-session or follow-up composer to search an inline list of your
personal and organization skills. Use arrow keys and Enter/Tab, or click a skill;
selection inserts its explicit scoped reference without sending or replacing the
rest of your draft. Escape closes the menu and Shift+Enter still adds a new line.
`/skill` opens the same list. The **Skills** button also remains available.

Write `/personal:benchmark-review`, `/org:benchmark-review`, or `/skill benchmark-review`
to invoke a workflow; existing `$personal:benchmark-review` and `$org:benchmark-review`
references still work. Slash references inside code, URLs, and file paths are not
automatically loaded. The first model call also receives a directory of up to
12 authorized skill references, descriptions and revisions, bounded to 6,000
serialized characters. Task keyword matches rank first, then personal skills,
then name; only the current requester's message and acknowledged steering supply
ranking keywords. No model call or embedding lookup is needed to build it.
Moyai can call `skills_load` directly for a relevant entry. Full instructions
enter context only when explicitly invoked or loaded.

The directory reports an omitted count. `skills_search` searches the full
authorized library when the directory is insufficient, returning up to five
matches. Search uses names and descriptions, never instruction bodies. Search
results and loaded skills take priority and are not repeated in the directory.
The full library remains available in the Skills page and composer picker.
Unqualified `/benchmark-review` or `$benchmark-review` prefers a personal skill over the same name in
the organization library. Slack sessions use the same references; personal
access requires a fresh eligible Slack email matching verified Google SSO, not
an accounting-only identity link. Saving from Slack requires that matching user
to have signed in with Google at least once; the saved owner is their Google
identity, and admin status is checked against the current SSO configuration. Subagents have the current requester's skill
access and can load a skill named in their assignment.

Definitions are encrypted at rest. Directory entries, searched descriptions and loaded definitions
are injected privately into inference by the server, rather than returned in
sandbox tool results or copied into workspace files. Search results replace the
previous search selection and remain scoped to the current requester and turn.
Each turn pins the revision it first loads, including across durable resumes;
later turns use the latest revision. Permissions and archive status are checked
again on every model call. There is no per-turn skill-count limit; explicitly
requested and tool-loaded skills remain available for that turn. The model's
context budget still applies to the full request, including skill instructions.
Library limits are 32,000 instruction characters per skill, 50 personal skills per user and 200 shared skills
(including archived entries). Skills cannot bypass tool permissions, provide
credentials, or approve writes. Personal skills do not make shared session
outputs private; generated results keep the session's existing sharing.

Each skill supports up to 20 UTF-8 supporting files, 1 MiB each and 4 MiB total.
`skills_load` returns a file manifest; `skills_read_file` privately loads bounded
excerpts (up to 24,000 characters, with optional text search). Only the four most
recent excerpts stay in inference context. File contents never appear in broker
tool results. References are versioned alongside instructions and retain the
loaded revision through restarts. Editing in the library preserves supporting
files, which can be downloaded from the editor by authorized users. Saving a
skill does not execute scripts or grant additional tool permissions.

Agent saves use a stable `request_id` per turn and an `expected_revision` for
updates. The skill, reference bundle, audit entry and replay result commit in one
transaction; an identical replay returns the original result, while conflicting
edits fail without overwriting newer work. New turns pick up saved changes.
Expected skill-tool rejections (such as unavailable skills, invalid content or
revision conflicts) return an error receipt so the agent can correct or report
the problem and continue. Authentication, server and checkpoint failures still
stop the request.
