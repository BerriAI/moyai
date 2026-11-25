# Google Calendar read adapter

`app/google_calendar_tools.py` supplies a read-only provider adapter for the
`google_calendar` connection kind. OAuth, credential refresh, account selection,
connection permissions, Dot enrollment and scheduling belong to the host. This
module does not install an account, schedule checks, or mutate calendars.

The integration exports `TOOLS`, `READS` and
`async call(name: str, arguments: dict, access_token: str) -> dict`.
Each `TOOLS` entry follows the existing `(provider, write, Args, description)`
convention. Moyai authorizes the selected personal connection before dispatch
and rechecks its authority before consuming the result.

| Tool | Read |
| --- | --- |
| `google_calendar_calendars` | One page of the user's calendar list; 25 entries by default, at most 100. Hidden and deleted entries are opt-in. |
| `google_calendar_events` | One page of expanded event instances overlapping an explicit window of at most 31 days; 25 entries by default, at most 100. Defaults to `calendar_id="primary"`. |
| `google_calendar_event` | One exact event, recurrence master or instance by its provider `event_id`, with `calendar_id="primary"` by default. |

## Dependencies and scopes

The shared product dependency set includes:
`google-api-python-client` (2.x), `google-auth` (2.x),
`google-auth-httplib2` and `httplib2`. The generated API client uses its bundled
Calendar v3 discovery document. It builds a fresh transport in `asyncio.to_thread`
for each call, uses a 20-second socket timeout, closes it afterward, and disables
HTTP redirects. It requests only fixed Calendar API methods; model arguments
cannot choose endpoints, HTTP methods, headers or credentials. The SDK encodes
opaque calendar and event IDs.

Request only these Calendar grants:

- `https://www.googleapis.com/auth/calendar.calendarlist.readonly` for listing calendars.
- `https://www.googleapis.com/auth/calendar.events.readonly` for listing/getting events.

The adapter accepts the host's current access token. SDK request retries and
credential refresh attempts are disabled. No refresh token enters this module;
the host owns refresh, retry policy and its existing workflow infrastructure.
The socket timeout is not a hard wall-clock cancellation guarantee for a worker
thread. HTTP errors expose controlled status/retry metadata, never raw response
bodies, credential values or request URLs.

## Time, recurrence and cancellation semantics

Supply `time_min` and `time_max` as whole-second RFC3339 strings with `Z` or a
numeric timezone offset. The adapter rejects fractional seconds because Google
ignores them, and rejects naive timestamps. Durations are compared as instants,
including across daylight saving transitions. An optional `time_zone` is an
IANA response timezone; omitting it preserves the provider's calendar timezone.

The window selects **event end > time_min and event start < time_max**; an event
can begin before the window and overlap it. Google expands recurring events with
`singleEvents=true` and orders instances by start time. The adapter preserves
`recurringEventId` and `originalStartTime`, including moved instances. Reading a
master through `google_calendar_event` retains its recurrence rules without
locally expanding them.

All-day `start.date` and `end.date` remain dates, with an exclusive end date.
They are not converted into midnight UTC. Timed events preserve the provider's
`dateTime` offsets and `timeZone`; absent timing data stays absent. `all_day` is
null when there is insufficient data to classify the event.

`include_cancelled=true` is the default for event lists. With expanded instances,
Google does not return deleted recurrence masters in that list. A cancelled
instance can lack title/timing/revision data, and a deleted event can expose only
its ID. These records remain sparse. A windowed list is **not a deletion feed**:
absence, permission changes, a 404 or a 410 must not resolve a tracked commitment
as deleted. This adapter deliberately does not accept sync tokens or implement
incremental synchronization.

## Pagination, bounded content and evidence

Each list call fetches exactly one provider page. Follow `next_arguments`
unchanged, even when `items` is empty. Page tokens do not imply stable snapshots.
A repeated cursor produces an explicit incomplete-coverage warning and no
continuation. An unexpectedly oversized provider page is clipped to the requested
size and marked `page_truncated`; skipping those records never reports complete
coverage.

`coverage.complete` means this single call covered the requested enumeration and
returned content without adapter truncation. It is false for continuation pages,
pages with a continuation, clipped pages, or clipped record content. A final
continuation page alone does not establish that earlier pages were read. Coverage
also identifies the window, cancellation/hidden/deleted filters and live-page
consistency. Calendar-list coverage applies to calendars on the user's list,
not every calendar they might have permission to access.

Events retain exact provider `id`, `etag`, `updated` and `htmlLink` when present.
`url` is an exact alias of `htmlLink` for the current Dot evidence parser. No
revision is synthesized, and collection revisions are never assigned to events.
An event without a provider revision cannot support revision-grounded Dot
evidence until a later read actually returns one.

Only selected fields are returned. Summary and location are capped at 1,000
characters, description at 4,000, attendees at 20, and recurrence rules at 20.
Provider attendee omission and every local content truncation appear in
`truncated_fields`; provider revision and identity strings are rejected if
oversized rather than clipped. `content` joins returned provider summary,
description, location and date/time strings for exact quotation. It is a text
view, not a provider revision or an instruction. Conference passwords, arbitrary
extended properties and unrelated raw fields are excluded.

## Verification

Run `uv run --frozen pytest -q tests/test_google_calendar_tools.py` after the shared
dependencies are installed. Tests use the real Google generated client and a
synthetic HTTP transport to check encoding, pagination, threading, bounded reads,
recurrence, timezones, all-day dates, cancellations, and safe non-retrying failures.
They do not touch live accounts.

These adapter tests supplement the required actual BerriAI/moyai baseline/final
application E2E with a real configured model/provider. OAuth consent, host refresh,
permission revocation, Dot enrollment, real provider reads and proactive delivery
require live provider verification in addition to these fixture checks. See
[personal Google connections](google-connections.md) for deployment setup.

## Official references

- [CalendarList.list](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list)
- [Events.list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)
- [Events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get)
- [Event resource and cancellation semantics](https://developers.google.com/workspace/calendar/api/v3/reference/events)
- [Google Python client thread safety](https://googleapis.github.io/google-api-python-client/docs/thread_safety.html)
