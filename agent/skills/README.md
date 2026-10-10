# Bundled procedures

Each `<name>/SKILL.md` contains a reusable public procedure and a short discovery
description. Supporting UTF-8 files live beside it. The shared agent prompt carries
a complete catalog; `bundled_skill_read` loads exact content revisions and supports
file ranges. All harnesses, automations and evals use this package. Private saved
skills retain their separate authorized broker path.

The revision covers every file in a bundle. A changed build cannot silently serve
new instructions under an older revision. Directory-scoped `AGENTS.md` guidance
uses the shared `repository_guidance` resolver; it is not a globally loaded skill.
