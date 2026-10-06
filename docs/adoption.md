# Adoption dashboard

Organization administrators can open **Settings → Adoption** (`#adoption`).
The page queries `GET /api/admin/adoption?start=YYYY-MM-DD&end=YYYY-MM-DD`.
It defaults to the last 30 UTC calendar days, supports up to 93 days, and rejects future dates.

- Daily human chat submissions, with a seven-day moving average.
- Distinct identified teammates in the selected period and daily breakdown.
- Last seven complete days versus the preceding seven, ending at the selected end date (or yesterday when today is selected). A zero baseline is labeled rather than divided by zero.
- Date filters, refresh, an accessible chart, and an exact-value table.

Counts derive from retained `messages`, joined to sessions, identity links and automation history. No telemetry provider or new event capture is required, and existing chat history works immediately after deployment.

A request is one stored `role=user` message in a top-level cloud session. Initial prompts, follow-ups, side chats, and steering messages count. Existing submission idempotency prevents duplicate retries. Queued, failed, cancelled and soft-deleted submissions still count: this measures demand, not completed work. Demo sessions, delegated-agent sessions and automation launch prompts do not count. Human follow-ups to an automation do count. Model/tool calls do not inflate adoption.

Linked Slack/Google identities count once; shared/anonymous submissions count toward requests but not identified teammates. The endpoint returns only aggregate data, not prompts, emails, names, or session identifiers, and requires administrator access.

Days without stored submissions are zero-filled. Missing or purged history and older non-chat tasks cannot be reconstructed; this is not a guarantee of complete historical coverage. Today is marked partial and excluded from weekly comparisons. Trends do not establish causation for product changes.

## Verification

```sh
uv run pytest tests/test_adoption.py tests/test_spend.py tests/test_sessions.py tests/test_message_queue.py -q
node --test tests/test_adoption.cjs tests/test_settings.cjs
git diff --check
```

Browser verification uses the actual app and database with clearly labeled synthetic test fixtures, not production adoption figures. No production deployment is performed by these changes.
