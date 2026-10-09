# Hosted media links

Moyai can explicitly share a saved capture or a sent image/video attachment as a stable bearer link. `media_list`, `media_share`, and `media_revoke` are built-in broker tools in every new or resumed session, independent of connected apps and harness choice. Listing, taking a screenshot, saving an attachment, and creating a PR do not automatically share media.

1. Ask for the selected media to be shared externally. Anyone with the resulting link can read it, including people outside the workspace.
2. Use `media_list` to select a `capture:<name>` or `attachment:<id>` and its revision. Only saved captures and attachments visible to the active session are eligible; arbitrary paths, remote URLs, byte strings, and MIME overrides are rejected. Paginate using `next_offset` if present.
3. Call `media_share` with that source, revision, and a stable `request_key`. Retrying that exact selection returns its durable receipt. Reusing the key with another selection fails. Changing the original file cannot change already shared bytes. `media_list` also returns prior receipts for recovery.
4. Use the receipt's `markdown` or `url` where authorized. Images use image Markdown. Videos use ordinary links because externally hosted video need not embed in GitHub PR Markdown. Sharing media itself does not publish or update a PR or comment.
5. Call `media_revoke` with the receipt's `id` to revoke future reads. Repeated revokes are safe. Retrying the original share cannot reactivate it. A fresh request key is a new explicit share. Deleting a session disables its links. Archiving and completing a session keep existing links usable.

## Deployment requirements

Configure `PUBLIC_URL` to the stable, externally reachable HTTPS origin and configure the existing private `OBJECT_STORAGE_*` destination described in [object-storage.md](object-storage.md). Keep the bucket private. The app reads and integrity-checks the selected snapshot; it never exposes object-store credentials, a public bucket URL, or a presigned storage URL. HTTP loopback is supported for local tests only and is not usable by GitHub's servers.

An edge proxy or access gateway **must allow anonymous GET and HEAD to `/media/<share-id>`**, preserving the `token` query parameter. Do not bypass authentication for `/api`, `/broker`, or other routes. The media endpoint still requires the bearer token and a live, unrevoked receipt belonging to a nondeleted session. Browser cookies are unnecessary. Existing source capture and attachment routes remain authenticated. Keep forwarding the configured Host header; TrustedHost checks remain in effect.

**Do not log query strings at the edge, CDN, load balancer, or analytics layer.** The token is a credential. Moyai's Uvicorn access filter strips query strings, its broker diagnostics omit arguments and responses, and media responses use `Referrer-Policy: no-referrer`. Infrastructure outside Moyai must apply the same policy. Do not configure CDN caching for `/media/`; success and error responses use `Cache-Control: no-store`. Keep the original public origin routable for the lifetime of existing links.

The encrypted bearer token, its hash, the immutable object reference, selected revision, and revocation state live in SQLite and its normal checkpoints. Preserve the database, original `ENCRYPTION_KEY`, and referenced private objects together. Follow the existing single-writer database deployment contract. Missing storage or configuration produces an actionable tool error, with no public-storage fallback. Expected tool errors are returned in the broker's tool result (`error`, `status_code`) so harnesses can recover without misclassifying a configuration problem as an uncertain network write; invalid run capabilities remain HTTP 401.

## Limits and revocation semantics

Supported content is decoded PNG, JPEG, GIF, or WebP images (at most 25 megapixels), and structurally validated WebM containers. SVG, HTML, text, and MIME spoofing are rejected. WebM identification validates its EBML document type and segment structure; it does not transcode or certify every codec frame.

Each selected file is at most 32 MiB. Per-session lifetime share quotas are 64 MiB and 128 receipts; workspace lifetime quotas default to 1 GiB and 4096 receipts (`MEDIA_SHARE_STORAGE_LIMIT_MB` and `MEDIA_SHARE_RECEIPT_LIMIT`). Each receipt counts even when the object store deduplicates identical bytes. Revocation preserves receipts and does not reset quotas, preventing an endless share/revoke loop from bypassing storage accounting. On quota exhaustion, an administrator must review retention/capacity; the workspace limits can be increased through configuration; this version has no automatic share garbage collection or quota-reset tool.

GET supports single byte ranges (206) and rejects unsatisfiable or malformed ranges (416). Multiple ranges are unsupported. HEAD returns the representation headers without a body. Conditional cache validation is intentionally absent; If-Range falls back to a full response. Every new read checks revocation, and checks again after object retrieval. Requests already in flight, downloads, screenshots, GitHub's image proxy, and other external caches can retain copies. Revocation cannot recall those copies.

## Local verification

`uv run pytest -q --tb=line tests/test_capture_release.py tests/test_access_logging.py tests/test_mcp_bridge.py tests/test_blob_storage.py tests/test_attachments.py`

The tests exercise the real broker and HTTP routes using generated fixtures, with an in-memory object-store double for workflow tests and signed boto3 transport tests for the storage adapter. `tests/fixtures/media-share.webm.b64` stores base64-encoded bytes of a 16×16 blue, silent synthetic video generated with:

```sh
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=16x16:d=0.2 -c:v libvpx -an -y /tmp/media-share.webm
base64 -w0 /tmp/media-share.webm > tests/fixtures/media-share.webm.b64
```
