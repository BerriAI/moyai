# Gmail reads

`app/gmail_tools.py` provides three read-only operations for a personal Gmail
connection. Account selection, OAuth consent, token refresh, credential fencing
and proactive enrollment belong to Moyai's connection layer. This adapter
receives only the already-selected access token; it cannot choose another user.

| Tool | Result | Bounds and continuation |
| --- | --- | --- |
| `gmail_search` | Message IDs, history revisions, selected headers and Gmail snippets matching a Gmail query | 10 results by default, at most 20 per call. Follow `next_arguments` with the provider's opaque page token. |
| `gmail_message` | One message's headers and readable text | 12,000 text characters by default, at most 24,000. Check `body.issues` and `content_complete`. |
| `gmail_thread` | A page of individually read messages from one thread | 5 messages by default, at most 10. Follow `next_arguments`, including the thread history revision. |

Every operation uses `userId="me"` and requires
`https://www.googleapis.com/auth/gmail.readonly`. There are no send, draft,
mark-read, label-update, delete, attachment-download, or caller-selected URL
operations. `TOOLS` uses the same `(provider, write, Args, description)` convention
as the other adapters; every `write` value is `False`, and `READS` is a frozen set.
The entry point is:

```python
await gmail_tools.call(name, arguments, access_token)
```

`app/connectors.py` registers `TOOLS` and `READS` and dispatches these operations.
The personal OAuth connection layer selects the account, refreshes credentials
and checks current owner and execution authority before releasing reads. Tokens
stay in the trusted worker.

Connecting with **Use proactively** enabled enrolls the account through
`native/src/moyai/google_attention.py`. It updates the owner's canonical Dot
attention automation and uses the existing Temporal reconciliation. This adapter
only reads Gmail; it does not create scheduled jobs. See
[Personal Google connections](google-connections.md) for consent, deployment
registration, automatic enrollment and disconnect behavior.

## Evidence and completeness

Message `id` and `threadId` are the exact provider identifiers.
`historyId` and `revision` retain Gmail's decimal revision as a string, including
values too large for a JavaScript number. `internalDate` is the provider's
creation timestamp in epoch milliseconds. It is not an update timestamp and is
never used as the revision.

Search hydrates each listed ID using `messages.get(format="metadata")`. Its
`headers_and_snippet` records are useful partial evidence, not proof that a whole
message was read. A message deleted between listing and reading is represented
in `unread_messages`, not as a successful content record. Other provider failures
abort the call for the existing connection/execution layer to handle.

The response separates enumeration coverage from content coverage:

- `next_arguments` and `coverage.has_more` describe remaining records.
- `coverage.page_complete` says whether all IDs on this page were read.
- `coverage.partial` preserves omissions, additional pages and incomplete
  thread content. A continuation is still only part of its search or thread,
  including an empty final search page.
- `body.complete`, `body.truncated`, and `body.issues` describe readable text.
- `content_complete` also accounts for skipped attachments, incomplete headers
  and text omitted from the scalar evidence fields.
- `result_size_estimate` is Google's estimate, not a complete mailbox count.

Each message also exposes scalar `subject`, `from` and `text` fields, each
limited to 2,000 characters, so Dot discovery can retain useful content alongside
the exact message ID and revision. `subject_truncated`, `from_truncated`,
`text_truncated` and `text_complete` make those bounds explicit. Metadata-only
search results have `text=None` and `text_complete=False`; they remain partial
even when every listed ID was read. Full reads retain the larger text in
`body.text`, but omitted scalar text cannot support a discovery citation.
Top-level `attachments_skipped` reports omitted attachments.

Gmail's thread-get endpoint has no pagination. The adapter first requests only
the thread ID, history revision and message IDs; it then fetches bodies for the
requested page. Continuations pin the prior thread revision. If it changes,
`thread_changed` requires restarting from offset zero without `history_id`.
Reads are not an atomic mailbox snapshot: messages can change during a page.

No account-index Gmail web links are fabricated. A caller that needs a UI link
must construct it using the verified account identity and test account routing;
an opaque API message ID alone does not identify which signed-in browser account
should open it.

## MIME handling and execution

Gmail supplies the parsed MIME tree. Within `multipart/alternative`, the adapter
prefers a non-attachment plain-text part, then HTML, then a multipart container
for further bounded traversal. An unsupported final alternative such as
`text/calendar` does not displace an available HTML body. Other mixed text
sections are retained. Python's standard `email` utilities decode encoded
headers and MIME header parameters.

Beautiful Soup with the `html5lib` parser extracts HTML text using HTML5 tree
construction, including implicit closing tags such as an omitted `</head>`.
It removes document heads, scripts, styles and templates, and inserts line
breaks around selected block elements. It does not render HTML, follow links,
load remote images or execute content.

Body traversal processes at most 100 selected MIME parts and 16 nesting levels;
alternative selection also inspects headers in bounded sibling lists without
decoding their bodies. Extraction applies a 128 KiB decoded-body budget and
bounds returned header and body text. Malformed encoding, unknown character
sets, missing body data, unsupported MIME parts, external text attachment bodies
and truncation are explicit. File attachments are skipped; the attachment
endpoint is never called. These are extraction
bounds after the SDK receives a message response, not a streaming response-size
limit. `messages.get(format="full")` can contain inline attachment bytes, which
are neither decoded nor returned by the adapter.

The official Google SDK runs in `asyncio.to_thread`. Each call owns and closes its
own `httplib2` transport with a 15-second socket timeout and redirects disabled.
Search performs at most 21 logical SDK reads and a thread page at most 11,
including their initial list/identity read. Message hydration remains serial.

Each read uses `execute(num_retries=2)`: at most three SDK attempts, with the
SDK's randomized exponential backoff for supported transient transport errors,
HTTP 5xx, HTTP 429, and HTTP 403 reasons `rateLimitExceeded` or
`userRateLimitExceeded`. HTTP 401 and ordinary permission-denied 403 responses
are not retried, including when they follow a transient failure. This budget is
per logical read, not per tool call; the transport can also retry certain
connection failures internally. The socket timeout is not an aggregate
wall-clock deadline, and cancelling the awaiting coroutine does not stop an
already-running worker thread. Multiple reads and retries can therefore
accumulate latency.

Bundled discovery initialization still uses `num_retries=0`. The token-only
authorized transport disables credential refresh; the host remains responsible
for refreshing tokens and deciding whether to attempt a later tool call.

Errors expose safe codes, retryability, status and bounded numeric Retry-After
metadata. Raw provider error messages, response bodies, access tokens and
credential-bearing exception chains are not returned. Retry-After is returned
as metadata; retry delays within this adapter follow the SDK's backoff policy.

## Typed boundaries

`call` accepts `Mapping[str, object]` arguments and validates them into
`GmailSearch`, `GmailMessage` or `GmailThread` before creating a service.
The internal implementation uses these models rather than untyped argument
dictionaries. A strict `WireMessage` validates message identity, revision,
labels, snippet and envelope fields. MIME children remain untrusted objects
until bounded traversal reaches them, so validation does not recursively walk
an entire MIME tree before applying extraction limits.

Normalized bodies, messages, coverage and result envelopes have explicit
`TypedDict` shapes. `Result` is the union of message, search and thread results;
the returned values remain ordinary dictionaries with the existing field names.
Small protocols describe the generated SDK methods used by the adapter, with
the dynamic discovery factory isolated at that boundary. A keyword-only
`service_factory` allows trusted callers and tests to inject the SDK service;
it is not part of the model-visible tool arguments.

## Dependencies and verification

Runtime dependencies are declared in `native/pyproject.toml` and pinned by
`uv.lock`:

- `google-api-python-client` (tested with 2.202.0)
- `google-auth` (tested with 2.62.0)
- `google-auth-httplib2` (tested with 0.4.4)
- `httplib2` (tested with 0.32.1; also supplied by the official client)
- `beautifulsoup4` (tested with 4.15.0)
- `html5lib` (tested with 1.1)

`native/tests/test_gmail_tools.py` exercises the real SDK against an injected fixture
transport, including its methods, account routing, pagination, message/thread
identity, MIME extraction limits, implicit HTML closing tags and supported
alternative selection. Retry tests cover transient recovery, exhausted budgets,
authentication failures and preservation of pagination during retries. They
replace the SDK request's sleep/random functions per instance to verify backoff
without waiting. The suite also passes real adapter records through Dot
discovery's evidence capture and validation, checking retained scalar content
and explicit incomplete coverage.
`native/tests/test_google_attention.py` uses disposable PostgreSQL source and
native schemas to exercise personal-connection endpoints, enrollment,
revocation and the existing Temporal scheduler.

These fixtures supplement actual baseline/final Moyai application verification.
Real Google consent and reads require a registered deployment OAuth app and an
authorized account. Fixture results do not establish that a deployment has
completed registration or that Gmail is connected; end-to-end verification
must exercise the real app, configured model and provider and record the result.

## Provider references

- [Message list and search](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
- [Message resource and revision semantics](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- [Message reads and formats](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)
- [Thread reads](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get)
- [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Official Python SDK thread safety](https://googleapis.github.io/google-api-python-client/docs/thread_safety.html)
