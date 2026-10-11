# Skills

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Skills

Moyai ships three organization skills: `moyai-dev-and-test`, `pr-feedback-loop`,
and `pr-delivery`. They cover this repository's setup/checks, repairing existing PR
feedback, and evidence-backed PR handoff. The Moyai runbook applies only to
`BerriAI/moyai`; other repositories should have their own verified runbooks.

The release installs these templates at application startup using the existing
encrypted, versioned library. No model call, sandbox startup or network lookup is
needed. Their initial directory fits the shared model budget; instructions and reference
files load on demand as described below. Simply having a skill does not invoke it.

Admins can edit or archive them in Settings. Template upgrades preserve edits,
renames, archives and existing names. Only unmodified, active
templates receive a higher bundled source version; restarting an older release
does not downgrade them. Set `BUNDLED_SKILLS_ENABLED=false` to stop future
installation/upgrades; archive installed entries to remove them from discovery.
These defaults require no schema migration and are independent of skill learning.
See the [initial coding-pack evaluation](coding-skills-evaluation.md) for observed
selection, overhead, limitations, and reproduction commands.

The **Skills** library stores reusable Markdown instructions. Add a name, a
description of when to use the skill, and Markdown text, or import a `SKILL.md`.
You can also ask Moyai in chat to “save this as a personal skill” or, as an admin,
“save this for the organization.” Attach a `SKILL.md` and its supporting text
files; `skills_save` copies the originals by attachment ID into the encrypted
library. It can also write instructions directly, update an existing skill, add
or replace references, and explicitly remove files. Only sent attachments in the
current session through the executing message can be imported. New skills default
to Personal in chat and in the Add skill form. Organization publishing requires
an explicit request and an admin; editing preserves the existing selection. Keep API keys in
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
automatically loaded. The first model call also receives an authorized directory,
fitted to the selected model's input window. Task keyword matches rank first, then personal skills,
then name; only the current requester's message and acknowledged steering supply
ranking keywords. No model call or embedding lookup is needed to build it.
Moyai can call `skills_load` directly for a relevant entry. Full instructions
enter context only when explicitly invoked or loaded.

The directory reports an omitted count. `skills_search` searches the full
authorized library when the directory is insufficient. An empty query lists every
authorized entry; follow `next` with `cursor` until it is null. `page_size` controls
transport, with no total result cap. Catalog changes reject a stale cursor so a
caller can restart enumeration. Search uses names and descriptions, never instruction bodies. Search
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
There are no fixed skill-count or instruction-length quotas. An omitted body is
explicitly marked and remains readable by range. Skills cannot bypass tool permissions, provide
credentials, or approve writes. Personal skills do not make shared session
outputs private; generated results keep the session's existing sharing.

Skills support complete UTF-8 supporting files, subject to ordinary transport and
storage availability. `skills_load` returns a file manifest; `skills_read_file`
privately reads `SKILL.md` or supporting-file ranges using `offset` and `limit`
(16,000 characters by default). Follow `next_offset` to reconstruct a file.
Every read receipt is retained; the inference view prioritizes recent requested
ranges and exposes omissions. File contents never appear in broker
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

Public procedures shipped with the agent are listed in the initial context with
content revisions. `bundled_skill_read` reads their full instructions or ranges
on chat, automation, and delegated surfaces. They are independent of a user's
private library. The shared `repository_guidance` tool resolves workspace
`AGENTS.md` files from root to the directory being edited, with scopes, hashes,
and complete range reads. Native duplicate guidance loading is disabled.

The editable coding templates are stored in `agent/skill_templates/` and installed
by `app/skill_templates.py`. Public build-pinned procedures in `agent/skills/`
keep their separate `bundled_skill_read` path; template edits, archives and disabled
installation never expose a duplicate template through that public catalog.
