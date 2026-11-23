# History

This module owns historical conversation rendering, file previews, continuation
review and the original session link. The application shell chooses the route
and opens the resulting native conversation.

| Interface | Responsibility |
| --- | --- |
| `product-history.tsx` | `ProductHistory` loads authorized earlier messages, child conversations and files, and presents continuation review. `ProductHistoryPrefix` keeps earlier history unmounted until expanded above a continued chat. |
| `session-reference.ts` | `sessionReferenceHash` preserves server-issued original links. `continuationReference` requires matching source, native UUID, original ID and canonical URL before opening a continuation. |
| `product-history.css` | History layout, file dialogs and responsive presentation. |

The shell imports route helpers directly and lazily imports the renderer.
Do not add a barrel that eagerly loads the history page through the route
helpers. Route types derive from the generated history contract rather than
from a renderer. The command palette uses that contract's source discriminator.

Route logic depends only on its own logic and generated contracts. Rendering
may use the generated client, shared transport, shadcn controls, Markdown and
shared Slack presentation. Shared modules do not import this feature.
`tests/history/test_native_imports.cjs` checks these rules, including nested
sources, type imports and declarations. Route and component tests exercise the
same exported interfaces used by the shell.

Retain the original `#run=` identity, same-origin session-scoped file URLs,
authority refreshes, hidden-tab behavior and collapsed-prefix loading. These
are part of the interface, as are the existing request and lazy-loading costs.
The backend still owns historical formats, authorization and continuation.
