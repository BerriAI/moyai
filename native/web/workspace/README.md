# Workspace panels

This module owns the resource menu and side-panel UI: panel tabs and resizing,
files and outputs, pull-request reviews, subagents, swarms, activity details and
the lazy computer panel. `workspace-types.ts` is its UI-only reference contract.

Callers import the specific renderer, hook or reducer they need. The application
continues to import `computer.tsx` lazily; importing the file reference types
does not load the computer client. `file-links.ts` shares this type contract
and is retained next to transport because both messages and panels consume it.

The module uses generated API contracts, shared controls and transport, the
conversation list's row type and the message code renderer. It never imports
application composition or Settings. `tests/test_native_feature_imports.cjs`
enforces these dependencies. `tests/workspace` exercises the existing panel
state and desktop input interfaces; the browser suite covers their rendering.
