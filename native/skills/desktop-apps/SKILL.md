---
name: desktop-apps
description: Read and operate native desktop apps (not the browser) on the computer's DISPLAY=:99 through the open-computer-use accessibility CLI.
---

`ocu` (open-computer-use) is installed on the computer. It reads native apps
through the Linux accessibility tree and acts on elements by index, so it works
without screenshots. Run it from the shell; every call prints MCP-style JSON.

1. `ocu call list_apps` to find the app name.
2. `ocu call get_app_state --args '{"app":"<name>","include_screenshot":false}'`
   prints the element tree with an index per element, including visible text.
   Add `"text_limit":"max"` for long text such as document bodies.
3. Act with the indexes from the latest state: `click`, `set_value`,
   `type_text`, `press_key`, `scroll`, `drag`, `perform_secondary_action`, e.g.
   `ocu call set_value --args '{"app":"<name>","element_index":"12","value":"7"}'`.
   Indexes are only valid inside one process, so chain dependent steps in one call:
   `ocu call --calls '[{"tool":"get_app_state","args":{...}},{"tool":"click","args":{...}}]'`.
4. Re-read the state after acting and confirm the change before reporting it.

Prefer `set_value` for editable fields and element clicks over coordinates.
Always pass `"include_screenshot":false` unless you will decode the image; the
base64 PNG is large and unreadable as text.
