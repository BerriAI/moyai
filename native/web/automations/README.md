# Automations

This module owns the saved product automation editor, review and history UI,
along with its validation, normalization and presentation helpers.

| Interface | Responsibility |
| --- | --- |
| `product-automations.tsx` | `ProductAutomationsPage`, consumed by Settings; editor and review renderers exercised by module tests |
| `product-automation-data.ts` | Generated-contract-derived editor types, validation, action eligibility, event edits, request keys and UTC date conversion |
| `automation-helpers.js` / `.d.ts` | Calendar summaries, trigger defaults, provider labels and history merging, with matching declarations |
| `product-automations.css` | Automation-specific styles; imported by the page |

Settings owns navigation and its standalone automation form. It imports the
product page and the UTC conversion helpers directly. The Settings lazy import
in `main.tsx` still determines when this UI loads; there is no barrel or extra
runtime layer.

The generated client owns request and response contracts. `api.ts` and
`api-client.ts` own transport and authentication. Temporal and the backend own
scheduling, synchronization, capabilities and execution. Moving the frontend
module does not change those responsibilities or the existing polling,
idempotency and stale-response behavior.

`tests/automations` exercises the module's existing interfaces. Import checks
scan TypeScript, declarations and JavaScript: pure automation logic may depend
on its own logic and generated contracts; rendering may also use React, icons,
shared controls and transport. Neither layer imports the Settings shell or
other features, and shared transport and controls cannot import automations.
