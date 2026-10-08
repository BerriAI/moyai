# Skill icons

Moyai uses a curated local outline-icon palette, not remote images or arbitrary SVG.

- Skills default to `icon: "auto"`. A bounded keyword match on the skill **name** suggests an icon; unknown names use a cube. Descriptions do not affect it.
- Skills → Add/Edit → Icon selects one of 12 explicit icons or restores Automatic. Overrides are stored on the skill, so all authorized viewers see the same artwork.
- Library rows, slash suggestions, the skill dialog, and new-session/follow-up chips share one renderer. The built-in `/goal` suggestion uses a target; a user skill named `goal` remains independently configurable.
- Exact scoped invocation text, authorization, and instructions do not change. Old clients and agent saves which omit `icon` preserve existing overrides. Existing databases migrate to Automatic.
- Imported `agents/openai.yaml` image assets are **not** supported by this change. No image upload, external URL, or untrusted SVG is rendered.

## Codex reference

Checked OpenAI's official [Build skills documentation](https://learn.chatgpt.com/docs/build-skills) (redirected from `https://developers.openai.com/codex/skills`). Its optional `agents/openai.yaml` interface supports `icon_small`, `icon_large`, and `brand_color`. This documents per-skill assets, not a fixed ten-category palette. The supplied screenshots show generic cube artwork for ordinary skills and a target for the built-in goal command.

Moyai's palette is a deliberate lightweight adaptation, not a claim to reproduce Codex's private implementation.

## Verification

- `npx --yes --package=node@22 node --test tests/*.cjs`
- `uv run pytest -q tests/test_skill_icons.py tests/test_skills.py tests/test_skill_saving.py`
- Start `TMPDIR=/workspace uv run python tests/browser_account_composer.py --serve`, then run `uv run --with playwright python tests/browser_skill_chips.py` and `uv run --with playwright python tests/browser_skill_icons.py` against its synthetic signed local fixture. The icon-creation browser scenario expects a fresh fixture.
