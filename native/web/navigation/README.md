# Conversation navigation

The sidebar, command palette, session actions, title editor and Slack sharing
dialog live together. Their tree, drag and resizing helpers retain the existing
session-list and pending-update interfaces from `conversations`.

`main.tsx` composes navigation and owns routing. Navigation may use shared
transport, generated contracts, debug export, review labels and the Slack logo;
it cannot import application composition, Settings or workspace renderers.
Import individual files instead of adding an eager package barrel.

`tests/navigation` exercises tree and drag behavior. The import rules are in
`tests/test_native_feature_imports.cjs`; browser coverage retains navigation,
permissions, keyboard, resizing and dialog scenarios.
