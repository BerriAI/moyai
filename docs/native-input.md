# Native Linux input

Moyai's native desktop tools use the pinned `open-computer-use` 1.1.2 runtime.
Its Linux implementation can append to the first editable field instead of
editing the focused field, invoke an entry's `activate` action when clicked,
and send scrolling keys to whichever app currently has keyboard focus.

`native/ocu_guest.py` adapts that runtime inside its private system-Python
launcher. The adapter keeps OCU's app identities, element indexes, screenshots,
and MCP protocol. The workspace's Python and Node lookup paths are unchanged.
The existing guest content hash delivers this adapter when helper files are
updated; it does not require reinstalling OCU.

## Editing contract

- `type_text` requires one focused, editable AT-SPI text control in the requested
  window. It inserts at the caret or replaces the selection, supports Unicode
  and multiline text, verifies the resulting text, and moves the caret after
  the inserted text. It never selects an arbitrary field or falls back to
  global keyboard typing.
- A single left element click on an editable text control focuses it without
  invoking `activate`. Explicit secondary actions still invoke the requested
  action.
- `press_key` requires a focused control and an active requested window before
  using AT-SPI key synthesis. Modifiers are released even if synthesis fails.
- `scroll` changes the requested element's accessible scrollbar in the selected
  direction. It does not send PageDown to an unrelated focused control.
- Observations include focused field identity, caret position, and selection
  ranges. A change invalidates the previous observation even when the visible
  text is unchanged.

Input checks that refuse before dispatch return `action_executed=false` with a
fresh observation. Once an edit or key may have been sent, an error remains an
uncertain outcome: inspect the app and do not automatically retry. In particular,
selection deletion and insertion are separate AT-SPI operations; a failed
insertion can leave the selection deleted.

These semantics require accessibility support from the application. Custom
canvas controls and terminals without `EditableText` are refused. Masked or
filtering controls may not allow exact readback and cannot be reported as a
verified text edit. Scroll targets must expose exactly one accessible scrollbar
for the requested direction. Key dispatch checks focus immediately before
synthesis, but Linux does not make that check and a global key event atomic.
Browser DOM tools remain available for browser-specific interactions.

## Verification

Focused regression tests:

```sh
cd native
uv run pytest -q tests/test_ocu_guest.py tests/test_ocu_linux_input.py
```

The existing desktop CI smoke test runs the actual Linux guest, OCU, GTK, and
Chromium stack. `evals/desktop/input_app.py` provides independent saved state for
caret insertion, selection replacement, Unicode, multiline input, read-only
refusal, click-without-activation, and scoped scrolling.

```sh
docker build -t moyai-desktop-test evals/tasks/desktop_license/environment
docker run --rm --network none -v "$PWD:/repo:ro" \
  moyai-desktop-test python /repo/evals/desktop/smoke.py
```

Keep this test green when upgrading OCU: the adapter relies on the pinned Linux
runtime's internal operation and snapshot hooks. The smoke test supplements
before/after tests through a running Moyai application with a configured model.
