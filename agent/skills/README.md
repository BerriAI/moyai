# Agent skills

Keep bundled, reusable agent procedures here as `<name>/SKILL.md`, with any supporting files beside them.

The application installs this release-owned coding pack into the existing organization library at startup. Discovery still uses the bounded metadata directory; bodies and supporting references load through `skills_load` and `skills_read_file` only when selected. No model, sandbox, script or network request is involved in installation.

Each `SKILL.md` has YAML frontmatter with `name`, `description`, and `metadata.moyai` containing `icon` and a positive integer `version`. The name matches its directory. Supporting UTF-8 files live beside it and follow the existing skill bundle limits. Increment `version` when changing either instructions, metadata or supporting files.

Admins can edit or archive these skills in Settings. Upgrades replace only unmodified, active templates with a lower source version. Edited/renamed skills, archived skills, name collisions and full libraries are preserved. A downgrade never replaces a newer template. `BUNDLED_SKILLS_ENABLED=false` disables future installation/upgrades; it does not delete existing skills.
