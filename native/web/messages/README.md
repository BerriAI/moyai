# Messages and input

This module owns message presentation and input: Markdown/code/images, Slack
and skill references, the skill composer, attachments, goal controls, progress,
questions, browser-login requests, credential requests and failure presentation.

Conversation reconciliation and polling remain in `conversations`. Application
composition stays in `main.tsx` and `home.tsx`. The shared upload and file helpers
stay beside transport; using conversation state does not load a renderer.
Callers import individual files, so the lightweight skill/Slack renderers do
not eagerly load the Lexical composer.

The existing progress renderer consumes `workspace.tsx`'s avatar and workspace
reference types. Credential requests reuse `settings/settings.css` without
loading Settings JavaScript. These specific dependencies are documented and
checked in `tests/test_native_feature_imports.cjs`.

`tests/messages` exercises composer feedback and skill mention interfaces.
Rendering, focus, uploads, permissions and streaming remain covered by the
browser suite.
