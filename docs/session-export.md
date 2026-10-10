# Session JSON downloads

Open a conversation's **Session actions** menu (or its sidebar menu) and select
**Download session JSON**. Attach that file to a conversation for debugging.

The versioned `moyai.session` JSON includes session metadata, messages, attachment
metadata, saved activity events, and locally retained model/tool/runtime spans.
Spans contain trace, parent, span and turn IDs, nanosecond timestamps as strings,
status and redacted attributes with model responses and tool inputs/results.
Public progress explanations are included; hidden reasoning, system prompts and
binary attachments are excluded. Existing private-tool preferences and trace
content limits still apply. Treat the export as conversation data when sharing.

Exports are authenticated, uncached snapshots of the selected session. Child
agents and side chats have their own exports. Activity history and trace content
may already have been bounded when recorded. Older model traces cannot be
reconstructed; local retention begins with this change, even without an external
tracing destination. Retention adds database storage for every new span.

This change requires schema revision 2 for PostgreSQL verify-mode installations;
apply the existing offline schema migration procedure before starting the new
runtime. SQLite initializes the new table automatically.

Verification: `uv run pytest tests/test_session_export.py`; start
`scripts/session_ui_demo.py --port 8830`, then run
`node --test tests/browser/session_export.cjs` for real browser downloads using
local fixture data at desktop, tablet and mobile widths.
