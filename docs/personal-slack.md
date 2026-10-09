# Optional personal Slack connections

Members can open **Settings > Personal Slack** and authorize their own Slack
account. Slack reads prefer that account whenever it is configured. Without a
personal grant, reads use the existing organization connection. Expired, revoked,
or failing personal access returns an error and never changes identities silently.
Disconnecting the personal grant restores organization fallback.

## Installation

Keep the existing Slack OAuth client configuration. Add this redirect URL to the
Slack app's OAuth settings, substituting the deployed Moyai origin:

```
https://<moyai-origin>/oauth/slack/personal/callback
```

The personal flow requests user scopes `search:read`, `channels:history`,
`groups:history`, `im:history`, and `mpim:history`. It does not replace the shared
organization grant or bot installation. Workspace app approval policies still apply.
Individual Google or Cloudflare sign-in is required; shared-password accounts cannot
own personal grants. One personal Slack workspace/account is supported per individual.

## Private sessions

Use **Start private web chat** from Personal Slack settings. Personal reads require
an owner-only web session. Existing sessions retain their shared behavior; they
cannot be converted. Private sessions remain private after disconnecting Slack.
Other users, including organization administrators, cannot open them through Moyai.

Private sessions currently do not support Slack mirroring, automations, delegation,
side chats, reusable memory writes, media sharing, or connector writes. Personal
reads in shared sessions, Slack DMs/threads and automations return private-chat
guidance. Organization policies remain authoritative, and bot sends in ordinary
sessions continue to use the organization bot.

Private model requests still go to the configured inference gateway/provider.
Moyai suppresses its own shared tracing of private content; operators must configure
gateway/provider retention separately.

## Verification

```
python -m pytest tests/test_personal_slack.py tests/test_private_sessions.py tests/test_private_sinks.py tests/test_slack.py -q --tb=line
node --test tests/test_personal_slack.cjs
PYTHONPATH=. python scripts/personal_slack_http_probe.py
```

The HTTP probe starts the real app and SQLite storage on a temporary loopback port,
uses synthetic Slack responses, and checks OAuth callback, private creation,
owner isolation, and personal-to-organization selection after disconnect.
It does not authorize a real Slack account or make model requests.
