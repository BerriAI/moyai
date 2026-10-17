# Debug traces

Moyai can export a session history as an ATIF v1.8 JSON trajectory. The trace
includes user messages, requester-visible reasoning, tool calls and their
results, along with timing and token metrics.

Open the session's Activity panel and select **Get debug trace**. The same
download is available from the session actions menu for native sessions.

Requester-owned turns include their recorded execution details. Turns started
by teammates show the saved assistant response without their private reasoning
or tool activity. Private memory, capability and credential tool arguments and
results are redacted. Treat the file as sensitive because it can include prompts,
reasoning, and non-private tool inputs and outputs.

To report a problem, attach the JSON file to Moyai and describe what you expected
instead. Point to the relevant step when possible so the trace can be used to
identify where the interaction went wrong.
