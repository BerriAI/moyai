---
name: artifacts
description: Preserve generated files, child outputs and media and deliver exact, accessible artifacts to the user.
---

Publish intended deliverables with `artifact_publish(path=...)` before reporting them as delivered. The tool uploads all bytes, verifies the hash, and returns an immutable revision and authenticated URL. `artifacts_list` enumerates current/direct-child outputs; follow its cursor. `artifact_read` reads exact byte ranges and returns base64 for binary or split UTF-8 data. `artifact_fetch` downloads a verified revision to a new workspace file without overwriting existing work.

A local file is intermediate work. Preserve deliverables through the available artifact registration/storage tools and verify their size, hash, revision and readable/downloadable location. Keep evidence late in directory ordering and binary/media output; a recovery archive or shortened preview is not a complete artifact manifest. Read large items by ranges or downloads and verify the exact referenced revision when integrating child outputs.

Link exact returned paths or authenticated file URLs in the final answer. Use Markdown image embeds for viewable images and ordinary links for video when the destination cannot embed it. Do not infer that an upload or final message was delivered merely because the model produced text; use saved result and delivery receipts.

For externally accessible media, discover `media_list`, `media_share` and `media_revoke`. Listing does not share. Sharing requires the user's authorization for the intended audience; explain when anyone with the link can read the artifact and that revocation cannot recall copies. Use returned source revisions and stable request keys. Never implicitly share all attachments or infer permission to publish a PR/comment from permission to create a media link.

A Slack-bound session delivers its answer automatically. Use the returned integration capabilities for native uploads or protected links; do not send a duplicate answer with `slack_send`. Confirm that the intended audience can use the delivered references. If delivery is uncertain, preserve the result and report the delivery state rather than repeating an external write blindly.
