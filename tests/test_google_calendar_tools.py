"""Adapter contracts against the real Google SDK and a synthetic HTTP transport."""
import asyncio
import json
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import httplib2
import pytest
from googleapiclient.http import HttpRequest

from app import google_calendar_tools as calendar
from app.connector_errors import ConnectorError
from app.connector_schemas import Args

WINDOW = {"time_min": "2026-11-25T00:00:00-08:00", "time_max": "2026-11-27T00:00:00-08:00"}
TOKEN = "synthetic-calendar-access-token"


@pytest.fixture
def provider():
    class Provider:
        def __init__(self):
            self.responses = []
            self.calls = []
            self.transports = []
            self.sleeps = []

        def add(self, data, *, status=200, headers=None):
            self.responses.append((
                httplib2.Response({"status": str(status), "content-type": "application/json", **(headers or {})}),
                json.dumps(data).encode(),
            ))

        def service(self, token):
            return calendar._service(token, http_factory=Transport, request_builder=Request)

        async def call(self, name, arguments, token):
            return await calendar.call(name, arguments, token, service_factory=self.service)

    backend = Provider()

    class Transport(httplib2.Http):
        def __init__(self):
            self.timeout = calendar.TIMEOUT_SECONDS
            self.closed = False
            self.follow_redirects = True
            backend.transports.append(self)

        def request(self, uri, method="GET", body=None, headers=None, **kwargs):
            backend.calls.append({
                "uri": uri, "method": method, "body": body, "headers": headers, "thread": threading.get_ident(),
            })
            response = backend.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    class Request(HttpRequest):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._sleep = backend.sleeps.append
            self._rand = lambda: 0.5

    return backend


def event(**extra):
    return {
        "kind": "calendar#event", "id": "event-123", "etag": '"provider-etag"',
        "updated": "2026-11-24T18:01:00.123Z", "status": "confirmed",
        "htmlLink": "https://www.google.com/calendar/event?eid=provider-id",
        "summary": "Planning", **extra,
    }


def event_page(items, **extra):
    return {"kind": "calendar#events", "timeZone": "America/Los_Angeles", "items": items, **extra}


def test_tools_are_read_only_args_and_have_no_caller_controlled_endpoint():
    assert calendar.READS == frozenset(calendar.TOOLS)
    assert len(calendar.TOOLS) == 3
    assert calendar.SCOPES == {
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    }
    for provider, write, model, description in calendar.TOOLS.values():
        assert provider == "google_calendar" and write is False
        assert issubclass(model, Args)
        assert description
        assert not {"url", "method", "access_token", "headers"} & model.model_fields.keys()


async def test_sdk_owns_escaping_transport_timeout_and_thread(provider, monkeypatch):
    builds = []
    original = calendar.build

    def build(*args, **kwargs):
        builds.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(calendar, "build", build)
    # Slashes, query separators and percent signs must stay inside one SDK path
    # argument, never alter the host, path or query structure.
    calendar_id = "team+one@example.test/a?x=1&y=%2F#fragment"
    event_id = "opaque/a?x=1&y=%2F#fragment"
    provider.add(event(id=event_id))
    result = await provider.call("google_calendar_event", {
        "calendar_id": calendar_id, "event_id": event_id, "time_zone": "America/Los_Angeles",
    }, TOKEN)
    request = provider.calls[0]
    uri = urlsplit(request["uri"])
    assert uri.scheme == "https" and uri.hostname == "www.googleapis.com"
    segments = uri.path.split("/")
    assert segments[:4] == ["", "calendar", "v3", "calendars"]
    assert len(segments) == 7 and segments[5] == "events"
    assert unquote(segments[4]) == calendar_id and unquote(segments[6]) == event_id
    query = parse_qs(uri.query)
    assert query["maxAttendees"] == [str(calendar.MAX_ATTENDEES)]
    assert query["timeZone"] == ["America/Los_Angeles"]
    assert "x" not in query and "y" not in query
    assert TOKEN not in request["uri"] and request["body"] is None and request["method"] == "GET"
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert request["thread"] != threading.get_ident()
    transport = provider.transports[0]
    assert transport.timeout == calendar.TIMEOUT_SECONDS and transport.closed
    assert transport.follow_redirects is False
    assert builds[0]["static_discovery"] is True and builds[0]["cache_discovery"] is False
    assert builds[0]["num_retries"] == 0
    assert builds[0]["http"]._max_refresh_attempts == 0
    assert result["id"] == event_id and result["calendar_id"] == calendar_id
    assert result["etag"] == '"provider-etag"' and result["updated"] == "2026-11-24T18:01:00.123Z"
    assert result["htmlLink"] == result["url"]


async def test_concurrent_calls_never_share_a_transport(provider):
    provider.add(event())
    provider.add(event())
    results = await asyncio.gather(*(
        provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN) for _ in range(2)
    ))
    assert len(results) == 2
    assert len(provider.transports) == 2 and all(transport.closed for transport in provider.transports)


async def test_empty_calendar_page_continues_and_final_page_is_not_whole_list(provider):
    cursor = "opaque+/=?&cursor"
    provider.add({"kind": "calendar#calendarList", "items": [], "nextPageToken": cursor})
    args = {"page_size": 2, "include_hidden": True, "include_deleted": True}
    first = await provider.call("google_calendar_calendars", args, TOKEN)
    assert first["items"] == []
    assert first["coverage"]["has_more"] is True and first["coverage"]["complete"] is False
    assert first["next_arguments"] == {**args, "cursor": cursor}
    provider.add({"kind": "calendar#calendarList", "items": [{
        "kind": "calendar#calendarListEntry", "id": "gone@example.test", "etag": '"calendar-etag"',
        "summary": "Old calendar", "deleted": True, "hidden": True, "timeZone": "Asia/Tokyo",
    }]})
    final = await provider.call("google_calendar_calendars", first["next_arguments"], TOKEN)
    query = parse_qs(urlsplit(provider.calls[-1]["uri"]).query)
    assert query["pageToken"] == [cursor] and query["showHidden"] == ["true"] and query["showDeleted"] == ["true"]
    assert final["coverage"]["has_more"] is False and final["coverage"]["complete"] is False
    assert final["next_arguments"] is None
    assert final["items"][0]["deleted"] is True and final["items"][0]["etag"] == '"calendar-etag"'


async def test_calendar_defaults_and_empty_list_coverage(provider):
    provider.add({"kind": "calendar#calendarList"})
    result = await provider.call("google_calendar_calendars", {}, TOKEN)
    assert result["items"] == [] and result["coverage"]["complete"] is True
    query = parse_qs(urlsplit(provider.calls[0]["uri"]).query)
    assert query["showHidden"] == ["false"] and query["showDeleted"] == ["false"]
    assert "pageToken" not in query


async def test_events_keep_all_day_moved_instances_timezones_and_sparse_cancellations(provider):
    moved = event(
        start={"dateTime": "2026-11-25T16:00:00-08:00", "timeZone": "America/Los_Angeles"},
        end={"dateTime": "2026-11-25T17:00:00-08:00", "timeZone": "America/Los_Angeles"},
        recurringEventId="series-123",
        originalStartTime={"dateTime": "2026-11-25T15:00:00-08:00", "timeZone": "America/Los_Angeles"},
    )
    all_day = event(
        id="holiday", summary="Holiday", start={"date": "2026-11-26"}, end={"date": "2026-11-27"},
    )
    cancelled = {
        "id": "cancelled-instance", "status": "cancelled", "recurringEventId": "series-123",
        "originalStartTime": {"dateTime": "2026-11-26T15:00:00-08:00"},
    }
    provider.add(event_page(
        [moved, all_day, cancelled], etag='"collection-etag"', updated="2026-11-25T18:00:00Z",
    ))
    result = await provider.call("google_calendar_events", WINDOW, TOKEN)
    timed, holiday, tombstone = result["items"]
    assert timed["start"] == moved["start"] and timed["originalStartTime"] == moved["originalStartTime"]
    assert timed["all_day"] is False and timed["recurringEventId"] == "series-123"
    assert "2026-11-25T16:00:00-08:00" in timed["content"]
    assert holiday["start"] == {"date": "2026-11-26"} and holiday["end"] == {"date": "2026-11-27"}
    assert holiday["all_day"] is True and "dateTime" not in holiday["start"]
    assert tombstone["all_day"] is None and tombstone["status"] == "cancelled"
    assert not {"etag", "updated", "htmlLink", "summary", "start", "end"} & tombstone.keys()
    assert not {"etag", "updated"} & result.keys()
    assert result["timeZone"] == "America/Los_Angeles"
    assert result["coverage"]["complete"] is True and result["coverage"]["include_cancelled"] is True
    query = parse_qs(urlsplit(provider.calls[0]["uri"]).query)
    assert query["timeMin"] == [WINDOW["time_min"]] and query["timeMax"] == [WINDOW["time_max"]]
    assert query["singleEvents"] == ["true"] and query["showDeleted"] == ["true"]
    assert query["orderBy"] == ["startTime"] and "timeZone" not in query
    assert "/calendars/primary/events?" in provider.calls[0]["uri"]


async def test_empty_event_page_has_continuation_with_unchanged_window(provider):
    provider.add(event_page([], nextPageToken="page-2"))
    first = await provider.call("google_calendar_events", {**WINDOW, "include_cancelled": False}, TOKEN)
    assert first["coverage"]["complete"] is False and first["coverage"]["has_more"] is True
    next_args = first["next_arguments"]
    assert {key: next_args[key] for key in WINDOW} == WINDOW
    provider.add(event_page([event()]))
    final = await provider.call("google_calendar_events", next_args, TOKEN)
    assert final["coverage"]["complete"] is False and final["coverage"]["has_more"] is False
    query = parse_qs(urlsplit(provider.calls[-1]["uri"]).query)
    assert query["timeMin"] == [WINDOW["time_min"]] and query["showDeleted"] == ["false"]
    assert query["pageToken"] == ["page-2"]


async def test_host_validation_dump_can_be_revalidated_by_adapter(provider):
    args = calendar.TOOLS["google_calendar_events"][2].model_validate(WINDOW).model_dump()
    assert isinstance(args["time_min"], str) and isinstance(args["time_max"], str)
    provider.add(event_page([]))
    result = await provider.call("google_calendar_events", args, TOKEN)
    assert result["coverage"]["complete"] is True


async def test_get_preserves_recurrence_master_and_attendee_omission(provider):
    provider.add(event(
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=America/Los_Angeles:20261201T090000"],
        start={"dateTime": "2026-11-24T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
        end={"dateTime": "2026-11-24T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
        attendees=[{"email": "owner@example.test", "self": True, "responseStatus": "accepted"}],
        attendeesOmitted=True,
    ))
    result = await provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN)
    assert result["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU"
    assert result["attendees"][0]["self"] is True
    assert result["truncated_fields"] == ["attendees"] and result["coverage"]["records_truncated"] is True


async def test_bounded_content_and_attendees_do_not_claim_complete_coverage(provider):
    provider.add(event_page([event(
        description="d" * 5000, summary="s" * 1200, location="l" * 1200,
        attendees=[{"email": f"a{i}@example.test", "displayName": "n" * 600} for i in range(30)],
        extendedProperties={"private": {"secret": "not-an-exposed-field"}},
        conferenceData={"entryPoints": [{"password": "not-an-exposed-field"}]},
    )]))
    result = await provider.call("google_calendar_events", WINDOW, TOKEN)
    record = result["items"][0]
    assert len(record["description"]) == 4000 and len(record["summary"]) == len(record["location"]) == 1000
    assert len(record["attendees"]) == calendar.MAX_ATTENDEES
    assert len(record["attendees"][0]["displayName"]) == 512
    assert record["attendeesOmitted"] is True
    assert {"summary", "description", "location", "attendees"} <= set(record["truncated_fields"])
    assert result["coverage"]["records_truncated"] is True and result["coverage"]["complete"] is False
    assert "not-an-exposed-field" not in json.dumps(result)


async def test_provider_oversize_page_and_repeated_cursor_stay_partial(provider):
    provider.add(event_page([event(id="one"), event(id="two")]))
    result = await provider.call("google_calendar_events", {**WINDOW, "page_size": 1}, TOKEN)
    assert [item["id"] for item in result["items"]] == ["one"]
    assert result["coverage"]["page_truncated"] is True and result["coverage"]["complete"] is False
    assert "omitted records" in result["coverage"]["warning"]
    provider.add(event_page([], nextPageToken="stuck"))
    result = await provider.call("google_calendar_events", {**WINDOW, "cursor": "stuck"}, TOKEN)
    assert result["next_arguments"] is None and result["coverage"]["has_more"] is True
    assert result["coverage"]["complete"] is False and "repeated" in result["coverage"]["warning"]


@pytest.mark.parametrize("patch", [
    {"time_min": "2026-11-25"}, {"time_min": "2026-11-25T00:00:00"},
    {"time_min": "2026-11-25 00:00:00Z"}, {"time_min": "2026-11-25T00:00:00.1Z"},
    {"time_min": 1795584000}, {"time_min": "2026-02-30T00:00:00Z"},
    {"time_min": "2026-11-25T00:00:00+25:00"}, {"time_max": WINDOW["time_min"]},
    {"time_max": "2026-11-24T00:00:00Z"}, {"time_max": "2026-12-27T00:00:00-08:00"},
    {"page_size": 0}, {"page_size": 101}, {"page_size": True}, {"page_size": "25"},
    {"calendar_id": ""}, {"calendar_id": "x" * 1025}, {"cursor": "x" * 8193},
    {"time_zone": "Mars/Olympus"}, {"time_zone": "../etc/passwd"},
    {"include_cancelled": "false"}, {"url": "https://example.test"}, {"sync_token": "not-supported"},
])
async def test_invalid_arguments_are_rejected_before_transport(provider, patch):
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_events", {**WINDOW, **patch}, TOKEN)
    assert failure.value.code == "invalid_arguments" and failure.value.dispatched is False
    assert not provider.calls and not provider.transports
    assert TOKEN not in str(failure.value)


async def test_window_duration_compares_instants_across_offsets(provider):
    # The date labels differ by 31 days, but the actual interval is 31 days + 1 hour.
    with pytest.raises(ConnectorError, match="Invalid Google Calendar arguments"):
        await provider.call("google_calendar_events", {
            "time_min": "2026-10-15T00:00:00-07:00", "time_max": "2026-11-15T00:00:00-08:00",
        }, TOKEN)
    provider.add(event_page([]))
    result = await provider.call("google_calendar_events", {
        "time_min": "2026-10-15T00:00:00-07:00", "time_max": "2026-11-14T23:00:00-08:00",
    }, TOKEN)
    assert result["coverage"]["complete"] is True


@pytest.mark.parametrize(("status", "reason", "code", "retryable"), [
    (400, "badRequest", "invalid_request", False), (401, "authError", "unauthorized", False),
    (403, "insufficientPermissions", "forbidden", False),
    (403, "rateLimitExceeded", "rate_limited", True), (403, "userRateLimitExceeded", "rate_limited", True),
    (404, "notFound", "not_found", False), (410, "gone", "not_found", False),
    (429, "rateLimitExceeded", "rate_limited", True), (500, "backendError", "provider_error", True),
    (503, "backendError", "provider_error", True),
])
async def test_provider_errors_are_safe_and_retries_are_bounded(provider, status, reason, code, retryable):
    attempts = 1 + calendar.READ_RETRIES if retryable else 1
    for _ in range(attempts):
        provider.add({"error": {
            "code": status, "message": f"raw provider message includes {TOKEN}",
            "errors": [{"reason": reason, "message": TOKEN}],
        }}, status=status, headers={"retry-after": "12"})
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN)
    error = failure.value
    assert error.code == code and error.retryable is retryable and error.status_code == status
    assert error.retry_after == 12
    assert TOKEN not in json.dumps(error.receipt()) and "raw provider" not in str(error)
    assert error.__suppress_context__ is True
    assert len(provider.calls) == attempts and provider.transports[0].closed
    assert len(provider.sleeps) == attempts - 1


async def test_network_failure_is_safe_and_has_bounded_retries(provider):
    provider.responses.extend([TimeoutError(f"raw transport contains {TOKEN}")] * (calendar.READ_RETRIES + 1))
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN)
    assert failure.value.code == "transport_error" and failure.value.retryable
    assert TOKEN not in str(failure.value)
    assert len(provider.calls) == calendar.READ_RETRIES + 1 and provider.transports[0].closed


@pytest.mark.parametrize(("status", "reason"), [
    (429, "rateLimitExceeded"), (503, "backendError"),
    (403, "userRateLimitExceeded"), (403, "rateLimitExceeded"),
])
async def test_sdk_retries_transient_reads_and_returns_recovered_result(provider, status, reason):
    provider.add({"error": {"errors": [{"reason": reason}]}}, status=status)
    provider.add(event())
    result = await provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN)
    assert result["id"] == "event-123"
    assert len(provider.calls) == 2 and provider.sleeps == [1.0]
    assert provider.transports[0].closed


async def test_sdk_stops_retrying_when_auth_fails_after_a_transient_error(provider):
    provider.add({"error": {"errors": [{"reason": "backendError"}]}}, status=503)
    provider.add({"error": {"message": "private authentication detail"}}, status=401)
    provider.add(event())
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_event", {"event_id": "event-123"}, TOKEN)
    assert failure.value.code == "unauthorized" and not failure.value.retryable
    assert len(provider.calls) == 2 and provider.sleeps == [1.0]
    assert len(provider.responses) == 1 and provider.transports[0].closed


async def test_event_page_retry_preserves_window_cursor_and_sparse_cancellation(provider):
    provider.add({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, status=429)
    provider.add(event_page([{"id": "cancelled", "status": "cancelled"}], nextPageToken="next"))
    result = await provider.call("google_calendar_events", {**WINDOW, "cursor": "previous"}, TOKEN)
    assert result["items"][0]["status"] == "cancelled"
    assert "etag" not in result["items"][0]
    assert result["next_arguments"]["cursor"] == "next"
    assert not result["coverage"]["complete"]
    assert len(provider.calls) == 2 and provider.calls[0]["uri"] == provider.calls[1]["uri"]
    assert all(call["headers"]["authorization"] == f"Bearer {TOKEN}" for call in provider.calls)


@pytest.mark.parametrize("data", [
    {}, {"kind": "calendar#events", "items": None},
    {"kind": "calendar#events", "items": [None]},
    {"kind": "calendar#events", "items": [{"id": "x" * 1025}]},
    {"kind": "calendar#events", "items": [{"id": "x", "etag": "e" * 1025}]},
    {"kind": "calendar#events", "items": [{"id": "x", "start": {"date": "2026-11-25", "dateTime": "bad"}}]},
    {"kind": "calendar#events", "items": [], "nextPageToken": 123},
])
async def test_malformed_provider_records_fail_closed(provider, data):
    provider.add(data)
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_events", WINDOW, TOKEN)
    assert failure.value.code == "invalid_response"


async def test_get_checks_exact_id_and_does_not_invent_missing_revision(provider):
    provider.add({"id": "different", "status": "cancelled"})
    with pytest.raises(ConnectorError) as failure:
        await provider.call("google_calendar_event", {"event_id": "expected"}, TOKEN)
    assert failure.value.code == "invalid_response"
    provider.add({"id": "expected", "status": "cancelled"})
    result = await provider.call("google_calendar_event", {"event_id": "expected"}, TOKEN)
    assert not {"etag", "updated", "htmlLink", "url"} & result.keys()
    assert result["id"] == "expected" and result["all_day"] is None


async def test_missing_token_and_unknown_tool_do_not_send_requests(provider):
    with pytest.raises(ConnectorError) as unknown:
        await provider.call("google_calendar_delete", {}, TOKEN)
    assert unknown.value.code == "unknown_tool"
    with pytest.raises(ConnectorError) as missing:
        await provider.call("google_calendar_calendars", {}, "")
    assert missing.value.code == "unauthorized"
    assert not provider.transports
