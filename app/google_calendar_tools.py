"""Bounded Google Calendar reads through Google's generated v3 client."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Annotated, Protocol, TypeGuard, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient import discovery
from googleapiclient.errors import Error as GoogleAPIError
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from pydantic import (
    AwareDatetime,
    Field,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import TypedDict

from .connector_errors import ConnectorError
from .connector_schemas import Args

SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
})
TIMEOUT_SECONDS = 20
READ_RETRIES = 2
MAX_ATTENDEES = 20
MAX_RANGE_DAYS = 31
OpaqueID = Annotated[str, Field(min_length=1, max_length=1024, strict=True)]
_OBJECT = TypeAdapter(dict[str, object])
_TOKEN = TypeAdapter(Annotated[str, Field(min_length=1, strict=True)])


class Request(Protocol):
    def execute(self, *, num_retries: int = 0) -> object: ...


class CalendarList(Protocol):
    def list(self, *, maxResults: int, pageToken: str | None, showHidden: bool,
             showDeleted: bool, fields: str) -> Request: ...


class Events(Protocol):
    def get(self, *, calendarId: str, timeZone: str | None, maxAttendees: int,
            eventId: str, fields: str) -> Request: ...

    def list(self, *, calendarId: str, timeZone: str | None, maxAttendees: int,
             timeMin: str, timeMax: str, singleEvents: bool, showDeleted: bool,
             orderBy: str, maxResults: int, pageToken: str | None, fields: str) -> Request: ...


class Service(Protocol):
    def calendarList(self) -> CalendarList: ...
    def events(self) -> Events: ...
    def close(self) -> None: ...


class ServiceBuilder(Protocol):
    def __call__(self, serviceName: str, version: str, *,
                 http: google_auth_httplib2.AuthorizedHttp, cache_discovery: bool,
                 static_discovery: bool, num_retries: int, requestBuilder: type[HttpRequest]) -> Service: ...


class ErrorResponse(Protocol):
    status: int
    def get(self, key: str, default: str = "") -> str: ...


class Discovery(Protocol):
    build: ServiceBuilder


# Discovery constructs resources dynamically; type only the SDK methods we use.
build = cast(Discovery, discovery).build
ServiceFactory = Callable[[str], Service]


class EventTime(TypedDict, total=False):
    date: str
    dateTime: str
    timeZone: str


class Person(TypedDict, total=False):
    id: str
    email: str
    displayName: str
    responseStatus: str
    self: bool
    organizer: bool
    optional: bool
    resource: bool


class RecordBase(TypedDict):
    id: str
    content: str
    truncated_fields: list[str]


class CalendarRecord(RecordBase, total=False):
    kind: str
    etag: str
    updated: str
    created: str
    htmlLink: str
    url: str
    status: str
    eventType: str
    recurringEventId: str
    iCalUID: str
    timeZone: str
    accessRole: str
    transparency: str
    visibility: str
    summary: str
    summaryOverride: str
    description: str
    location: str
    primary: bool
    selected: bool
    hidden: bool
    deleted: bool
    endTimeUnspecified: bool
    attendeesOmitted: bool
    sequence: int
    start: EventTime
    end: EventTime
    originalStartTime: EventTime
    organizer: Person
    creator: Person
    attendees: list[Person]
    recurrence: list[str]
    all_day: bool | None


class Coverage(TypedDict, total=False):
    scope: str
    has_more: bool
    page_truncated: bool
    records_truncated: bool
    complete: bool
    warning: str
    consistency: str
    calendar_id: str
    time_min: str
    time_max: str
    time_zone: str | None
    single_events: bool
    include_cancelled: bool
    bounds: str
    cancellations: str
    include_hidden: bool
    include_deleted: bool


class PageBase(TypedDict):
    items: list[CalendarRecord]
    next_arguments: dict[str, str | int | bool | None] | None
    coverage: Coverage


class PageResult(PageBase, total=False):
    timeZone: str
    accessRole: str


class EventResult(CalendarRecord):
    calendar_id: str
    coverage: Coverage


_RECORD = TypeAdapter(CalendarRecord)
_PERSON = TypeAdapter(Person)
_CONTINUATION = TypeAdapter(dict[str, str | int | bool | None])


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


TOOLS: dict[str, tuple[str, bool, type[CalendarPage | CalendarEvents | CalendarEvent], str]] = {
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


def _object(value: object) -> dict[str, object]:
    try:
        return _OBJECT.validate_python(value, strict=True)
    except ValidationError:
        raise _invalid_response() from None


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _record(value: object, *, kind: str) -> CalendarRecord:
    source = _object(value)
    if source.get("kind", kind) != kind:
        raise _invalid_response()
    identity = _text(source.get("id"), 1024)
    if not identity:
        raise _invalid_response()
    result: dict[str, object] = {"id": identity}
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
        value = _object(source[key])
        if "date" in value and "dateTime" in value:
            raise _invalid_response()
        result[key] = {
            field: _text(value[field], 128 if field == "timeZone" else 64)
            for field in ("date", "dateTime", "timeZone") if field in value
        }
    for key in ("organizer", "creator"):
        if key in source:
            result[key] = _person(source[key], truncated, key)
    if "attendees" in source:
        attendees = source["attendees"]
        if not _is_list(attendees):
            raise _invalid_response()
        result["attendees"] = [
            _person(person, truncated, f"attendees[{index}]")
            for index, person in enumerate(attendees[:MAX_ATTENDEES])
        ]
        if len(attendees) > MAX_ATTENDEES:
            result["attendeesOmitted"] = True
    if result.get("attendeesOmitted"):
        truncated.append("attendees")
    if "recurrence" in source:
        recurrence = source["recurrence"]
        if not _is_list(recurrence):
            raise _invalid_response()
        # Rules are never clipped into a different valid-looking recurrence.
        result["recurrence"] = [_text(rule, 2048) for rule in recurrence[:20]]
        if len(recurrence) > 20:
            truncated.append("recurrence")
    if kind == "calendar#event":
        start = _object(result.get("start", {}))
        result["all_day"] = True if "date" in start else False if "dateTime" in start else None
    if "htmlLink" in result:
        result["url"] = result["htmlLink"]  # Exact alias understood by DotEvidence.
    # Each part is provider text, also retained in its original field. This lets
    # attention cite dates without depending on traversal of nested objects.
    parts = [_text(result[key], 4000) for key in ("summary", "summaryOverride", "description", "location") if result.get(key)]
    for key in ("start", "end", "originalStartTime"):
        value = _object(result.get(key, {}))
        parts.extend(_text(value[field], 128) for field in ("date", "dateTime", "timeZone") if value.get(field))
    result["content"] = "\n".join(parts)
    result["truncated_fields"] = sorted(set(truncated))
    try:
        return _RECORD.validate_python(result, strict=True)
    except ValidationError:
        raise _invalid_response() from None


def _person(value: object, truncated: list[str], path: str) -> Person:
    source = _object(value)
    result: dict[str, str | bool] = {}
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
            value = source[key]
            if type(value) is not bool:
                raise _invalid_response()
            result[key] = value
    return _PERSON.validate_python(result, strict=True)


def _page(value: object, arguments: CalendarPage | CalendarEvents) -> PageResult:
    events = isinstance(arguments, CalendarEvents)
    kind = "calendar#events" if events else "calendar#calendarList"
    data = _object(value)
    if data.get("kind") != kind:
        raise _invalid_response()
    items = data.get("items", [])
    if not _is_list(items):
        raise _invalid_response()
    cursor = _text(data.get("nextPageToken", ""), 8192)
    page_truncated = len(items) > arguments.page_size
    records = [
        _record(item, kind="calendar#event" if events else "calendar#calendarListEntry")
        for item in items[:arguments.page_size]
    ]
    records_truncated = any(record["truncated_fields"] for record in records)
    next_arguments = _CONTINUATION.validate_python(
        {**arguments.model_dump(mode="json"), "cursor": cursor}, strict=True,
    ) if cursor and cursor != arguments.cursor else None
    warnings: list[str] = []
    if page_truncated:
        warnings.append("Provider returned more than the requested page size; omitted records make coverage incomplete.")
    if cursor and next_arguments is None:
        warnings.append("Provider repeated the cursor; coverage is incomplete and no continuation is safe.")
    coverage: Coverage = {
        "scope": "events_overlapping_window" if events else "user_calendar_list",
        "has_more": bool(cursor),
        "page_truncated": page_truncated,
        "records_truncated": records_truncated,
        "complete": not cursor and not arguments.cursor and not page_truncated and not records_truncated,
        "warning": " ".join(warnings),
        "consistency": "live provider pages; concurrent changes may affect enumeration",
    }
    if isinstance(arguments, CalendarEvents):
        coverage.update(
            calendar_id=arguments.calendar_id, time_min=arguments.time_min.isoformat(), time_max=arguments.time_max.isoformat(),
            time_zone=arguments.time_zone, single_events=True, include_cancelled=arguments.include_cancelled,
            bounds="event end > time_min and event start < time_max; all-day end dates are exclusive",
            cancellations="Expanded instances only, not a deletion feed; absent events do not prove deletion.",
        )
    else:
        coverage.update(include_hidden=arguments.include_hidden, include_deleted=arguments.include_deleted)
    result: PageResult = {"items": records, "next_arguments": next_arguments, "coverage": coverage}
    if "timeZone" in data:
        result["timeZone"] = _text(data["timeZone"], 128)
    if "accessRole" in data:
        result["accessRole"] = _text(data["accessRole"], 128)
    # A collection ETag/updated timestamp must never become an event revision.
    return result


def _http_error(exc: HttpError) -> ConnectorError:
    response = cast(ErrorResponse, exc.resp)
    status = response.status
    rate_limited = status == 429
    if status == 403:
        try:
            errors = _object(_object(json.loads(exc.content)).get("error")).get("errors", [])
            rate_limited = any(
                _object(entry).get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
                for entry in errors
            ) if _is_list(errors) else False
        except (ValueError, TypeError, ConnectorError):
            pass
    retry_after = None
    value = response.get("retry-after", "")
    if re.fullmatch(r"\d{1,5}", value):
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
        message, code, retryable = "Google Calendar read failed.", "provider_error", status == 408 or 500 <= status <= 599
    return ConnectorError(
        message, code=code, dispatched=True, retryable=retryable, status_code=status, retry_after=retry_after,
    )


def _http() -> httplib2.Http:
    return httplib2.Http(timeout=TIMEOUT_SECONDS)


def _service(access_token: str, *, http_factory: Callable[[], httplib2.Http] = _http,
             request_builder: type[HttpRequest] = HttpRequest) -> Service:
    transport = http_factory()
    transport.follow_redirects = False
    try:
        authorized = google_auth_httplib2.AuthorizedHttp(
            Credentials(token=access_token), http=transport,
            max_refresh_attempts=0, refresh_status_codes=(),
        )
        return build(
            "calendar", "v3", http=authorized, static_discovery=True, cache_discovery=False, num_retries=0,
            requestBuilder=request_builder,
        )
    except Exception:
        transport.close()
        raise


Result = PageResult | EventResult


def _call_sync(arguments: CalendarPage | CalendarEvents | CalendarEvent, access_token: str,
               service_factory: ServiceFactory) -> Result:
    try:
        # httplib2 transports are not thread safe. Own and close one per call.
        service = service_factory(access_token)
        try:
            if isinstance(arguments, CalendarPage):
                request = service.calendarList().list(
                    maxResults=arguments.page_size, pageToken=arguments.cursor or None,
                    showHidden=arguments.include_hidden, showDeleted=arguments.include_deleted,
                    fields=f"kind,nextPageToken,items({_CALENDAR_FIELDS})",
                )
            elif isinstance(arguments, CalendarEvent):
                request = service.events().get(
                    calendarId=arguments.calendar_id, timeZone=arguments.time_zone,
                    maxAttendees=MAX_ATTENDEES, eventId=arguments.event_id, fields=_EVENT_FIELDS,
                )
            else:
                request = service.events().list(
                    calendarId=arguments.calendar_id, timeZone=arguments.time_zone, maxAttendees=MAX_ATTENDEES,
                    timeMin=arguments.time_min.isoformat(), timeMax=arguments.time_max.isoformat(),
                    singleEvents=True, showDeleted=arguments.include_cancelled, orderBy="startTime",
                    maxResults=arguments.page_size, pageToken=arguments.cursor or None,
                    fields=f"kind,timeZone,accessRole,nextPageToken,items({_EVENT_FIELDS})",
                )
            data = request.execute(num_retries=READ_RETRIES)
            if isinstance(arguments, CalendarEvent):
                result = _record(data, kind="calendar#event")
                if result["id"] != arguments.event_id:
                    raise _invalid_response()
                return {
                    **result, "calendar_id": arguments.calendar_id,
                    "coverage": {
                        "scope": "single_event", "records_truncated": bool(result["truncated_fields"]),
                        "time_zone": arguments.time_zone,
                        "bounds": "all-day end dates are exclusive",
                    },
                }
            return _page(data, arguments)
        finally:
            service.close()
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


async def call(name: str, arguments: Mapping[str, object], access_token: str, *,
               service_factory: ServiceFactory = _service) -> Result:
    """Run one read with bounded SDK retries; the host owns token refresh."""
    if name not in TOOLS:
        raise ConnectorError("Unknown Google Calendar tool.", code="unknown_tool", dispatched=False)
    try:
        validated = TOOLS[name][2].model_validate(arguments)
    except ValidationError:
        raise ConnectorError("Invalid Google Calendar arguments.", code="invalid_arguments", dispatched=False) from None
    try:
        token = _TOKEN.validate_python(access_token)
    except ValidationError:
        raise ConnectorError("Connect Google Calendar before reading it.", code="unauthorized", dispatched=False) from None
    if not token.strip():
        raise ConnectorError("Connect Google Calendar before reading it.", code="unauthorized", dispatched=False)
    return await asyncio.to_thread(_call_sync, validated, token, service_factory)
