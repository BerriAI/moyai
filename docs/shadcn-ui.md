# Moyai UI components

The React application lives in `native/web`. It imports shared shadcn/Radix
controls from `frontend/components/ui` and semantic tokens from
`frontend/theme.css`: The workspace uses neutral charcoal and white themes, fine
borders, restrained purple actions and semantic status colors. A compact appearance
control in the account footer and Settings switches light/dark mode without replacing
drafts or navigating. The choice persists locally and applies before the first paint;
dark is the default. Message surfaces and composer focus borders stay neutral. General Sans supplies the interface
through Fontshare's official stylesheet; the bundled Geist font is its offline
fallback. Geist Mono is used for code. General Sans is not redistributed in this
public repository; see https://www.fontshare.com/licenses/itf-ffl. CSP allows only
the exact Fontshare stylesheet and font origins in their respective directives.
Use 13px desktop text, 6–8px control corners and a 12px desktop workspace frame;
mobile inputs retain 16px text and the mobile shell uses the full viewport.
The retired escaped-template renderer is no longer used.

Use direct React components for buttons, dialogs, forms, selects and menus.
Preserve labels, focus behavior, portal geometry and permission-driven actions.
`native/web/settings.tsx` and `settings.css` own the Settings shell; product
adapters supply capability-specific forms. `style.css` and the focused panel,
sidebar and workspace styles own application layout.
Desktop sidebars collapse to a 52px rail with an open button at the top;
workspace and Settings share the collapsed state. Mobile navigation stays an overlay.

The folder workspace key uses the shared Button and Radix Dialog behavior, including
Escape, outside dismissal and the Command/Control-period shortcut. Its resource
menu floats within the chat area and must not resize the transcript, move the
composer or change their scroll positions. The independent Computer/resource
side panel still resizes the chat through its existing split.

Images/fonts belong to `native/web/assets`; public files and the sculpture model
belong to `native/web/public`. Analytics rendering and automation normalization
are ordinary modules with type declarations. There are no build-time source
extractors or imports from a legacy static tree.

The home screen places a left-aligned heading and a plain rounded composer over a
faint 24px dot grid. Compact draft starters fill and focus the composer without
sending a message. Once a user
starts drafting, the starters cannot replace their text. Home no longer loads the
Three.js sculpture or runs the headline decode animation. Chat surfaces remain
plain for reading; the full-width shell keeps navigation and independent panels
compact across viewport sizes.

The sidebar defaults to Needs you, In progress, In review and Backlog, derived
from current session/child states and accessible, non-draft open pull requests.
Each conversation family appears once. The grouping control retains the folder
view with its pins, participation, archive and drag/drop behavior; moving a task
between status groups is not an action. The chat composer uses a compact rounded
entry above its attachment/model toolbar. Fenced Markdown code has a language
header, local syntax coloring and plain-text copy; unknown languages remain
readable plain text, and long lines scroll within a focusable region. PR diffs appear above
provider-limit guidance, with repository, author and commit metadata available
through a keyboard-accessible Review details disclosure.

```sh
npm ci
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run test:ui
```

Generated `native/src/moyai/static` assets are ignored by Git. Docker and package
builds create them from source; wheels and source distributions carry the build
without requiring Node.js at runtime. `npm run dev` rebuilds on changes; use `uv run moyai dev` to serve the
application with its configured backend.

Browser tests in `tests/browser/native_*.cjs` serve the real React application
against synthetic APIs. They cover keyboard navigation, access revocation,
historical routes, attachments, responsive panels and settings. Inspect affected
screens in both light and dark modes at 1440, 768 and 320 pixels, including dialogs,
focus, overflow and reduced motion. Fixture screenshots demonstrate presentation, not live provider execution.
See the [repository UI skill](../.agents/skills/moyai-ui/SKILL.md) before changes.
