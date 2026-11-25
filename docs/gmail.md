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

Plain text is preferred within a `multipart/alternative`; other mixed text
sections are retained. Python's standard `email` parser handles MIME header
parameters and encoded headers. Python's standard `HTMLParser` extracts text
from HTML and omits scripts, styles, document heads and templates. It does not
render HTML, follow links, load remote images or execute content.

Parsing visits at most 100 MIME parts and 16 nesting levels, decodes at most
128 KiB of body bytes, and bounds returned header and body text. Malformed
encoding, unknown character sets, missing body data, unsupported MIME parts,
external text attachment bodies and truncation are explicit. File attachments
are skipped; the attachment endpoint is never called. These are extraction
bounds after the SDK receives a message response, not a streaming response-size
limit. `messages.get(format="full")` can contain inline attachment bytes, which
are neither decoded nor returned by the adapter.

The official Google SDK runs in `asyncio.to_thread`. Each call owns and closes its
own `httplib2` transport with a 15-second socket timeout. Calls make at most 21
provider requests for search and 11 for a thread page. This is a per-request
transport timeout, not a wall-clock deadline for the entire tool call.
SDK retries are disabled (`num_retries=0`); the token-only authorized transport
also disables refresh. Existing Moyai execution and connection logic owns
retry/refresh decisions.

Errors expose safe codes, retryability, status and bounded numeric Retry-After
metadata. Raw provider error messages, response bodies, access tokens and
credential-bearing exception chains are not returned.

## Dependencies and verification

Runtime dependencies are declared in `native/pyproject.toml` and pinned by
`uv.lock`:

- `google-api-python-client` (tested with 2.202.0)
- `google-auth` (tested with 2.62.0)
- `google-auth-httplib2` (tested with 0.4.4)
- `httplib2` (tested with 0.32.1; also supplied by the official client)

`native/tests/test_gmail_tools.py` exercises the real SDK against a fixture
transport, including its methods, account routing, pagination, message/thread
identity, MIME extraction limits and sanitized failures. It also passes real
adapter records through Dot discovery's evidence capture and validation,
checking retained scalar content and explicit incomplete coverage.
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
