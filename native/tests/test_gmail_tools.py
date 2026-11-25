"""Provider fixtures exercise the official SDK; actual-app proof is separate."""
import base64
import json
import threading
from urllib.parse import parse_qs, urlsplit

import httplib2
import pytest
from app import gmail_tools as gmail
from app.connector_errors import ConnectorError
from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest
from pydantic_ai import Conversation, ConversationTypeAdapter, RunContext, RunUsage
from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from moyai.contracts import Grant, RunScope
from moyai.dot_contracts import DotEvidence
from moyai.dot_discovery_agent import DiscoveryBounds
from moyai.dot_evidence import validate_evidence
from moyai.dot_sources import provider_complete, provider_partial


def part(text, mime="text/plain", *, charset="utf-8"):
    data = text.encode(charset) if isinstance(text, str) else text
    return {
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
        "body": {"size": len(data), "data": base64.urlsafe_b64encode(data).decode().rstrip("=")},
    }


def message(identity="abc123", *, thread="def456", payload=None):
    return {
        "id": identity, "threadId": thread, "historyId": "9007199254740993123",
        "internalDate": "1795595162083", "labelIds": ["INBOX", "UNREAD"],
        "snippet": "The meeting was moved to Thursday & needs a reply.",
        "payload": payload if payload is not None else part("The meeting is Thursday at 10am."),
    }


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False
        self.sleeps = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        parsed = urlsplit(uri)
        self.requests.append({
            "method": method, "host": parsed.hostname, "path": parsed.path,
            "query": parse_qs(parsed.query), "body": body, "thread": threading.get_ident(),
        })
        assert self.responses, "Unexpected provider request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response if isinstance(response, tuple) else (200, response)
        return httplib2.Response({"status": str(status), "retry-after": "17"}), json.dumps(payload).encode()

    def close(self):
        self.closed = True


@pytest.fixture
def provider():
    class Provider:
        def __call__(self, *responses):
            self.transport = Transport(responses)
            return self.transport

        def service(self, token):
            transport = self.transport

            class Request(HttpRequest):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self._sleep = transport.sleeps.append
                    self._rand = lambda: 0.5

            return build(
                "gmail", "v1", http=transport, static_discovery=True,
                cache_discovery=False, num_retries=0, requestBuilder=Request,
            )

        async def call(self, name, arguments, token):
            return await gmail.call(name, arguments, token, service_factory=self.service)

    return Provider()


async def test_search_hydrates_metadata_with_exact_revisions_and_explicit_pagination(provider):
    record = message(payload={"headers": [
        {"name": "Subject", "value": "=?UTF-8?B?TWVldGluZyDimJU=?="},
        {"name": "From", "value": "Owner <owner@example.test>"},
    ]})
    transport = provider(
        {"messages": [{"id": record["id"], "threadId": record["threadId"]}],
         "nextPageToken": "opaque-token", "resultSizeEstimate": 200},
        record,
        {"resultSizeEstimate": 0},
    )
    result = await provider.call("gmail_search", {"query": "is:unread", "page_size": 1}, "private-token")
    item = result["messages"][0]
    assert item["revision"] == item["historyId"] == "9007199254740993123"
    assert item["internalDate"] == "1795595162083"
    assert "updated_at" not in item
    assert item["snippet"] == record["snippet"]
    assert item["headers"][0]["value"] == "Meeting ☕"
    assert item["content_state"] == "headers_and_snippet"
    assert item["body"] is None and item["content_complete"] is False
    assert "url" not in item  # No unstable account-index link.
    assert result["coverage"]["partial"] is True
    assert result["next_arguments"]["query"] == "is:unread"
    assert result["next_arguments"]["cursor"] == "opaque-token"
    last = await provider.call("gmail_search", result["next_arguments"], "private-token")
    assert last["messages"] == [] and last["next_arguments"] is None
    assert last["coverage"]["has_more"] is False
    assert transport.requests[0]["query"]["maxResults"] == ["1"]
    assert transport.requests[1]["query"]["format"] == ["metadata"]
    assert transport.requests[1]["query"]["metadataHeaders"] == list(gmail.HEADERS)
    assert transport.requests[2]["query"]["pageToken"] == ["opaque-token"]
    assert all(request["method"] == "GET" and request["body"] is None for request in transport.requests)
    assert all(request["path"].startswith("/gmail/v1/users/me/") for request in transport.requests)
    assert all(request["host"] == "gmail.googleapis.com" for request in transport.requests)
    assert all(request["thread"] != threading.get_ident() for request in transport.requests)
    assert transport.closed


async def test_search_missing_message_is_partial_and_not_content_evidence(provider):
    transport = provider(
        {"messages": [{"id": "abc", "threadId": "def"}]},
        (404, {"error": {"message": "private email details"}}),
    )
    result = await provider.call("gmail_search", {}, "token")
    assert result["messages"] == []
    assert result["unread_messages"] == [{"id": "abc", "threadId": "def", "code": "not_found"}]
    assert result["coverage"]["page_complete"] is False
    assert result["coverage"]["partial"] is True
    assert "private email" not in json.dumps(result)
    assert len(transport.requests) == 2


async def test_repeated_page_token_does_not_create_an_endless_continuation(provider):
    provider({"nextPageToken": "same"})
    result = await provider.call("gmail_search", {"cursor": "same"}, "token")
    assert result["next_arguments"] is None
    assert result["coverage"]["has_more"] is True
    assert result["coverage"]["partial"] is True
    assert result["coverage"]["warning"]


async def test_thread_reads_bounded_page_and_checks_revision_on_continuation(provider):
    metadata = {"id": "def456", "historyId": "888", "messages": [
        {"id": identity} for identity in ("one", "two", "three")
    ]}
    transport = provider(metadata, message("one"), message("two"), metadata, message("three"))
    result = await provider.call("gmail_thread", {"thread_id": "def456", "page_size": 2}, "token")
    assert result["message_count"] == 3
    assert [item["id"] for item in result["messages"]] == ["one", "two"]
    assert result["historyId"] == result["revision"] == "888"
    assert result["coverage"]["has_more"] is True
    assert result["next_arguments"]["history_id"] == "888"
    assert result["next_arguments"]["offset"] == 2
    last = await provider.call("gmail_thread", result["next_arguments"], "token")
    assert [item["id"] for item in last["messages"]] == ["three"]
    assert last["next_arguments"] is None
    assert last["coverage"]["partial"] is True  # This call did not read earlier pages.
    assert transport.requests[0]["query"]["format"] == ["minimal"]
    assert transport.requests[0]["query"]["fields"] == ["id,historyId,messages(id)"]
    assert len(transport.requests) == 5


async def test_thread_change_does_not_silently_skip_or_duplicate_messages(provider):
    transport = provider({"id": "def456", "historyId": "889", "messages": []})
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_thread", {
            "thread_id": "def456", "offset": 2, "history_id": "888",
        }, "token")
    assert caught.value.code == "thread_changed"
    assert len(transport.requests) == 1


async def test_thread_read_rejects_a_message_from_a_different_thread(provider):
    provider(
        {"id": "def456", "historyId": "888", "messages": [{"id": "abc123"}]},
        message(thread="wrong-thread"),
    )
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_thread", {"thread_id": "def456"}, "token")
    assert caught.value.code == "invalid_response"


async def test_mime_alternative_prefers_plain_but_retains_other_mixed_content(provider):
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            part("Plain original"), part("<p>Duplicated HTML alternative</p>", "text/html"),
        ]},
        part("<p>A different &amp; useful section.</p>", "text/html"),
        {**part("Secret attachment content"), "filename": "notes.txt"},
    ]}
    transport = provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == "Plain original\n\nA different & useful section."
    assert result["body"]["complete"] is True
    assert result["body"]["attachments_skipped"] == 1
    assert result["content_complete"] is False
    assert len(transport.requests) == 1


async def test_html_is_text_only_and_cannot_fetch_resources(provider):
    payload = part(
        '<head><style>private style</style></head><script>sendSecret()</script>'
        '<p>Reply &amp; confirm</p><img src="https://attacker.test/beacon">'
        '<a href="https://attacker.test/fetch">Useful link text</a>', "text/html",
    )
    transport = provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == "Reply & confirm\nUseful link text"
    assert result["body"]["complete"]
    assert len(transport.requests) == 1


async def test_html_optional_head_end_tag_retains_the_body_and_evidence(provider):
    provider(message(payload=part(
        "<html><head><title>Private title</title><body>"
        "<p>Please reply today.</p><script>hidden()</script></body></html>", "text/html",
    )))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == "Please reply today."
    assert result["text"] == "Please reply today."
    assert result["text_complete"] and result["content_complete"]
    bounded, conversation, grant = await discovery_result(result)
    assert not bounded["limited"]
    assert validate_evidence([DotEvidence(
        connection_id=grant.id, source=result["id"], source_version=result["revision"],
        quote="Please reply today.",
    )], conversation, [grant])


@pytest.mark.parametrize("alternative", [
    part("BEGIN:VCALENDAR", "text/calendar"),
    {**part("Do not read this attachment"), "headers": [
        {"name": "Content-Disposition", "value": "attachment"},
    ]},
])
async def test_alternative_selects_readable_html_before_unsupported_or_attachment_part(provider, alternative):
    provider(message(payload={"mimeType": "multipart/alternative", "parts": [
        part("<p>Meeting moved to Friday.</p>", "text/html"), alternative,
    ]}))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == "Meeting moved to Friday."
    assert result["body"]["complete"] and result["content_complete"]


async def test_nested_alternative_keeps_related_body_and_mixed_siblings(provider):
    provider(message(payload={"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "multipart/related", "parts": [part("<p>Meeting.</p>", "text/html")]},
            part("BEGIN:VCALENDAR", "text/calendar"),
        ]},
        part("Please confirm."),
    ]}))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == "Meeting.\n\nPlease confirm."
    assert result["content_complete"]


@pytest.mark.parametrize(("payload", "expected", "issue"), [
    (part("Réunion", charset="iso-8859-1"), "Réunion", None),
    (part(b"\xff", charset="utf-8"), "\ufffd", "lossy_charset_decoding"),
    ({"mimeType": "text/plain", "body": {"data": "!!!"}}, "", "invalid_body_encoding"),
    ({"mimeType": "text/plain"}, "", "malformed_mime"),
    ({"mimeType": "text/plain", "body": {}}, "", "missing_body_data"),
    ({"mimeType": "text/plain", "body": {"size": 5}}, "", "missing_body_data"),
    ({"mimeType": "text/plain", "body": {"size": 0}}, "", None),
    ({"mimeType": "text/plain", "body": {"attachmentId": "do-not-fetch", "size": 50}},
     "", "text_requires_attachment_download"),
    ({"mimeType": "application/octet-stream", "body": {"data": "!!!!"}},
     "", "unsupported_mime_part"),
])
async def test_mime_encoding_and_missing_content_are_honest(provider, payload, expected, issue):
    transport = provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["text"] == expected
    assert result["body"]["complete"] is (issue is None)
    if issue:
        assert issue in result["body"]["issues"]
        assert not result["content_complete"]
    assert len(transport.requests) == 1


async def test_decoding_and_output_budgets_are_explicit(provider):
    provider(message(payload=part("x" * (gmail.MAX_BODY_BYTES + 500))))
    result = await provider.call("gmail_message", {"message_id": "abc123", "max_body_chars": 30}, "token")
    assert result["body"]["text"] == "x" * 30
    assert result["body"]["truncated"] is True
    assert set(result["body"]["issues"]) == {"body_byte_limit", "body_character_limit"}
    assert not result["content_complete"]


@pytest.mark.parametrize("bound", ["depth", "parts"])
async def test_mime_traversal_is_bounded(provider, bound):
    payload = part("Deep content")
    if bound == "depth":
        for _ in range(gmail.MAX_DEPTH + 5):
            payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    else:
        payload = {"mimeType": "multipart/mixed", "parts": [payload] * (gmail.MAX_PARTS + 5)}
    provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["truncated"]
    assert f"mime_{'depth' if bound == 'depth' else 'part'}_limit" in result["body"]["issues"]
    assert not result["content_complete"]


async def test_header_truncation_does_not_appear_as_complete_evidence(provider):
    payload = part("A body")
    payload["headers"].append({"name": "Subject", "value": "a" * (gmail.MAX_HEADER_CHARS + 1)})
    provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["headers_incomplete"] and not result["content_complete"]
    assert sum(len(header["value"]) for header in result["headers"]) <= gmail.MAX_HEADER_CHARS


@pytest.mark.parametrize(("name", "args"), [
    ("gmail_send", {"message_id": "abc123"}),
    ("gmail_message", {"message_id": "../another-user/messages"}),
    ("gmail_message", {"message_id": "abc123", "userId": "other@example.test"}),
    ("gmail_message", {"message_id": "abc123", "url": "https://attacker.test"}),
    ("gmail_message", {"message_id": "abc123", "max_body_chars": 999999}),
    ("gmail_search", {"page_size": 500}),
    ("gmail_search", {"page_size": True}),
    ("gmail_thread", {"thread_id": "def456", "offset": 10}),
])
async def test_read_only_boundary_rejects_unknown_or_unbounded_arguments_before_io(provider, name, args):
    transport = provider()
    with pytest.raises(ConnectorError) as caught:
        await provider.call(name, args, "token")
    assert caught.value.dispatched is False
    assert not transport.requests
    assert gmail.READS == frozenset(gmail.TOOLS)
    assert all(spec[0] == "gmail" and spec[1] is False for spec in gmail.TOOLS.values())


@pytest.mark.parametrize(("field", "value"), [
    ("id", "different-message"),
    ("historyId", None),
    ("historyId", 9007199254740993123),
    ("internalDate", "not-a-date"),
    ("labelIds", "INBOX"),
    ("payload", []),
])
async def test_malformed_or_mismatched_message_cannot_become_evidence(provider, field, value):
    record = message()
    record[field] = value
    provider(record)
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (401, "authentication_required", False),
    (403, "access_denied", False),
    (404, "not_found", False),
    (429, "rate_limited", True),
    (503, "provider_error", True),
])
async def test_provider_errors_are_sanitized_with_bounded_retries(provider, status, code, retryable):
    attempts = 1 + gmail.READ_RETRIES if retryable else 1
    transport = provider(*[(status, {"error": {"message": "secret-token private-email-body"}})] * attempts)
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_message", {"message_id": "abc123"}, "secret-token")
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert caught.value.retry_after == 17
    assert "secret-token" not in str(caught.value) and "private-email-body" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(transport.requests) == attempts and transport.closed
    assert len(transport.sleeps) == attempts - 1


async def test_google_403_quota_failure_is_retryable_without_exposing_body(provider):
    transport = provider(*[(403, {"error": {"message": "private text", "errors": [
        {"reason": "userRateLimitExceeded"},
    ]}})] * (gmail.READ_RETRIES + 1))
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_search", {}, "token")
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert len(transport.requests) == gmail.READ_RETRIES + 1


async def test_transport_errors_are_safe_and_close_the_service(provider):
    transport = provider(*[TimeoutError("secret-token and private-email-body")] * (gmail.READ_RETRIES + 1))
    with pytest.raises(ConnectorError) as caught:
        await provider.call("gmail_search", {}, "secret-token")
    assert caught.value.code == "provider_unavailable"
    assert "secret-token" not in str(caught.value)
    assert transport.closed
    assert len(transport.requests) == gmail.READ_RETRIES + 1


@pytest.mark.parametrize(("status", "reason"), [
    (429, "rateLimitExceeded"), (503, "backendError"),
    (403, "userRateLimitExceeded"), (403, "rateLimitExceeded"),
])
async def test_sdk_retries_transient_reads_and_returns_recovered_result(provider, status, reason):
    transport = provider(
        (status, {"error": {"errors": [{"reason": reason}]}}), message(),
    )
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["id"] == "abc123" and result["content_complete"]
    assert len(transport.requests) == 2 and transport.sleeps == [1.0]
    assert transport.closed


async def test_sdk_stops_retrying_when_auth_fails_after_a_transient_error(provider):
    transport = provider(
        (503, {"error": {"errors": [{"reason": "backendError"}]}}),
        (401, {"error": {"message": "private authentication detail"}}),
        message(),
    )
    with pytest.raises(ConnectorError) as failure:
        await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert failure.value.code == "authentication_required"
    assert not failure.value.retryable
    assert len(transport.requests) == 2 and transport.sleeps == [1.0]
    assert len(transport.responses) == 1 and transport.closed


async def test_search_retries_metadata_reads_without_advancing_the_page(provider):
    transport = provider(
        {"messages": [{"id": "abc123", "threadId": "def456"}], "nextPageToken": "page-2"},
        (429, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}),
        message(),
    )
    result = await provider.call("gmail_search", {"query": "is:unread"}, "token")
    assert result["messages"][0]["id"] == "abc123"
    assert result["next_arguments"]["cursor"] == "page-2"
    assert result["coverage"]["page_complete"] and result["coverage"]["partial"]
    assert transport.requests[1]["path"] == transport.requests[2]["path"]
    assert len(transport.requests) == 3 and transport.closed


def test_real_service_is_token_only_with_private_timeout_transport_and_no_refresh():
    first, second = gmail._service("first-token"), gmail._service("second-token")
    try:
        assert first._http is not second._http
        assert first._http.http.timeout == gmail.HTTP_TIMEOUT_SECONDS
        assert first._http.credentials.scopes == [gmail.SCOPE]
        assert first._http.credentials.refresh_token is None
        assert first._http._refresh_status_codes == ()
        assert first._http._max_refresh_attempts == 0
        assert first._http.http.follow_redirects is False
    finally:
        first.close()
        second.close()


async def discovery_result(result, tool="message"):
    grant = Grant(
        id="11111111-1111-1111-1111-111111111111", kind="gmail", revision=1,
        credential="synthetic-only", read_only=True,
        policy={"oauth": True, "allowed_tools": ["message", "search", "thread"]},
    )
    scope = RunScope(
        session_id="synthetic-session", turn_id="synthetic-turn", actor_id="synthetic-owner",
        model="test", grants=[grant], attention=True, discovery=True,
    )
    name = f"gmail_{grant.id[:8]}_{tool}"
    bounded = await DiscoveryBounds().after_tool_execute(
        RunContext(deps=scope, model=TestModel(), usage=RunUsage()),
        call=ToolCallPart(name, {}, "read"), tool_def=ToolDefinition(name=name),
        args={}, result=result,
    )
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[ToolReturnPart(name, bounded, tool_call_id="read")]),
    ])).decode()
    return bounded, conversation, grant


async def test_full_message_scalar_evidence_survives_actual_discovery_bounds(provider):
    quote = "Please prepare the launch agenda before Thursday."
    payload = part(quote)
    payload["headers"].extend([
        {"name": "Subject", "value": "=?UTF-8?B?TWVldGluZyDimJU=?="},
        {"name": "From", "value": "Owner <owner@example.test>"},
    ])
    provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert quote not in result["snippet"]
    assert result["subject"] == "Meeting ☕"
    assert result["from"] == "Owner <owner@example.test>"
    assert result["text"] == quote and result["text_complete"]
    assert not result["text_truncated"] and result["content_complete"]
    assert provider_complete(result) and not provider_partial(result)
    bounded, conversation, grant = await discovery_result(result)
    assert bounded["scanned"] and not bounded["limited"]
    record = next(item for item in bounded["records"] if item.get("id") == "abc123")
    assert record["text"] == quote and record["subject"] == result["subject"] and record["from"] == result["from"]
    assert "headers" not in record and "body" not in record
    assert not any(key in record for key in ("url", "html_url", "permalink", "web_url"))
    evidence = DotEvidence(
        connection_id=grant.id, source=result["id"], source_version=result["revision"], quote=quote,
    )
    assert validate_evidence([evidence], conversation, [grant]) == [
        (grant.id, "abc123", "9007199254740993123"),
    ]
    for wrong in [
        evidence.model_copy(update={"source_version": result["internalDate"]}),
        evidence.model_copy(update={"source": result["threadId"]}),
    ]:
        with pytest.raises(ValueError, match="structured provider record"):
            validate_evidence([wrong], conversation, [grant])


async def test_search_header_evidence_is_preserved_but_does_not_claim_body_read(provider):
    record = message(payload={"headers": [
        {"name": "sUbJeCt", "value": "Agenda requested"},
        {"name": "FROM", "value": "Owner <owner@example.test>"},
    ]})
    provider({"messages": [{"id": "abc123", "threadId": "def456"}]}, record)
    result = await provider.call("gmail_search", {}, "token")
    item = result["messages"][0]
    assert item["subject"] == "Agenda requested" and item["from"] == "Owner <owner@example.test>"
    assert item["text"] is None and not item["text_complete"]
    assert item["body"] is None and item["content_complete"] is False
    assert result["coverage"]["page_complete"]  # Enumeration and content are distinct.
    bounded, conversation, grant = await discovery_result(result, "search")
    assert bounded["scanned"] and bounded["limited"]
    assert validate_evidence([DotEvidence(
        connection_id=grant.id, source=item["id"], source_version=item["revision"], quote=item["subject"],
    )], conversation, [grant]) == [(grant.id, item["id"], item["revision"])]


@pytest.mark.parametrize("cursor", ["", "previous-page-token"])
async def test_empty_search_page_preserves_first_page_and_continuation_coverage(provider, cursor):
    provider({"resultSizeEstimate": 0})
    result = await provider.call("gmail_search", {"cursor": cursor}, "token")
    assert result["coverage"]["page_complete"] and result["next_arguments"] is None
    assert result["coverage"]["has_more"] is False
    assert result["coverage"]["partial"] is bool(cursor)
    assert provider_partial(result) is bool(cursor)
    assert provider_complete(result) is (not cursor)
    bounded, _, _ = await discovery_result(result, "search")
    assert bounded["scanned"] and bounded["limited"] is bool(cursor)


async def test_scalar_text_limit_is_partial_even_when_nested_body_is_complete(provider):
    provider(message(payload=part("x" * 2000 + "UNSEEN_BODY_SUFFIX")))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["body"]["complete"] and not result["body"]["truncated"]
    assert result["body"]["text"].endswith("UNSEEN_BODY_SUFFIX")
    assert result["text"] == "x" * 2000
    assert result["text_truncated"] and not result["text_complete"] and not result["content_complete"]
    assert provider_partial(result) and not provider_complete(result)
    bounded, conversation, grant = await discovery_result(result)
    assert bounded["limited"]
    assert "UNSEEN_BODY_SUFFIX" not in conversation
    with pytest.raises(ValueError, match="structured provider record"):
        validate_evidence([DotEvidence(
            connection_id=grant.id, source=result["id"],
            source_version=result["revision"], quote="UNSEEN_BODY_SUFFIX",
        )], conversation, [grant])


@pytest.mark.parametrize(("header", "field"), [("Subject", "subject"), ("From", "from")])
async def test_scalar_header_limits_keep_omissions_explicit(provider, header, field):
    payload = part("Short body")
    payload["headers"].append({"name": header, "value": "a" * 2001})
    provider(message(payload=payload))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result[field] == "a" * 2000 and result[f"{field}_truncated"]
    assert result["text_complete"] and not result["headers_incomplete"]
    assert not result["content_complete"] and provider_partial(result)
    bounded, _, _ = await discovery_result(result)
    assert bounded["limited"]


async def test_attachment_omission_remains_partial_after_scalar_normalization(provider):
    provider(message(payload={"mimeType": "multipart/mixed", "parts": [
        part("Please review the attached proposal."),
        {**part("Proposal details not read"), "filename": "proposal.txt"},
    ]}))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["text_complete"] and result["body"]["complete"]
    assert result["attachments_skipped"] == 1 and result["content_complete"] is False
    bounded, _, _ = await discovery_result(result)
    assert bounded["limited"]
    record = next(item for item in bounded["records"] if item.get("id") == result["id"])
    assert record["attachments_skipped"] == 1 and not record["content_complete"]


async def test_invalid_body_encoding_cannot_become_complete_scalar_evidence(provider):
    provider(message(payload={"mimeType": "text/plain", "body": {"data": "not!base64"}}))
    result = await provider.call("gmail_message", {"message_id": "abc123"}, "token")
    assert result["text"] == "" and not result["text_complete"] and not result["content_complete"]
    assert result["body"]["issues"] == ["invalid_body_encoding"]
    assert provider_partial(result) and not provider_complete(result)
    bounded, _, _ = await discovery_result(result)
    assert bounded["limited"]


async def test_thread_messages_keep_their_own_scalar_evidence_and_revision(provider):
    provider(
        {"id": "def456", "historyId": "555", "messages": [{"id": "one"}, {"id": "two"}]},
        message("one", payload=part("Original meeting request.")),
        {**message("two", payload=part("The meeting is cancelled.")), "historyId": "9007199254740993124"},
    )
    result = await provider.call("gmail_thread", {"thread_id": "def456"}, "token")
    assert result["coverage"]["page_complete"] and not result["coverage"]["partial"]
    bounded, conversation, grant = await discovery_result(result, "thread")
    assert bounded["scanned"] and not bounded["limited"]
    evidence = DotEvidence(
        connection_id=grant.id, source="two", source_version="9007199254740993124",
        quote="The meeting is cancelled.",
    )
    assert validate_evidence([evidence], conversation, [grant]) == [
        (grant.id, "two", "9007199254740993124"),
    ]
    with pytest.raises(ValueError, match="structured provider record"):
        validate_evidence([evidence.model_copy(update={"source": "def456", "source_version": "555"})],
                          conversation, [grant])
