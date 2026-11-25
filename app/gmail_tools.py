"""Read-only Gmail operations; OAuth and account selection belong to the caller."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable, Mapping
from email.header import decode_header, make_header
from email.message import Message
from typing import Annotated, Literal, Protocol, TypeGuard, cast

import google_auth_httplib2
import httplib2
from bs4 import BeautifulSoup
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from typing_extensions import TypedDict

from .connector_errors import ConnectorError
from .connector_schemas import Args

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
HTTP_TIMEOUT_SECONDS = 15
READ_RETRIES = 2
MAX_PARTS = 100
MAX_DEPTH = 16
MAX_BODY_BYTES = 128 * 1024
MAX_HEADER_CHARS = 16 * 1024
MAX_EVIDENCE_CHARS = 2000
HEADERS = (
    "From", "To", "Cc", "Bcc", "Reply-To", "Subject", "Date", "Message-ID",
    "In-Reply-To", "References", "Content-Type", "Content-Disposition",
)
HEADER_NAMES = frozenset(header.lower() for header in HEADERS)
MESSAGE_FIELDS = "id,threadId,historyId,internalDate,labelIds,snippet,sizeEstimate,payload"
ProviderId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
HistoryId = Annotated[str, Field(pattern=r"^[0-9]{1,40}$")]
_PROVIDER_ID = TypeAdapter(ProviderId)
_HISTORY_ID = TypeAdapter(HistoryId)
_OBJECT = TypeAdapter(dict[str, object])
_TOKEN = TypeAdapter(Annotated[str, Field(min_length=1, strict=True)])


class Request(Protocol):
    def execute(self, *, num_retries: int = 0) -> object: ...


class Messages(Protocol):
    def get(self, *, userId: str, id: str, format: str, fields: str,
            metadataHeaders: list[str] | None = None) -> Request: ...

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str | None,
             includeSpamTrash: bool, fields: str) -> Request: ...


class Threads(Protocol):
    def get(self, *, userId: str, id: str, format: str, fields: str) -> Request: ...


class Users(Protocol):
    def messages(self) -> Messages: ...
    def threads(self) -> Threads: ...


class Service(Protocol):
    def users(self) -> Users: ...
    def close(self) -> None: ...


class ServiceBuilder(Protocol):
    def __call__(self, serviceName: str, version: str, *,
                 http: google_auth_httplib2.AuthorizedHttp, cache_discovery: bool,
                 static_discovery: bool, num_retries: int) -> Service: ...


class ErrorResponse(Protocol):
    status: int
    def get(self, key: str, default: str = "") -> str: ...


class Discovery(Protocol):
    build: ServiceBuilder


# Discovery constructs resources dynamically; type only the SDK methods we use.
build = cast(Discovery, discovery).build
ServiceFactory = Callable[[str], Service]


class Header(TypedDict):
    name: str
    value: str


class Body(TypedDict):
    text: str
    format: Literal["plain_text"]
    complete: bool
    truncated: bool
    issues: list[str]
    attachments_skipped: int


Sender = TypedDict("Sender", {"from": str})


class MessageRecord(Sender):
    provider: Literal["gmail"]
    id: str
    threadId: str
    historyId: str
    revision: str
    internalDate: str | None
    labelIds: list[str]
    headers: list[Header]
    headers_incomplete: bool
    snippet: str
    snippet_truncated: bool
    subject: str
    subject_truncated: bool
    from_truncated: bool
    text: str | None
    text_complete: bool
    text_truncated: bool
    content_state: Literal["bounded_body", "headers_and_snippet"]
    body: Body | None
    attachments_read: bool
    attachments_skipped: int | None
    content_complete: bool


class Reference(TypedDict):
    id: str
    threadId: str


class Unread(Reference):
    code: Literal["not_found"]


class Coverage(TypedDict):
    scope: str
    has_more: bool
    page_complete: bool
    partial: bool
    consistency: str


class SearchCoverage(Coverage):
    warning: str


class SearchResult(TypedDict):
    messages: list[MessageRecord]
    unread_messages: list[Unread]
    result_size_estimate: int | None
    next_arguments: dict[str, str | int | bool] | None
    coverage: SearchCoverage


class ThreadResult(TypedDict):
    provider: Literal["gmail"]
    id: str
    historyId: str
    revision: str
    messages: list[MessageRecord]
    unread_messages: list[Unread]
    message_count: int
    offset: int
    next_arguments: dict[str, str | int | None] | None
    coverage: Coverage


class WireMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    id: ProviderId
    threadId: ProviderId
    historyId: HistoryId
    internalDate: Annotated[str, Field(pattern=r"^[0-9]{1,20}$")] | None = None
    labelIds: list[Annotated[str, Field(max_length=256)]] = Field(default_factory=list, max_length=1000)
    snippet: str = ""
    # MIME parts are validated only as visited, preserving traversal limits.
    payload: dict[str, object] = Field(default_factory=dict)


class GmailSearch(Args):
    query: str = Field(default="", max_length=2048, description="Gmail search syntax. Empty lists messages.")
    cursor: str = Field(default="", max_length=4096, description="Opaque next-page token from next_arguments.")
    page_size: int = Field(default=10, ge=1, le=20, strict=True)
    include_spam_trash: bool = Field(default=False, strict=True)


class GmailMessage(Args):
    message_id: ProviderId
    max_body_chars: int = Field(default=12000, ge=1, le=24000, strict=True)


class GmailThread(Args):
    thread_id: ProviderId
    page_size: int = Field(default=5, ge=1, le=10, strict=True)
    offset: int = Field(default=0, ge=0, le=1000000, strict=True)
    history_id: HistoryId | None = None
    max_body_chars: int = Field(default=12000, ge=1, le=24000, strict=True)

    @model_validator(mode="after")
    def continuation_revision(self) -> GmailThread:
        if self.offset and self.history_id is None:
            raise ValueError("Use next_arguments, including history_id, for thread continuation.")
        return self


TOOLS: dict[str, tuple[str, bool, type[GmailSearch | GmailMessage | GmailThread], str]] = {
    "gmail_search": (
        "gmail", False, GmailSearch,
        ("Search/list the connected Gmail account. Returns exact message IDs, history revisions, "
        "selected headers and provider snippets, not full message bodies. Read gmail_message or "
        "gmail_thread before conclusions requiring omitted content. Follow next_arguments; "
        "result_size_estimate is approximate and a page is not a mailbox mirror."),
    ),
    "gmail_message": (
        "gmail", False, GmailMessage,
        ("Read one exact Gmail message with bounded text and MIME parsing. Subject, from and text "
        "are scalar evidence; check text_complete, body.issues and content_complete before "
        "treating it as complete evidence. The nested body can contain more text. Attachments "
        "are never downloaded. internalDate is creation time, not an update revision."),
    ),
    "gmail_thread": (
        "gmail", False, GmailThread,
        ("Read a bounded page of messages from one Gmail thread. Follow next_arguments, which "
        "pins the thread history revision; restart from offset=0 if the thread changed. "
        "Check per-message body completeness and unread_messages. No attachment downloads."),
    ),
}
READS = frozenset(TOOLS)


def _invalid_response() -> ConnectorError:
    return ConnectorError("Gmail returned an invalid or mismatched response.", code="invalid_response")


def _service(access_token: str) -> Service:
    # A transport is private to this worker invocation: httplib2 is not thread-safe.
    # Refresh belongs to the connection owner, never to this token-only adapter.
    credentials = Credentials(token=access_token, scopes=[SCOPE])
    http = httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
    http.follow_redirects = False
    transport = google_auth_httplib2.AuthorizedHttp(
        credentials, http=http,
        refresh_status_codes=(), max_refresh_attempts=0,
    )
    return build(
        "gmail", "v1", http=transport, cache_discovery=False,
        static_discovery=True, num_retries=0,
    )


def _object(value: object) -> dict[str, object]:
    try:
        return _OBJECT.validate_python(value, strict=True)
    except ValidationError:
        raise _invalid_response() from None


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _http_error(exc: HttpError) -> ConnectorError:
    response = cast(ErrorResponse, exc.resp)
    status = response.status
    rate_limited = status == 429
    if status == 403 and len(exc.content) <= 16384:
        try:
            errors = _object(_object(json.loads(exc.content)).get("error")).get("errors", [])
            rate_limited = any(
                _object(item).get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
                for item in errors
            ) if _is_list(errors) else False
        except (ValueError, TypeError, ConnectorError):
            pass
    code, message = {
        400: ("invalid_request", "Gmail rejected the query or page token."),
        401: ("authentication_required", "Reconnect Gmail; its authorization is no longer valid."),
        403: ("access_denied", "Gmail denied access. Check the account's read permission."),
        404: ("not_found", "Gmail could not find the requested message or thread."),
    }.get(status, ("provider_error", "Gmail could not complete the read."))
    if rate_limited:
        code, message = "rate_limited", "Gmail temporarily limited reads. Try again later."
    value = response.get("retry-after", "")
    retry_after = int(value) if len(value) <= 5 and value.isascii() and value.isdigit() else None
    return ConnectorError(
        message, code=code, status_code=status, retryable=rate_limited or status >= 500,
        retry_after=min(retry_after, 86400) if retry_after is not None else None,
    )


def _execute(request: Request) -> dict[str, object]:
    try:
        result = request.execute(num_retries=READ_RETRIES)
    except HttpError as exc:
        raise _http_error(exc) from None
    except (OSError, httplib2.HttpLib2Error, GoogleAuthError):
        raise ConnectorError("Gmail could not be reached.", code="provider_unavailable", retryable=True) from None
    except (ValueError, TypeError):
        raise _invalid_response() from None
    return _object(result)


def _identity(value: object, *, history: bool = False) -> str:
    try:
        return (_HISTORY_ID if history else _PROVIDER_ID).validate_python(value, strict=True)
    except ValidationError:
        raise _invalid_response() from None


def _headers(payload: Mapping[str, object]) -> tuple[list[Header], bool]:
    values = payload.get("headers", [])
    if not _is_list(values):
        raise _invalid_response()
    result: list[Header] = []
    remaining, incomplete = MAX_HEADER_CHARS, len(values) > 100
    for raw_header in values[:100]:
        header = _object(raw_header)
        name, value = header.get("name"), header.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise _invalid_response()
        if name.lower() not in HEADER_NAMES:
            continue
        if len(value) > remaining:
            incomplete = True
        value = value[:remaining]
        remaining -= len(value)
        try:
            value = str(make_header(decode_header(value)))
        except (LookupError, UnicodeError, ValueError):
            incomplete = True
        result.append({"name": name, "value": value})
    return result, incomplete


def _html_text(text: str) -> str:
    soup = BeautifulSoup(text, "html5lib")
    for hidden in soup.find_all(["head", "script", "style", "template"]):
        hidden.decompose()
    for block in soup.find_all(["p", "div", "br", "li", "tr", "h1", "h2", "h3", "blockquote", "pre", "hr"]):
        block.insert_before("\n")
        block.insert_after("\n")
    return soup.get_text()


def _alternative(parts: list[object]) -> object:
    """Prefer plain/HTML bodies; retain containers for bounded traversal."""
    candidates: list[tuple[int, object]] = []
    for value in parts[:MAX_PARTS]:
        try:
            part = _object(value)
        except ConnectorError:
            continue
        mime = part.get("mimeType")
        if part.get("filename") or not isinstance(mime, str):
            continue
        mime = mime.lower()
        headers, _ = _headers(part)
        description = Message()
        for header in headers:
            if header["name"].lower() == "content-disposition":
                description[header["name"]] = header["value"]
        if description.get_content_disposition() == "attachment":
            continue
        if mime in {"text/plain", "text/html"}:
            candidates.append((0 if mime == "text/plain" else 1, part))
        elif mime.startswith("multipart/"):
            candidates.append((2, part))
    return min(candidates, key=lambda item: item[0])[1] if candidates else parts[-1]


def _body(payload: dict[str, object], max_chars: int) -> Body:
    stack: list[tuple[object, int]] = [(payload, 0)]
    chunks: list[str] = []
    issues: set[str] = set()
    seen, remaining, attachments = 0, MAX_BODY_BYTES, 0
    while stack:
        raw_part, depth = stack.pop()
        seen += 1
        try:
            part = _object(raw_part)
        except ConnectorError:
            issues.add("malformed_mime")
            continue
        mime = part.get("mimeType", "")
        if not isinstance(mime, str):
            issues.add("malformed_mime")
            continue
        mime = mime.lower()
        headers, headers_incomplete = _headers(part)
        if headers_incomplete:
            issues.add("incomplete_mime_headers")
        description = Message()
        for header in headers:
            if header["name"].lower() in {"content-type", "content-disposition"}:
                description[header["name"]] = header["value"]
        if part.get("filename") or description.get_content_disposition() == "attachment":
            attachments += 1
            continue
        if mime.startswith("multipart/"):
            parts = part.get("parts", [])
            if not _is_list(parts):
                issues.add("malformed_mime")
                continue
            if depth >= MAX_DEPTH:
                issues.add("mime_depth_limit")
                continue
            available = MAX_PARTS - seen - len(stack)
            if len(parts) > available:
                issues.add("mime_part_limit")
            parts = parts[:max(available, 0)]
            if mime == "multipart/alternative" and parts:
                # Prefer the plain representation without discarding mixed siblings.
                parts = [_alternative(parts)]
            stack.extend((child, depth + 1) for child in reversed(parts))
            continue
        if mime not in {"text/plain", "text/html"}:
            attachments += 1
            issues.add("unsupported_mime_part")
            continue
        try:
            body = _object(part.get("body"))
        except ConnectorError:
            issues.add("malformed_mime")
            continue
        if body.get("attachmentId"):
            attachments += 1
            issues.add("text_requires_attachment_download")
            continue
        encoded = body.get("data", "")
        if not isinstance(encoded, str):
            issues.add("invalid_body_encoding")
            continue
        if not encoded and body.get("size") != 0:
            issues.add("missing_body_data")
            continue
        if not remaining:
            issues.add("body_byte_limit")
            continue
        limit = ((remaining + 2) // 3) * 4
        if len(encoded) > limit:
            encoded = encoded[:limit]
            issues.add("body_byte_limit")
        try:
            decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            issues.add("invalid_body_encoding")
            continue
        if len(decoded) > remaining:
            issues.add("body_byte_limit")
        decoded = decoded[:remaining]
        remaining -= len(decoded)
        charset = description.get_content_charset() or "utf-8"
        try:
            text = decoded.decode(charset)
        except (LookupError, UnicodeError):
            text = decoded.decode("utf-8", errors="replace")
            issues.add("lossy_charset_decoding")
        if mime == "text/html":
            text = _html_text(text)
        chunks.append(text.strip())
    text = "\n\n".join(chunk for chunk in chunks if chunk)
    if len(text) > max_chars:
        issues.add("body_character_limit")
    return {
        "text": text[:max_chars], "format": "plain_text", "complete": not issues,
        "truncated": bool(issues & {"body_byte_limit", "body_character_limit", "mime_depth_limit", "mime_part_limit"}),
        "issues": sorted(issues), "attachments_skipped": attachments,
    }


def _message(data: object, message_id: str, *, thread_id: str | None = None,
             max_chars: int | None = None) -> MessageRecord:
    try:
        source = WireMessage.model_validate(data)
    except ValidationError:
        raise _invalid_response() from None
    identity, thread, revision = source.id, source.threadId, source.historyId
    if identity != message_id or (thread_id is not None and thread != thread_id):
        raise _invalid_response()
    payload = source.payload
    headers, headers_incomplete = _headers(payload)
    body = _body(payload, max_chars) if max_chars is not None else None
    snippet = source.snippet
    subject = "\n".join(header["value"] for header in headers if header["name"].lower() == "subject")
    sender = "\n".join(header["value"] for header in headers if header["name"].lower() == "from")
    body_text = body["text"] if body is not None else None
    text_truncated = bool(body is not None and (
        body["truncated"] or len(body_text or "") > MAX_EVIDENCE_CHARS
    ))
    text_complete = bool(body is not None and body["complete"] and not text_truncated)
    subject_truncated, sender_truncated = len(subject) > MAX_EVIDENCE_CHARS, len(sender) > MAX_EVIDENCE_CHARS
    return {
        "provider": "gmail", "id": identity, "threadId": thread,
        "historyId": revision, "revision": revision, "internalDate": source.internalDate,
        "labelIds": source.labelIds, "headers": headers, "headers_incomplete": headers_incomplete,
        "snippet": snippet[:4096], "snippet_truncated": len(snippet) > 4096,
        # Discovery retains scalar evidence alongside its exact identity/revision.
        # Keep the larger nested body for ordinary reads without hiding omissions.
        "subject": subject[:MAX_EVIDENCE_CHARS], "from": sender[:MAX_EVIDENCE_CHARS],
        "subject_truncated": subject_truncated, "from_truncated": sender_truncated,
        "text": body_text[:MAX_EVIDENCE_CHARS] if body_text is not None else None,
        "text_complete": text_complete, "text_truncated": text_truncated,
        "content_state": "bounded_body" if body is not None else "headers_and_snippet",
        "body": body, "attachments_read": False,
        "attachments_skipped": body["attachments_skipped"] if body is not None else None,
        "content_complete": bool(text_complete and body is not None and not body["attachments_skipped"]
                                 and not headers_incomplete and not subject_truncated and not sender_truncated),
    }


def _messages(service: Service, references: list[object], *,
              max_chars: int | None = None) -> tuple[list[MessageRecord], list[Unread]]:
    messages: list[MessageRecord] = []
    unread: list[Unread] = []
    for raw_reference in references:
        reference = _object(raw_reference)
        identity = _identity(reference.get("id"))
        thread = _identity(reference.get("threadId"))
        try:
            data = _execute(service.users().messages().get(
                userId="me", id=identity, format="full" if max_chars is not None else "metadata",
                fields=MESSAGE_FIELDS if max_chars is not None else MESSAGE_FIELDS.replace("payload", "payload(headers)"),
                metadataHeaders=list(HEADERS) if max_chars is None else None,
            ))
        except ConnectorError as exc:
            if exc.code != "not_found":
                raise
            unread.append({"id": identity, "threadId": thread, "code": "not_found"})
            continue
        messages.append(_message(data, identity, thread_id=thread, max_chars=max_chars))
    return messages, unread


def _search(service: Service, args: GmailSearch) -> SearchResult:
    data = _execute(service.users().messages().list(
        userId="me", q=args.query, maxResults=args.page_size,
        pageToken=args.cursor or None, includeSpamTrash=args.include_spam_trash,
        fields="messages(id,threadId),nextPageToken,resultSizeEstimate",
    ))
    references = data.get("messages", [])
    cursor = data.get("nextPageToken", "")
    estimate = data.get("resultSizeEstimate")
    if not _is_list(references) or len(references) > args.page_size or not isinstance(cursor, str) or len(cursor) > 4096:
        raise _invalid_response()
    if estimate is not None and (type(estimate) is not int or estimate < 0):
        raise _invalid_response()
    messages, unread = _messages(service, references)
    next_args: dict[str, str | int | bool] | None = {
        "query": args.query, "cursor": cursor, "page_size": args.page_size,
        "include_spam_trash": args.include_spam_trash,
    } if cursor and cursor != args.cursor else None
    return {
        "messages": messages, "unread_messages": unread,
        "result_size_estimate": estimate, "next_arguments": next_args,
        "coverage": {
            "scope": "matching_message_headers_and_snippets", "has_more": bool(cursor),
            "page_complete": not unread, "partial": bool(args.cursor or cursor or unread),
            "warning": "Gmail repeated a page token; coverage is incomplete." if cursor and not next_args else "",
            "consistency": "Live Gmail pages; concurrent changes can affect enumeration.",
        },
    }


def _thread(service: Service, args: GmailThread) -> ThreadResult:
    # threads.get has no provider pagination. Fetch only compact identities first,
    # then read a bounded slice; never download every message body in a thread.
    data = _execute(service.users().threads().get(
        userId="me", id=args.thread_id, format="minimal",
        fields="id,historyId,messages(id)",
    ))
    identity = _identity(data.get("id"))
    history = _identity(data.get("historyId"), history=True)
    references = data.get("messages")
    if identity != args.thread_id or not _is_list(references):
        raise _invalid_response()
    if args.history_id is not None and history != args.history_id:
        raise ConnectorError("The Gmail thread changed. Read it again from offset=0 without history_id.",
                             code="thread_changed")
    offset = args.offset
    page = references[offset:offset + args.page_size]
    # Minimal thread reads only promise message IDs. The subsequent full read
    # verifies each message's threadId against this independently returned thread.
    page_references: list[object] = [{"id": _object(item).get("id"), "threadId": identity} for item in page]
    messages, unread = _messages(service, page_references, max_chars=args.max_body_chars)
    end = offset + len(page)
    has_more = end < len(references)
    next_args: dict[str, str | int | None] | None = {
        "thread_id": args.thread_id, "page_size": args.page_size,
        "offset": end, "history_id": history, "max_body_chars": args.max_body_chars,
    } if has_more else None
    return {
        "provider": "gmail", "id": identity, "historyId": history, "revision": history,
        "messages": messages, "unread_messages": unread, "message_count": len(references),
        "offset": offset, "next_arguments": next_args,
        "coverage": {
            "scope": "thread_messages", "has_more": has_more, "page_complete": not unread,
            "partial": bool(offset or has_more or unread or any(not item["content_complete"] for item in messages)),
            "consistency": "Live reads; history_id detects changes between pages, not an atomic snapshot.",
        },
    }


Result = MessageRecord | SearchResult | ThreadResult


def _call(args: GmailSearch | GmailMessage | GmailThread, access_token: str,
          service_factory: ServiceFactory) -> Result:
    service = service_factory(access_token)
    try:
        if isinstance(args, GmailSearch):
            return _search(service, args)
        if isinstance(args, GmailThread):
            return _thread(service, args)
        data = _execute(service.users().messages().get(
            userId="me", id=args.message_id, format="full", fields=MESSAGE_FIELDS,
        ))
        return _message(data, args.message_id, max_chars=args.max_body_chars)
    finally:
        service.close()


async def call(name: str, arguments: Mapping[str, object], access_token: str, *,
               service_factory: ServiceFactory = _service) -> Result:
    if name not in READS:
        raise ConnectorError("Unsupported Gmail read operation.", code="unsupported_operation", dispatched=False)
    try:
        args = TOOLS[name][2].model_validate(arguments)
    except ValidationError:
        raise ConnectorError("Invalid Gmail read arguments.", code="invalid_arguments", dispatched=False) from None
    try:
        token = _TOKEN.validate_python(access_token)
    except ValidationError:
        raise ConnectorError("Connect Gmail before reading mail.", code="authentication_required", dispatched=False) from None
    try:
        return await asyncio.to_thread(_call, args, token, service_factory)
    except ConnectorError:
        raise
    except Exception:  # noqa: BLE001 — sanitize failures at the credential-bearing SDK boundary.
        # SDK/transport failures must not expose tokens, request URLs or mail bodies.
        raise ConnectorError("Gmail could not complete the read.", code="provider_error") from None
