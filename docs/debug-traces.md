# Debug traces

Moyai can export a session history as an ATIF v1.8 JSON trajectory. The trace
includes user messages, requester-visible reasoning, tool calls and their
results, along with timing and token metrics.

Each user step identifies its sender in `extra.author` using the saved friendly
name or email, with the stable account ID in `extra.author_id`. The previous
requester/teammate distinction is retained in `extra.author_relationship`.
Slack senders use the same verified profile names as conversation messages.
Unavailable profiles are explicitly labeled `Unknown sender`; message text and
the ATIF `source: "user"` role stay unchanged. Steering uses its saved input's
author when available, without attributing unidentified input to the exporter.

Open the session's Activity panel and select **Get debug trace**. The same
download is available from the session actions menu for native sessions.

Requester-owned turns include their recorded execution details. Turns started
by teammates show the saved assistant response without their private reasoning
or tool activity. Private memory, capability and credential tool arguments and
results are labeled in place with explanatory redaction text and redacted
metadata, rather than appearing as empty data. Treat the file as sensitive
because it can include prompts, reasoning, and non-private tool inputs and
outputs.

To report a problem, attach the JSON file to Moyai and describe what you expected
instead. Point to the relevant step when possible so the trace can be used to
identify where the interaction went wrong.
