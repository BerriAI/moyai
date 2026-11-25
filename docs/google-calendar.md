# Google Calendar read adapter

`app/google_calendar_tools.py` supplies a read-only provider adapter for the
`google_calendar` connection kind. OAuth, credential refresh, account selection,
connection permissions, Dot enrollment and scheduling belong to the host. This
module does not install an account, schedule checks, or mutate calendars.

The integration exports `TOOLS`, `READS` and an asynchronous `call` accepting
`name: str`, `arguments: Mapping[str, object]` and `access_token: str`.
It returns `Result`, the union of typed page and single-event dictionaries.
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

The adapter accepts the host's current access token. Each logical read uses
`execute(num_retries=2)`: at most three SDK attempts, with the SDK's randomized
exponential backoff for supported transient transport errors, HTTP 5xx,
HTTP 429, and HTTP 403 reasons `rateLimitExceeded` or `userRateLimitExceeded`.
HTTP 401 and ordinary permission-denied 403 responses are not retried, including
when they follow a transient failure. A retry repeats the same request, including
its cursor, time window and filters.

Bundled discovery initialization still uses `num_retries=0`. Credential refresh
attempts are disabled, and no refresh token enters this module. The host owns
token refresh and decisions to attempt later tool calls. The transport can retry
certain connection failures internally; the SDK attempt budget and socket
timeout are not hard wall-clock cancellation guarantees for a worker thread.
HTTP errors expose controlled status/retry metadata, never raw response bodies,
credential values or request URLs. Retry-After is returned as metadata; retry
delays within this adapter follow the SDK's backoff policy.

## Typed boundaries

Arguments are validated into `CalendarPage`, `CalendarEvents` or `CalendarEvent`
before transport creation, and those models are used throughout dispatch.
Datetime serialization preserves the existing string-based continuation
arguments. Provider payloads enter as untrusted objects; field checks and
strict Pydantic `TypeAdapter` validation produce calendar/event records,
including their nested event times and people. Continuation arguments are also
validated. Normalized records, coverage and result envelopes have explicit
`TypedDict` shapes; coverage and envelopes are constructed from the validated
inputs and records. Public results remain ordinary dictionaries with the same
field names and omission behavior.

Small protocols describe only the generated SDK methods used by the adapter;
the dynamic discovery factory is isolated at that boundary. The keyword-only
`service_factory` supports trusted caller/test injection, while the service
factory also accepts an HTTP factory and SDK request class for transport tests.
These injection points are not exposed in model-visible tool arguments.

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

Each list call requests one provider page, with bounded retries of that same
request if needed. Follow `next_arguments` unchanged, even when `items` is empty.
Page tokens do not imply stable snapshots.
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
dependencies are installed. Tests use the real Google generated client and an
injected synthetic HTTP transport to check encoding, pagination, threading,
bounded reads, recurrence, timezones, all-day dates, cancellations and sanitized
failures. Retry tests cover transient recovery, exhausted budgets, immediate
termination on authentication failures and preservation of the window/cursor.
Per-instance SDK sleep/random replacements verify backoff without waiting.
The tests do not touch live accounts.

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
