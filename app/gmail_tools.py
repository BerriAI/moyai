"""Read-only Gmail operations; OAuth and account selection belong to the caller."""
import asyncio
import base64
import binascii
import json
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from typing import Annotated

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .connector_errors import ConnectorError
from .connector_schemas import Args

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
HTTP_TIMEOUT_SECONDS = 15
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
    def continuation_revision(self):
        if self.offset and self.history_id is None:
            raise ValueError("Use next_arguments, including history_id, for thread continuation.")
        return self


TOOLS = {
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


def _invalid_response():
    return ConnectorError("Gmail returned an invalid or mismatched response.", code="invalid_response")


def _service(access_token):
    # A transport is private to this worker invocation: httplib2 is not thread-safe.
    # Refresh belongs to the connection owner, never to this token-only adapter.
    credentials = Credentials(token=access_token, scopes=[SCOPE])
    transport = google_auth_httplib2.AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS),
        refresh_status_codes=(), max_refresh_attempts=0,
    )
    return build(
        "gmail", "v1", http=transport, cache_discovery=False,
        static_discovery=True, num_retries=0,
    )


def _http_error(exc):
    status = exc.resp.status
    rate_limited = status == 429
    if status == 403 and len(exc.content) <= 16384:
        try:
            errors = json.loads(exc.content).get("error", {}).get("errors", [])
            rate_limited = any(
                item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
                for item in errors if isinstance(item, dict)
            )
        except (ValueError, TypeError, AttributeError):
            pass
    code, message = {
        400: ("invalid_request", "Gmail rejected the query or page token."),
        401: ("authentication_required", "Reconnect Gmail; its authorization is no longer valid."),
        403: ("access_denied", "Gmail denied access. Check the account's read permission."),
        404: ("not_found", "Gmail could not find the requested message or thread."),
    }.get(status, ("provider_error", "Gmail could not complete the read."))
    if rate_limited:
        code, message = "rate_limited", "Gmail temporarily limited reads. Try again later."
    retry_after = exc.resp.get("retry-after", "")
    retry_after = int(retry_after) if str(retry_after).isascii() and str(retry_after).isdigit() else None
    return ConnectorError(
        message, code=code, status_code=status, retryable=rate_limited or status >= 500,
        retry_after=min(retry_after, 86400) if retry_after is not None else None,
    )


def _execute(request):
    try:
        result = request.execute(num_retries=0)
    except HttpError as exc:
        raise _http_error(exc) from None
    except (OSError, httplib2.HttpLib2Error, GoogleAuthError):
        raise ConnectorError("Gmail could not be reached.", code="provider_unavailable", retryable=True) from None
    except (ValueError, TypeError):
        raise _invalid_response() from None
    if not isinstance(result, dict):
        raise _invalid_response()
    return result


def _identity(value, *, history=False):
    try:
        return (_HISTORY_ID if history else _PROVIDER_ID).validate_python(value)
    except ValidationError:
        raise _invalid_response() from None


def _headers(payload):
    values = payload.get("headers", [])
    if not isinstance(values, list):
        raise _invalid_response()
    result, remaining, incomplete = [], MAX_HEADER_CHARS, len(values) > 100
    for header in values[:100]:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("value"), str):
            raise _invalid_response()
        name, value = header["name"], header["value"]
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


class _HTMLText(HTMLParser):
    """Extract text without following URLs or returning executable markup."""
    HIDDEN = frozenset({"script", "style", "head", "template"})
    BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "blockquote", "pre", "hr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.BLOCKS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.BLOCKS:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _body(payload, max_chars):
    stack = [(payload, 0)]
    chunks, issues, seen, remaining, attachments = [], set(), 0, MAX_BODY_BYTES, 0
    while stack:
        part, depth = stack.pop()
        seen += 1
        if not isinstance(part, dict):
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
            if not isinstance(parts, list):
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
                plain = next((item for item in parts if isinstance(item, dict)
                              and item.get("mimeType") == "text/plain"
                              and not item.get("filename")), None)
                parts = [plain if plain is not None else parts[-1]]
            stack.extend((child, depth + 1) for child in reversed(parts))
            continue
        if mime not in {"text/plain", "text/html"}:
            attachments += 1
            issues.add("unsupported_mime_part")
            continue
        body = part.get("body")
        if not isinstance(body, dict):
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
            parser = _HTMLText()
            parser.feed(text)
            parser.close()
            text = "".join(parser.text)
        chunks.append(text.strip())
    text = "\n\n".join(chunk for chunk in chunks if chunk)
    if len(text) > max_chars:
        issues.add("body_character_limit")
    return {
        "text": text[:max_chars], "format": "plain_text", "complete": not issues,
        "truncated": bool(issues & {"body_byte_limit", "body_character_limit", "mime_depth_limit", "mime_part_limit"}),
        "issues": sorted(issues), "attachments_skipped": attachments,
    }


def _message(data, message_id, *, thread_id=None, max_chars=None):
    identity = _identity(data.get("id"))
    thread = _identity(data.get("threadId"))
    revision = _identity(data.get("historyId"), history=True)
    if identity != message_id or (thread_id is not None and thread != thread_id):
        raise _invalid_response()
    payload = data.get("payload", {})
    if not isinstance(payload, dict) or not isinstance(data.get("snippet", ""), str):
        raise _invalid_response()
    headers, headers_incomplete = _headers(payload)
    labels = data.get("labelIds", [])
    if not isinstance(labels, list) or len(labels) > 1000 or not all(isinstance(label, str) and len(label) <= 256 for label in labels):
        raise _invalid_response()
    internal_date = data.get("internalDate")
    if internal_date is not None and (not isinstance(internal_date, str) or not internal_date.isascii() or not internal_date.isdigit() or len(internal_date) > 20):
        raise _invalid_response()
    body = _body(payload, max_chars) if max_chars is not None else None
    snippet = data.get("snippet", "")
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
        "historyId": revision, "revision": revision, "internalDate": internal_date,
        "labelIds": labels, "headers": headers, "headers_incomplete": headers_incomplete,
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


def _messages(service, references, *, max_chars=None):
    messages, unread = [], []
    for reference in references:
        if not isinstance(reference, dict):
            raise _invalid_response()
        identity = _identity(reference.get("id"))
        thread = _identity(reference.get("threadId"))
        params: dict[str, str | list[str]] = {
            "userId": "me", "id": identity, "format": "full" if max_chars is not None else "metadata",
            "fields": MESSAGE_FIELDS if max_chars is not None else MESSAGE_FIELDS.replace("payload", "payload(headers)"),
        }
        if max_chars is None:
            params["metadataHeaders"] = list(HEADERS)
        try:
            data = _execute(service.users().messages().get(**params))
        except ConnectorError as exc:
            if exc.code != "not_found":
                raise
            unread.append({"id": identity, "threadId": thread, "code": "not_found"})
            continue
        messages.append(_message(data, identity, thread_id=thread, max_chars=max_chars))
    return messages, unread


def _search(service, args):
    data = _execute(service.users().messages().list(
        userId="me", q=args["query"], maxResults=args["page_size"],
        pageToken=args["cursor"] or None, includeSpamTrash=args["include_spam_trash"],
        fields="messages(id,threadId),nextPageToken,resultSizeEstimate",
    ))
    references = data.get("messages", [])
    cursor = data.get("nextPageToken", "")
    estimate = data.get("resultSizeEstimate")
    if not isinstance(references, list) or len(references) > args["page_size"] or not isinstance(cursor, str) or len(cursor) > 4096:
        raise _invalid_response()
    if estimate is not None and (type(estimate) is not int or estimate < 0):
        raise _invalid_response()
    messages, unread = _messages(service, references)
    next_args = {**args, "cursor": cursor} if cursor and cursor != args["cursor"] else None
    return {
        "messages": messages, "unread_messages": unread,
        "result_size_estimate": estimate, "next_arguments": next_args,
        "coverage": {
            "scope": "matching_message_headers_and_snippets", "has_more": bool(cursor),
            "page_complete": not unread, "partial": bool(args["cursor"] or cursor or unread),
            "warning": "Gmail repeated a page token; coverage is incomplete." if cursor and not next_args else "",
            "consistency": "Live Gmail pages; concurrent changes can affect enumeration.",
        },
    }


def _thread(service, args):
    # threads.get has no provider pagination. Fetch only compact identities first,
    # then read a bounded slice; never download every message body in a thread.
    data = _execute(service.users().threads().get(
        userId="me", id=args["thread_id"], format="minimal",
        fields="id,historyId,messages(id)",
    ))
    identity = _identity(data.get("id"))
    history = _identity(data.get("historyId"), history=True)
    if identity != args["thread_id"] or not isinstance(data.get("messages"), list):
        raise _invalid_response()
    if args["history_id"] is not None and history != args["history_id"]:
        raise ConnectorError("The Gmail thread changed. Read it again from offset=0 without history_id.",
                             code="thread_changed")
    references = data["messages"]
    offset = args["offset"]
    page = references[offset:offset + args["page_size"]]
    if any(not isinstance(item, dict) for item in page):
        raise _invalid_response()
    # Minimal thread reads only promise message IDs. The subsequent full read
    # verifies each message's threadId against this independently returned thread.
    page_references = [{"id": item.get("id"), "threadId": identity} for item in page]
    messages, unread = _messages(service, page_references, max_chars=args["max_body_chars"])
    end = offset + len(page)
    has_more = end < len(references)
    next_args = {**args, "offset": end, "history_id": history} if has_more else None
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


def _call(name, args, access_token):
    service = _service(access_token)
    try:
        if name == "gmail_search":
            return _search(service, args)
        if name == "gmail_thread":
            return _thread(service, args)
        data = _execute(service.users().messages().get(
            userId="me", id=args["message_id"], format="full", fields=MESSAGE_FIELDS,
        ))
        return _message(data, args["message_id"], max_chars=args["max_body_chars"])
    finally:
        service.close()


async def call(name: str, arguments: dict, access_token: str) -> dict:
    if name not in READS:
        raise ConnectorError("Unsupported Gmail read operation.", code="unsupported_operation", dispatched=False)
    try:
        args = TOOLS[name][2].model_validate(arguments).model_dump()
    except ValidationError:
        raise ConnectorError("Invalid Gmail read arguments.", code="invalid_arguments", dispatched=False) from None
    if not isinstance(access_token, str) or not access_token:
        raise ConnectorError("Connect Gmail before reading mail.", code="authentication_required", dispatched=False)
    try:
        return await asyncio.to_thread(_call, name, args, access_token)
    except ConnectorError:
        raise
    except Exception:  # noqa: BLE001 — sanitize failures at the credential-bearing SDK boundary.
        # SDK/transport failures must not expose tokens, request URLs or mail bodies.
        raise ConnectorError("Gmail could not complete the read.", code="provider_error") from None
