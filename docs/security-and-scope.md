# Security, scope, and references

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Scope and next steps

The current boundary is a single shared internal workspace. A GitHub App supports the configured repository, including private code, with direct PR creation in authorized repositories. Per-user app grants, a live remote-desktop viewer, and multi-instance database storage are not included. Files and conversation resume between turns. The main Computer browser restores its session's encrypted authentication and tabs; running processes do not resume.

For a broader team rollout, extend the existing Google SSO with per-user session authorization, move orchestration to a durable worker service with Postgres, and verify live writes against explicitly authorized disposable destinations. Keep a budget-limited LiteLLM key: request-count and output limits do not substitute for a currency budget. Network egress from the sandbox is not restricted to an allowlist, and downloaded source/app content remains untrusted input to the agent.

An encrypted database alone does not protect credentials from an attacker who also obtains the adjacent encryption key or controls the host. Store the cloud encryption key separately, restrict access to the host and backups, and rotate provider grants as needed. Stopping a run revokes new broker calls but cannot retract an external write already in flight.

## References inspected

- [Hermes programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration)
- [Hermes Python library](https://hermes-agent.nousresearch.com/docs/guides/python-library)
- [Hermes package management](https://hermes-agent.nousresearch.com/docs/reference/package-management)
- [Hermes source pin](https://github.com/NousResearch/hermes-agent/tree/7968c72a3cb80beaae51948378944dd6e3423b96)
- [Modal sandboxes](https://modal.com/docs/guide/sandboxes)
- [Modal VM sandboxes](https://modal.com/docs/guide/vm-sandboxes)
- [Linear OAuth](https://linear.app/developers/oauth-2-0-authentication)
- [Slack OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)
- [Notion integrations](https://developers.notion.com/docs/authorization)

Hermes is an independent MIT-licensed project from Nous Research. Moyai builds on it and is not affiliated with Nous Research.
