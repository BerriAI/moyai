# Capture and package the result in Moyai

## Real behavior

Use the sandbox's normal browser tools and current schemas. Start
`browser_record_start` before the meaningful actions, exercise the flow, then call
`browser_record_stop`. Aim for a short, readable recording, usually 15–45 seconds.
Take `browser_screenshot` when the useful result is visible. Use safe fixture data
and exclude credentials or unrelated conversations.

These tools capture the sandbox browser. For a backend/CLI change, a local page may
display the **actual saved request or command output** for recording; label it as
such. Do not animate a hypothetical result or describe a mockup as an executed demo.
Documentation-only changes can demonstrate the rendered document.

Check the returned capture paths and inspect the result. A successful recording
tool call alone does not show that the intended behavior appeared in the recording.
If browser capture is unavailable, keep the executable reproduction and verified
results, and state the specific capture limitation.

## Where the evidence goes

In the chat, link the exact saved paths returned by the capture tools. A sandbox
localhost URL is local and may stop working when its process or workspace ends;
provide the local run steps and test input so the reviewer can reproduce it.

GitHub reviewers cannot access a sandbox-local path. Use an existing authorized
preview or approved media-sharing result when available. Otherwise put reproduction
steps in the PR and keep the private captures in the session. Follow the platform's
media-sharing rules; this skill grants no new permission to expose recordings.

If sharing was authorized, use only returned links for the selected captures.
Avoid committing recordings or temporary fixture output to the source repository.
Do not deploy a public site merely to obtain a demo link.

## Useful final description

- Describe the concrete trigger and resulting behavior.
- Name the checks actually run and what they establish.
- Include a working preview or local run command with the relevant input.
- Identify pending CI, unavailable integration checks or synthetic providers.
- Link the confirmed PR and selected screenshot/recording in the handoff.

Communication to Slack or another destination follows the user's authorization and
the connected tools. The presence of this skill is not permission to send messages.
