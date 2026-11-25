"""Bounded Google Calendar reads through Google's generated v3 client."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import closing
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import Error as GoogleAPIError
from googleapiclient.errors import HttpError
from pydantic import (
    AwareDatetime,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from .connector_errors import ConnectorError
from .connector_schemas import Args

SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
})
TIMEOUT_SECONDS = 20
MAX_ATTENDEES = 20
MAX_RANGE_DAYS = 31
OpaqueID = Annotated[str, Field(min_length=1, max_length=1024, strict=True)]


class CalendarPage(Args):
    cursor: str = Field(default="", max_length=8192, strict=True)
    page_size: int = Field(default=25, ge=1, le=100, strict=True)
    include_hidden: bool = Field(default=False, strict=True)
    include_deleted: bool = Field(default=False, strict=True)


class CalendarTarget(Args):
    calendar_id: OpaqueID = Field(default="primary", description="Exact provider ID, or primary.")
    time_zone: str | None = Field(
        default=None, max_length=128, strict=True,
        description="IANA response timezone, e.g. America/Los_Angeles. Omit for the calendar's timezone.",
    )

    @field_validator("time_zone")
    @classmethod
    def valid_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Use an IANA timezone name.") from None
        return value


class CalendarEvents(CalendarTarget):
    time_min: AwareDatetime = Field(
        description="Exclusive lower bound on event END, as whole-second RFC3339 with Z or an offset.",
    )
    time_max: AwareDatetime = Field(
        description="Exclusive upper bound on event START; after time_min and at most 31 days later.",
    )
    cursor: str = Field(default="", max_length=8192, strict=True)
    page_size: int = Field(default=25, ge=1, le=100, strict=True)
    include_cancelled: bool = Field(default=True, strict=True)

    @field_validator("time_min", "time_max", mode="before")
    @classmethod
    def explicit_timestamp(cls, value: object) -> str:
        # Google ignores fractional seconds. Require whole seconds so coverage
        # describes the actual provider query rather than a more precise window.
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", value,
        ):
            raise ValueError("Use whole-second RFC3339 with an explicit timezone offset.")
        return value

    @model_validator(mode="after")
    def bounded_window(self) -> CalendarEvents:
        if not timedelta(0) < self.time_max - self.time_min <= timedelta(days=MAX_RANGE_DAYS):
            raise ValueError("The event window must be positive and at most 31 days.")
        return self

    @field_serializer("time_min", "time_max")
    def timestamp_string(self, value: datetime) -> str:
        # Host authorization validates/dumps before the adapter validates again.
        return value.isoformat()


class CalendarEvent(CalendarTarget):
    event_id: OpaqueID = Field(description="Exact event or recurring-instance ID, not iCalUID.")


TOOLS = {
    "google_calendar_calendars": (
        "google_calendar", False, CalendarPage,
        ("List one page of calendars on the connected user's calendar list. Follow next_arguments; "
         "hidden and deleted entries are excluded by default. This is not every accessible calendar."),
    ),
    "google_calendar_events": (
        "google_calendar", False, CalendarEvents,
        ("Read one page of events overlapping an explicit timezone-aware window of up to 31 days, "
         "defaulting to the primary calendar. For upcoming events choose now through a future time. "
         "Recurring events are expanded into instances and cancelled instances are included by default. "
         "Follow next_arguments unchanged; a short or empty page can still have more results. "
         "All-day end dates are exclusive. Missing events are not proof of deletion."),
    ),
    "google_calendar_event": (
        "google_calendar", False, CalendarEvent,
        ("Read one exact event, recurrence master or instance by its provider ID on a calendar. "
         "Preserves provider revisions, cancellation status and recurrence details. "
         "A cancelled record may have only an ID; never invent a revision or event details."),
    ),
}
READS = frozenset(TOOLS)

_PERSON_FIELDS = "id,email,displayName,self"
_EVENT_FIELDS = (
    "kind,id,etag,status,htmlLink,created,updated,summary,description,location,eventType,"
    f"creator({_PERSON_FIELDS}),organizer({_PERSON_FIELDS}),"
    "start(date,dateTime,timeZone),end(date,dateTime,timeZone),endTimeUnspecified,"
    "recurrence,recurringEventId,originalStartTime(date,dateTime,timeZone),"
    "transparency,visibility,iCalUID,sequence,attendeesOmitted,"
    f"attendees({_PERSON_FIELDS},organizer,optional,resource,responseStatus)"
)
_CALENDAR_FIELDS = (
    "kind,id,etag,summary,summaryOverride,description,location,timeZone,accessRole,"
    "primary,selected,hidden,deleted"
)


def _invalid_response() -> ConnectorError:
    return ConnectorError("Google Calendar returned an invalid response.", code="invalid_response", dispatched=True)


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid_response()
    return value


def _record(source: object, *, kind: str) -> dict:
    if not isinstance(source, dict) or source.get("kind", kind) != kind:
        raise _invalid_response()
    identity = _text(source.get("id"), 1024)
    if not identity:
        raise _invalid_response()
    result: dict = {"id": identity}
    truncated: list[str] = []
    # Identity and revision fields are exact or rejected, never clipped.
    for key, limit in {
        "kind": 64, "etag": 1024, "updated": 64, "created": 64, "htmlLink": 4096,
        "status": 64, "eventType": 64, "recurringEventId": 1024, "iCalUID": 2048,
        "timeZone": 128, "accessRole": 64, "transparency": 64, "visibility": 64,
    }.items():
        if key in source:
            result[key] = _text(source[key], limit)
    for key, limit in {"summary": 1000, "summaryOverride": 1000, "description": 4000, "location": 1000}.items():
        if key in source:
            value = source[key]
            if not isinstance(value, str):
                raise _invalid_response()
            result[key] = value[:limit]
            if len(value) > limit:
                truncated.append(key)
    for key in ("primary", "selected", "hidden", "deleted", "endTimeUnspecified", "attendeesOmitted"):
        if key in source:
            if type(source[key]) is not bool:
                raise _invalid_response()
            result[key] = source[key]
    if "sequence" in source:
        if type(source["sequence"]) is not int or not 0 <= source["sequence"] <= 2**53 - 1:
            raise _invalid_response()
        result["sequence"] = source["sequence"]
    for key in ("start", "end", "originalStartTime"):
        if key not in source:
            continue
        value = source[key]
        if not isinstance(value, dict) or ("date" in value and "dateTime" in value):
            raise _invalid_response()
        result[key] = {
            field: _text(value[field], 128 if field == "timeZone" else 64)
            for field in ("date", "dateTime", "timeZone") if field in value
        }
    for key in ("organizer", "creator"):
        if key in source:
            result[key] = _person(source[key], truncated, key)
    if "attendees" in source:
        if not isinstance(source["attendees"], list):
            raise _invalid_response()
        result["attendees"] = [
            _person(person, truncated, f"attendees[{index}]")
            for index, person in enumerate(source["attendees"][:MAX_ATTENDEES])
        ]
        if len(source["attendees"]) > MAX_ATTENDEES:
            result["attendeesOmitted"] = True
    if result.get("attendeesOmitted"):
        truncated.append("attendees")
    if "recurrence" in source:
        if not isinstance(source["recurrence"], list):
            raise _invalid_response()
        # Rules are never clipped into a different valid-looking recurrence.
        result["recurrence"] = [_text(rule, 2048) for rule in source["recurrence"][:20]]
        if len(source["recurrence"]) > 20:
            truncated.append("recurrence")
    if kind == "calendar#event":
        start = result.get("start", {})
        result["all_day"] = True if "date" in start else False if "dateTime" in start else None
    if "htmlLink" in result:
        result["url"] = result["htmlLink"]  # Exact alias understood by DotEvidence.
    # Each part is provider text, also retained in its original field. This lets
    # attention cite dates without depending on traversal of nested objects.
    parts = [result[key] for key in ("summary", "summaryOverride", "description", "location") if result.get(key)]
    for key in ("start", "end", "originalStartTime"):
        value = result.get(key, {})
        parts.extend(value[field] for field in ("date", "dateTime", "timeZone") if value.get(field))
    result["content"] = "\n".join(parts)
    result["truncated_fields"] = sorted(set(truncated))
    return result


def _person(source: object, truncated: list[str], path: str) -> dict:
    if not isinstance(source, dict):
        raise _invalid_response()
    result = {}
    for key, limit in {"id": 1024, "email": 512, "displayName": 512, "responseStatus": 64}.items():
        if key not in source:
            continue
        value = source[key]
        if not isinstance(value, str):
            raise _invalid_response()
        if key == "displayName":
            result[key] = value[:limit]
            if len(value) > limit:
                truncated.append(f"{path}.{key}")
        else:
            result[key] = _text(value, limit)
    for key in ("self", "organizer", "optional", "resource"):
        if key in source:
            if type(source[key]) is not bool:
                raise _invalid_response()
            result[key] = source[key]
    return result


def _page(data: object, arguments: dict, *, events: bool) -> dict:
    kind = "calendar#events" if events else "calendar#calendarList"
    if not isinstance(data, dict) or data.get("kind") != kind:
        raise _invalid_response()
    items = data.get("items", [])
    if not isinstance(items, list):
        raise _invalid_response()
    cursor = _text(data.get("nextPageToken", ""), 8192)
    page_truncated = len(items) > arguments["page_size"]
    records = [
        _record(item, kind="calendar#event" if events else "calendar#calendarListEntry")
        for item in items[:arguments["page_size"]]
    ]
    records_truncated = any(record["truncated_fields"] for record in records)
    next_arguments = {**arguments, "cursor": cursor} if cursor and cursor != arguments["cursor"] else None
    warnings = []
    if page_truncated:
        warnings.append("Provider returned more than the requested page size; omitted records make coverage incomplete.")
    if cursor and next_arguments is None:
        warnings.append("Provider repeated the cursor; coverage is incomplete and no continuation is safe.")
    coverage = {
        "scope": "events_overlapping_window" if events else "user_calendar_list",
        "has_more": bool(cursor),
        "page_truncated": page_truncated,
        "records_truncated": records_truncated,
        "complete": not cursor and not arguments["cursor"] and not page_truncated and not records_truncated,
        "warning": " ".join(warnings),
        "consistency": "live provider pages; concurrent changes may affect enumeration",
    }
    if events:
        coverage.update(
            calendar_id=arguments["calendar_id"], time_min=arguments["time_min"], time_max=arguments["time_max"],
            time_zone=arguments["time_zone"], single_events=True, include_cancelled=arguments["include_cancelled"],
            bounds="event end > time_min and event start < time_max; all-day end dates are exclusive",
            cancellations="Expanded instances only, not a deletion feed; absent events do not prove deletion.",
        )
    else:
        coverage.update(include_hidden=arguments["include_hidden"], include_deleted=arguments["include_deleted"])
    result = {"items": records, "next_arguments": next_arguments, "coverage": coverage}
    for key in ("timeZone", "accessRole"):
        if key in data:
            result[key] = _text(data[key], 128)
    # A collection ETag/updated timestamp must never become an event revision.
    return result


def _http_error(exc: HttpError) -> ConnectorError:
    status = getattr(exc.resp, "status", None)
    rate_limited = status == 429
    if status == 403:
        try:
            payload = json.loads(exc.content)
            rate_limited = any(
                entry.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
                for entry in payload["error"]["errors"] if isinstance(entry, dict)
            )
        except (ValueError, TypeError, KeyError):
            pass
    retry_after = None
    value = exc.resp.get("retry-after", "")
    if isinstance(value, str) and re.fullmatch(r"\d{1,5}", value):
        retry_after = int(value)
    if rate_limited:
        message, code, retryable = "Google Calendar rate limit reached.", "rate_limited", True
    elif status == 401:
        message, code, retryable = "Google Calendar authorization expired; reconnect the account.", "unauthorized", False
    elif status == 403:
        message, code, retryable = "Google Calendar denied access. Check the account's read scopes and calendar access.", "forbidden", False
    elif status in {404, 410}:
        message, code, retryable = "Google Calendar could not return this resource or page. This does not prove deletion.", "not_found", False
    elif status == 400:
        message, code, retryable = "Google Calendar rejected the read parameters or cursor.", "invalid_request", False
    else:
        message, code, retryable = "Google Calendar read failed.", "provider_error", status == 408 or (
            isinstance(status, int) and 500 <= status <= 599
        )
    return ConnectorError(
        message, code=code, dispatched=True, retryable=retryable, status_code=status, retry_after=retry_after,
    )


def _call_sync(name: str, arguments: dict, access_token: str) -> dict:
    try:
        # httplib2 transports are not thread safe. Own and close one per call.
        with closing(httplib2.Http(timeout=TIMEOUT_SECONDS)) as transport:
            transport.follow_redirects = False
            authorized = google_auth_httplib2.AuthorizedHttp(
                Credentials(token=access_token), http=transport, max_refresh_attempts=0,
            )
            service = build(
                "calendar", "v3", http=authorized, static_discovery=True, cache_discovery=False, num_retries=0,
            )
            if name == "google_calendar_calendars":
                request = service.calendarList().list(
                    maxResults=arguments["page_size"], pageToken=arguments["cursor"] or None,
                    showHidden=arguments["include_hidden"], showDeleted=arguments["include_deleted"],
                    fields=f"kind,nextPageToken,items({_CALENDAR_FIELDS})",
                )
            else:
                target = {
                    "calendarId": arguments["calendar_id"], "timeZone": arguments["time_zone"],
                    "maxAttendees": MAX_ATTENDEES,
                }
                if name == "google_calendar_event":
                    request = service.events().get(
                        **target, eventId=arguments["event_id"], fields=_EVENT_FIELDS,
                    )
                else:
                    request = service.events().list(
                        **target, timeMin=arguments["time_min"], timeMax=arguments["time_max"],
                        singleEvents=True, showDeleted=arguments["include_cancelled"], orderBy="startTime",
                        maxResults=arguments["page_size"], pageToken=arguments["cursor"] or None,
                        fields=f"kind,timeZone,accessRole,nextPageToken,items({_EVENT_FIELDS})",
                    )
            data = request.execute(num_retries=0)
            if name == "google_calendar_event":
                result = _record(data, kind="calendar#event")
                if result["id"] != arguments["event_id"]:
                    raise _invalid_response()
                return {
                    **result, "calendar_id": arguments["calendar_id"],
                    "coverage": {
                        "scope": "single_event", "records_truncated": bool(result["truncated_fields"]),
                        "time_zone": arguments["time_zone"],
                        "bounds": "all-day end dates are exclusive",
                    },
                }
            return _page(data, arguments, events=name == "google_calendar_events")
    except ConnectorError:
        raise
    except HttpError as exc:
        raise _http_error(exc) from None
    except GoogleAuthError:
        raise ConnectorError(
            "Google Calendar authorization failed; reconnect the account.", code="unauthorized", dispatched=False,
        ) from None
    except (OSError, httplib2.HttpLib2Error):
        raise ConnectorError(
            "Google Calendar could not be reached.", code="transport_error", dispatched=True, retryable=True,
        ) from None
    except (GoogleAPIError, ValueError, TypeError, KeyError, HTTPException):
        # SDK errors can contain the request URI or raw provider body. Only
        # controlled metadata crosses the credential-bearing process boundary.
        raise ConnectorError("Google Calendar read failed.", code="provider_error", dispatched=True) from None


async def call(name: str, arguments: dict, access_token: str) -> dict:
    """Run one bounded read with a current token; the host owns refresh/retries."""
    if name not in TOOLS:
        raise ConnectorError("Unknown Google Calendar tool.", code="unknown_tool", dispatched=False)
    try:
        validated = TOOLS[name][2].model_validate(arguments).model_dump(mode="json")
    except ValidationError:
        raise ConnectorError("Invalid Google Calendar arguments.", code="invalid_arguments", dispatched=False) from None
    if not isinstance(access_token, str) or not access_token.strip():
        raise ConnectorError("Connect Google Calendar before reading it.", code="unauthorized", dispatched=False)
    return await asyncio.to_thread(_call_sync, name, validated, access_token)
