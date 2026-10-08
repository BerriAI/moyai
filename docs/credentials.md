# Secure access and 1Password

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Secure access requests

Obtaining access is part of completing a task. Durable sessions first use
`credentials_list` to discover authorized saved access, then `credentials_request`
to reuse a matching connection or open a secure form. Pending requests checkpoint
the conversation/files, release the Modal sandbox and wait for a Temporal wake.
Providing or declining access resumes the same message. Slack and subagent
requests link to the same secure web flow; never paste credentials into chat.

After verifying a manual browser login or existing authorized access, the agent
uses `credentials_list` to find the pending request ID and generation, then
`credentials_resolve` with `source=browser_session` or `existing_credentials`.
This marks only that request `satisfied` and removes its secure form. It stores
no secret, grants no access and does not replay the earlier requesting turn.
The agent must verify access first; a chat message saying "signed in" alone is
insufficient. Other requests stay pending. If the access later stops working,
create a new request with a new `request_key` after checking existing access.

Provider-filtered inventory also includes authorized active Shared vault
connections in `credential_sources`. An empty provider-key list does not mean
the vault is empty: inspect that source before asking for another key.

The broker enforces this order when `credentials_request` cannot reuse a saved
connection: if authorized Shared access is available, it returns
`lookup_required` with source metadata instead of opening a new form or pausing.
The agent requests the source using its `secret_id`, searches Shared through
`credentials_run`, and verifies the task's access. Working vault access is used
directly; no provider key needs to be copied into Moyai. If lookup or verification
fails, the agent retries the original request with `source_checks` containing
the source ID, revision and observed outcome. These reports require that the
same source revision was supplied to the credential executor in the current
turn. This receipt proves source access was supplied; the lookup outcome is
reported by the agent, not independently inspected by the broker. A rotated
source or new turn requires a new check. Values never belong in these reports.

Saved personal access takes precedence over organization access. Confirmed
invalid or expired authentication tries another unambiguous authorized saved
connection, then Shared lookup, before opening a replacement form. The failed
operation is never replayed automatically. Multiple saved matches still require
an appropriate selection; `secret_id` cannot select another user's private
credential, a different capability, expired access or another session's access.

For generic services, requests can include a verified service `setup_url` and
plain-text `setup_instructions`. These appear in both the access card and secure
form. The agent should explain the actual access method, such as an AWS access
portal and permitted role, instead of assuming every task needs a new API key.
Only absolute HTTPS setup links are shown; a missing link is omitted. Inference
providers retain their fixed setup destinations. An exact retry can add missing
guidance to an older pending request without creating a second access request.

The form requires two independent choices:

| Choice | Options |
| --- | --- |
| Who can use it | Personal (your requests) or Organization (everyone in this organization) |
| When Moyai can use it | This session and its subagents, or across future sessions |

Long-lived credentials are supported. Session use is bounded to the root chat,
including for organization credentials; it does not alter provider expiry.
Organization access remains managed by admins. Saved credentials remain encrypted
until revoked. The Secrets page exposes metadata, optional known expiry and
status, and supports editing, replacement and revocation without revealing the
saved value. Replacing a value preserves its identity; revision checks reject
stale edits. Existing session keys migrate to personal/session access, and
existing personal/organization keys retain future-session use.

Generic service access uses `provider=generic` with a stable capability `name`.
Choose `format=env` for a JSON object of environment variable names to string
values, or `format=file` and an `env_var` such as `KUBECONFIG` for multiline UTF-8
file contents. This supports token sets, kubeconfigs, service-account JSON and PEM
files. Values are limited to 128 KiB; runtime-control environment names and null
characters are rejected. A single unambiguous matching connection is reused;
multiple matches require a user choice. `credentials_list` returns metadata only.

`credentials_run` uses one or more approved request handles to run a command in
the sandbox. The internal broker rechecks the current requester, session, expiry
and revocation before releasing values to that executor. Environment values are
scoped to the subprocess; file values use Linux anonymous memory files with mode
0600, referenced through inherited descriptors. The executor closes descriptors,
stops the process group and redacts known values from bounded output before
returning it to the agent. It does not write credential values to the launch spec,
Temporal state or a credential file in the snapshotted filesystem. Approved
commands can themselves copy data or create service caches; this is not a
boundary against malicious sandbox code. Revocation prevents subsequent loads,
not an already-running command or use at the upstream service.

New images include `aws`, `kubectl`, `helm` and 1Password CLI `op` 2.30.0. Restored older images install a
missing supported CLI when a credential command references it. Installation
errors are separate from authentication failures. Generic access is a deliberate
policy expansion: approved credentials can now reach sandbox commands, with the
permissions the user supplied. Organization connection policies still apply.

Known expiry and recognizable invalid-authentication errors reopen the secure
request only after alternative saved access and available source lookup are
exhausted. Permission failures request additional access without invalidating the
shared credential for other tasks. Delayed errors from an earlier credential
revision cannot invalidate its replacement. Commands with multiple credentials
return their observed revisions so the agent can identify the failed connection;
ambiguous failures do not invalidate every credential. Completed or uncertain
writes are never automatically replayed. The agent verifies updated access and
continues from the saved task.

A command using Shared may fail because a retrieved destination key is invalid
while the 1Password service-account token remains valid. The executor reports
this uncertainty without invalidating the source automatically; the agent must
attribute the failure before reporting that the source itself is invalid.

Run `uv run python scripts/credential_discovery_demo.py` for a local walkthrough,
or add `--serve` and open `http://127.0.0.1:8798`. It exercises the actual broker,
saved-access fallback and credential executor with synthetic keys and a synthetic
vault result. It does not contact 1Password or an inference provider.

Fireworks, OpenAI, Anthropic, Together AI and Groq inference keys keep their
existing server-side proxy. Its fixed HTTPS origins/routes, redirect rejection,
non-streaming requests, response bounds and concurrency limit remain in place.
Use `credentials_http_request` or the returned SDK proxy instructions with
`max_retries=0` and `stream=False`. Those inference keys stay out of the sandbox,
and their provider charges remain separate from Moyai gateway spend.

Access is checked against the active message's server-owned identity, including
follow-ups by another teammate. Slack personal access requires a recent eligible
profile matching verified SSO; spend attribution links grant no access. Personal
credentials require Google sign-in outside local previews. This remains a
single-organization workspace, and personal credential ownership does not make
shared conversations or their results private.

### 1Password Shared vault

Use **Settings → Secrets → Connect 1Password** to save a service-account token.
The form has one masked token field; it stores `OP_SERVICE_ACCOUNT_TOKEN` under
the generic capability `1password-shared`. Choose **Organization** and **Across
future sessions** for team access, and record the token's actual expiry if known.
An existing matching connection opens for editing instead of creating another.
Only an administrator can save organization access. The value remains encrypted
in Moyai's existing credential store and is never returned by the Secrets API.

Create a dedicated Moyai service account in `berriai.1password.com` with
`Shared:read_items,write_items` only. Vault creation is unnecessary. Saving a
token does **not** restrict its upstream permissions: the account's 1Password
grants are the access boundary. Do not reuse another agent's token by copying it
through chat. See the official [service-account setup guide](https://developer.1password.com/docs/service-accounts/get-started/).

This uses the plain CLI, with no 1Password MCP or connector. New Modal images
install the pinned official Linux archive after verifying its SHA-256 checksum.
An older restored sandbox installs a missing `op` when a credential command
references it. Changing this code alone does not update the deployed service;
deploy it following the [active-session precautions](deployment.md#render-web-app-with-modal-sandboxes).

The agent checks saved Shared access before asking for a provider key. It uses
`credentials_request` with `provider=generic`, `name=1password-shared`, and
`format=env`, then runs `op` through `credentials_run` using the returned handle.
The existing broker rechecks scope, expiry, and revocation on every load. The
token reaches only the command subprocess, rather than every agent shell.
It is not part of the sandbox launch spec or prepared environment image.

First verify `op --version`, `op whoami`, and `op vault list`, then list item
metadata with `op item list --vault Shared`. Authentication is automatic from
the service-account token. For provider calls, use nonsecret references and
`op run`, retaining its default output masking, for example inside
`credentials_run`:

```sh
PROVIDER_API_KEY='op://Shared/<item>/credential' op run -- python provider_check.py
```

The program reads `PROVIDER_API_KEY` from its environment and reports only a
nonsecret success/failure result. Do not print values or disable output masking.
`credentials_run` redacts the injected service-account token; it cannot know
all newly fetched vault values. If direct `op read` is necessary, capture its
stdout in memory and pass it directly to the intended process in the same command.

For authorized writes, search for an existing item first and edit it; resolve
ambiguous matches before writing. Send secret JSON templates to `op item
create/edit` through stdin, capture their output in memory, and report only item
IDs/titles. Keep values out of command arguments, files, recordings and logs.
Renew expired tokens in 1Password and replace the saved value using **Edit**;
this preserves Moyai's credential identity and existing recovery flow.
